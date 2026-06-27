from __future__ import annotations

import asyncio
import time

from vpn_access_bot.advertising_readiness import (
    CapacityService,
    CommerceAdmissionDecision,
    CommerceOperationKind,
    CommercePolicyRepository,
)
from vpn_access_bot.config import Settings
from vpn_access_bot.mediator_client import MediatorClient, MediatorClientError, MediatorReadiness
from vpn_access_bot.models import utc_now

CommerceReadiness = CommerceAdmissionDecision


def readiness_failure_reason(
    readiness: MediatorReadiness,
    operation_kind: CommerceOperationKind = CommerceOperationKind.NEW_PURCHASE,
) -> str | None:
    if not readiness.migrations_current:
        return "migrations_pending"
    if getattr(readiness, "device_issuance_version", 1) < 2:
        return "device_issuance_v2_unavailable"
    if not getattr(readiness, "unified_subscription_feed_enabled", False):
        return "unified_subscription_feed_unavailable"
    if not getattr(readiness, "shared_subscription_links_only", False):
        return "shared_subscription_links_only_unavailable"

    if readiness.server_count <= 0:
        return "catalog_empty"
    if readiness.status not in {"ready", "degraded"}:
        return "service_not_ready"
    if readiness.catalog_state not in {"fresh", "stale"}:
        return "catalog_unavailable"

    strict_operations = {
        CommerceOperationKind.NEW_PURCHASE,
        CommerceOperationKind.TRIAL,
        CommerceOperationKind.RESUME,
        CommerceOperationKind.UPGRADE_DEVICES,
        CommerceOperationKind.EXTEND_AND_UPGRADE,
    }
    if operation_kind in strict_operations:
        if readiness.status != "ready" or readiness.catalog_state != "fresh":
            return "fresh_catalog_required"
    return None


class CommerceReadinessService:
    _RECOVERY_OPERATIONS = frozenset(
        {
            CommerceOperationKind.COMPLETE_PAID_ORDER,
            CommerceOperationKind.RETRY_ACTIVATION,
            CommerceOperationKind.REFUND_PREPARE,
            CommerceOperationKind.REFUND_COMPENSATION,
        }
    )

    def __init__(
        self,
        mediator_client: MediatorClient,
        cache_seconds: int = 10,
        session_factory=None,
        settings: Settings | None = None,
    ) -> None:
        self._mediator_client = mediator_client
        self._cache_seconds = max(cache_seconds, 0)
        self._session_factory = session_factory
        self._settings = settings
        self._cached_mediator: tuple[float, MediatorReadiness] | None = None

    async def _get_mediator(
        self, *, force: bool, timeout_seconds: float | None
    ) -> tuple[MediatorReadiness | None, str | None]:
        now_monotonic = time.monotonic()
        if (
            not force
            and self._cached_mediator is not None
            and now_monotonic - self._cached_mediator[0] <= self._cache_seconds
        ):
            return self._cached_mediator[1], None

        try:
            if timeout_seconds is None:
                mediator = await self._mediator_client.get_readiness()
            else:
                async with asyncio.timeout(timeout_seconds):
                    mediator = await self._mediator_client.get_readiness()
        except TimeoutError:
            return None, "mediator_timeout"
        except MediatorClientError:
            return None, "mediator_unavailable"

        self._cached_mediator = (time.monotonic(), mediator)
        return mediator, None

    async def _load_policy(self):
        if self._session_factory is None or self._settings is None:
            return None
        async with self._session_factory() as session:
            policy = await CommercePolicyRepository(session).get()
            await session.commit()
            return policy

    async def _load_local_state(self, mediator: MediatorReadiness | None):
        if self._session_factory is None or self._settings is None:
            return None, None
        async with self._session_factory() as session:
            policy = await CommercePolicyRepository(session).get()
            capacity = await CapacityService(session, self._settings).capture(mediator)
            await session.commit()
            return policy, capacity

    @staticmethod
    def _policy_flag(operation: CommerceOperationKind) -> str | None:
        return {
            CommerceOperationKind.NEW_PURCHASE: "new_purchases_enabled",
            CommerceOperationKind.TRIAL: "trials_enabled",
            CommerceOperationKind.RENEWAL: "renewals_enabled",
            CommerceOperationKind.RESUME: "resumes_enabled",
            CommerceOperationKind.UPGRADE_DEVICES: "device_upgrades_enabled",
            CommerceOperationKind.EXTEND_AND_UPGRADE: "extend_and_upgrade_enabled",
        }.get(operation)

    def _evaluate(
        self,
        *,
        operation: CommerceOperationKind,
        policy,
        capacity,
        mediator: MediatorReadiness | None,
        mediator_error: str | None,
    ) -> CommerceReadiness:
        recovery_operation = operation in self._RECOVERY_OPERATIONS
        allowed = True
        reason = "recovery_only" if recovery_operation else "ready"
        policy_version = policy.version if policy is not None else 0

        flag_name = self._policy_flag(operation)
        if policy is not None and flag_name is not None and not bool(getattr(policy, flag_name)):
            allowed = False
            reason = f"policy_{operation.value}_disabled"

        if allowed and not recovery_operation:
            if mediator_error is not None:
                allowed = False
                reason = mediator_error
            elif mediator is None:
                allowed = False
                reason = "mediator_unavailable"
            else:
                failure = readiness_failure_reason(mediator, operation)
                if failure is not None:
                    allowed = False
                    reason = failure

        capacity_sensitive = operation in {
            CommerceOperationKind.NEW_PURCHASE,
            CommerceOperationKind.TRIAL,
            CommerceOperationKind.RESUME,
            CommerceOperationKind.UPGRADE_DEVICES,
            CommerceOperationKind.EXTEND_AND_UPGRADE,
        }
        if (
            allowed
            and capacity_sensitive
            and policy is not None
            and policy.capacity_enforcement_enabled
            and capacity is not None
            and capacity.state in {"saturated", "unknown"}
        ):
            allowed = False
            reason = capacity.reason_code

        facts: dict[str, object] = {
            "operationKind": operation.value,
            "mediatorStatus": mediator.status if mediator is not None else "unavailable",
            "catalogState": mediator.catalog_state if mediator is not None else "unavailable",
            "serverCount": mediator.server_count if mediator is not None else 0,
            "capacityState": capacity.state if capacity is not None else "not_evaluated",
            "admissionMode": "recovery_only" if recovery_operation else "commerce",
        }
        return CommerceReadiness(
            allowed=allowed,
            reason_code=reason,
            operation_kind=operation,
            policy_version=policy_version,
            snapshot_at_utc=utc_now(),
            mediator=mediator,
            capacity=capacity,
            facts=facts,
        )

    async def check(
        self,
        *,
        operation_kind: CommerceOperationKind | str = CommerceOperationKind.NEW_PURCHASE,
        force: bool = False,
        timeout_seconds: float | None = None,
    ) -> CommerceReadiness:
        operation = CommerceOperationKind(operation_kind)

        # Local policy is never served from the readiness cache. This makes an emergency stop
        # visible immediately, even while a positive Mediator snapshot is still reusable.
        early_policy = await self._load_policy()
        early_flag = self._policy_flag(operation)
        if (
            early_policy is not None
            and early_flag is not None
            and not bool(getattr(early_policy, early_flag))
        ):
            return self._evaluate(
                operation=operation,
                policy=early_policy,
                capacity=None,
                mediator=None,
                mediator_error=None,
            )

        if operation in self._RECOVERY_OPERATIONS:
            return self._evaluate(
                operation=operation,
                policy=early_policy,
                capacity=None,
                mediator=None,
                mediator_error=None,
            )

        mediator, mediator_error = await self._get_mediator(
            force=force, timeout_seconds=timeout_seconds
        )
        policy, capacity = await self._load_local_state(mediator)
        if policy is None:
            policy = early_policy
        return self._evaluate(
            operation=operation,
            policy=policy,
            capacity=capacity,
            mediator=mediator,
            mediator_error=mediator_error,
        )

    async def all_decisions(self, *, force: bool = False) -> list[CommerceReadiness]:
        # Monitoring obtains one immutable external/local snapshot per scrape instead of
        # issuing one Mediator request and one write-producing capacity capture per operation.
        mediator, mediator_error = await self._get_mediator(force=force, timeout_seconds=None)
        policy, capacity = await self._load_local_state(mediator)
        return [
            self._evaluate(
                operation=operation,
                policy=policy,
                capacity=capacity,
                mediator=mediator,
                mediator_error=mediator_error,
            )
            for operation in CommerceOperationKind
        ]
