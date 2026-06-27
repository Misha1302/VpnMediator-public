from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from vpn_access_bot.constants import ENTITLEMENT_STATUS_ACTIVE
from vpn_access_bot.mediator_client import (
    EntitlementPayload,
    MediatorClient,
    MediatorClientError,
    MediatorEntitlementDetails,
    MediatorEntitlementOperationResult,
)
from vpn_access_bot.models import EntitlementOperation, Order, Subscription, utc_now
from vpn_access_bot.repositories import (
    EntitlementOperationRepository,
    RefundOperationRepository,
    SubscriptionRepository,
    to_aware_utc,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppliedEntitlement:
    public_guid: str
    version: int
    status: str
    valid_until_utc: datetime
    max_device_tokens: int
    applied_at_utc: datetime
    created_subscription: bool = False


class EntitlementOperationCoordinator:
    """Durable coordinator for all Bot -> Mediator entitlement mutations.

    The local operation row is committed before any request may leave the process. A stable
    operation id is used for every retry. When an outcome is uncertain, recovery asks Mediator
    for the durable operation result before deciding whether a retry is safe.
    """

    def __init__(
        self,
        session: AsyncSession,
        mediator_client: MediatorClient,
        *,
        worker_id: str | None = None,
    ) -> None:
        self._session = session
        self._mediator_client = mediator_client
        self._worker_id = worker_id or f"bot-{uuid4().hex[:12]}"

    async def prepare_order(
        self,
        order: Order,
        *,
        enforce_refund_block: bool = True,
    ) -> EntitlementOperation:
        if enforce_refund_block and await RefundOperationRepository(
            self._session
        ).has_blocking_for_user(order.user_id):
            raise MediatorClientError(
                "A refund operation blocks entitlement changes for this subscription.",
                error_code="refund_operation_in_progress",
            )
        if order.target_subscription_id is not None:
            target = await SubscriptionRepository(self._session).get_by_id(
                order.target_subscription_id
            )
            if target is None or target.user_id != order.user_id:
                raise MediatorClientError(
                    "Order target subscription is invalid.",
                    error_code="invalid_target_subscription",
                )
            if target.reconciliation_state != "healthy":
                raise MediatorClientError(
                    "Subscription is quarantined by reconciliation.",
                    error_code="reconciliation_blocked",
                )
        duration_days = (
            order.purchased_duration_days
            if order.purchased_duration_days is not None
            else (order.requested_duration_days or order.duration_days)
        )
        duration_delta_seconds = max(duration_days, 0) * 86400
        operation_repository = EntitlementOperationRepository(self._session)
        operation = await operation_repository.create_once(
            user_id=order.user_id,
            operation_type="paid_activation" if order.amount_minor_units > 0 else "complimentary",
            source_entity_type="order",
            source_entity_id=order.public_order_id,
            idempotency_key=f"entitlement:order:{order.public_order_id}",
            subscription_id=order.target_subscription_id,
            duration_delta_seconds=duration_delta_seconds,
            requested_device_limit=order.requested_max_devices or order.selected_max_devices,
            requested_status=ENTITLEMENT_STATUS_ACTIVE,
            observed_valid_until_utc=order.base_valid_until_utc,
        )
        if operation.state == "manual_review":
            rearmed = await operation_repository.rearm_after_reconciliation(operation)
            if not rearmed:
                raise MediatorClientError(
                    "Entitlement operation requires manual review.",
                    error_code="entitlement_operation_manual_review",
                )
        await self._session.flush()
        return operation

    async def prepare_generic(
        self,
        *,
        user_id: int,
        subscription_id: int | None,
        operation_type: str,
        source_entity_type: str,
        source_entity_id: str,
        duration_delta_seconds: int,
        requested_device_limit: int | None,
        requested_status: str,
        observed_valid_until_utc: datetime | None = None,
    ) -> EntitlementOperation:
        if operation_type != "refund_compensation" and await RefundOperationRepository(
            self._session
        ).has_blocking_for_user(user_id):
            raise MediatorClientError(
                "A refund operation blocks entitlement changes for this subscription.",
                error_code="refund_operation_in_progress",
            )
        operation = await EntitlementOperationRepository(self._session).create_once(
            user_id=user_id,
            operation_type=operation_type,
            source_entity_type=source_entity_type,
            source_entity_id=source_entity_id,
            idempotency_key=(
                f"entitlement:{operation_type}:{source_entity_type}:{source_entity_id}"
            ),
            subscription_id=subscription_id,
            duration_delta_seconds=duration_delta_seconds,
            requested_device_limit=requested_device_limit,
            requested_status=requested_status,
            observed_valid_until_utc=observed_valid_until_utc,
        )
        await self._session.flush()
        return operation

    async def apply_order(
        self,
        operation: EntitlementOperation,
        order: Order,
        target_subscription: Subscription | None,
    ) -> AppliedEntitlement:
        if operation.state in {"external_applied", "local_commit_pending", "completed"}:
            return self._from_operation(operation)

        repository = EntitlementOperationRepository(self._session)
        claimed = await repository.claim(operation, owner=self._worker_id)
        if not claimed:
            raise MediatorClientError(
                "Another entitlement operation owns this subscription.",
                error_code="access_operation_in_progress",
            )
        await self._session.commit()

        if target_subscription is None:
            return await self._create_subscription(
                operation,
                customer_reference=f"telegram:{order.user.telegram_id}",
            )
        return await self._apply_to_existing(operation, target_subscription)

    async def apply_new_subscription(
        self,
        operation: EntitlementOperation,
        *,
        customer_reference: str,
    ) -> AppliedEntitlement:
        if operation.state in {"external_applied", "local_commit_pending", "completed"}:
            return self._from_operation(operation)

        repository = EntitlementOperationRepository(self._session)
        claimed = await repository.claim(operation, owner=self._worker_id)
        if not claimed:
            raise MediatorClientError(
                "Another entitlement operation owns this subscription.",
                error_code="access_operation_in_progress",
            )
        await self._session.commit()
        return await self._create_subscription(
            operation,
            customer_reference=customer_reference,
        )

    async def apply_generic(
        self,
        operation: EntitlementOperation,
        subscription: Subscription,
        *,
        exact_valid_until_utc: datetime | None = None,
        exact_device_limit: int | None = None,
        required_current_version: int | None = None,
        allow_stale_observation_supersede: bool = False,
    ) -> AppliedEntitlement | None:
        if operation.state == "completed":
            return self._from_operation(operation)

        repository = EntitlementOperationRepository(self._session)
        claimed = await repository.claim(operation, owner=self._worker_id)
        if not claimed:
            raise MediatorClientError(
                "Another entitlement operation owns this subscription.",
                error_code="access_operation_in_progress",
            )
        await self._session.commit()

        if allow_stale_observation_supersede and operation.observed_valid_until_utc is not None:
            remote = await self._mediator_client.get_entitlement(subscription.public_guid)
            remote_valid_until = self._parse_required(remote.valid_until_utc)
            if remote_valid_until > to_aware_utc(operation.observed_valid_until_utc):
                await repository.mark_superseded(operation, "authoritative_entitlement_advanced")
                await self._session.commit()
                return None

        return await self._apply_to_existing(
            operation,
            subscription,
            exact_valid_until_utc=exact_valid_until_utc,
            exact_device_limit=exact_device_limit,
            required_current_version=required_current_version,
        )

    async def recover_remote_result(
        self, operation: EntitlementOperation
    ) -> AppliedEntitlement | None:
        remote_operation = await self._mediator_client.get_entitlement_operation(
            operation.public_id
        )
        if remote_operation is None:
            return None
        await self._record_remote_result(operation, remote_operation)
        await self._session.commit()
        return self._from_operation(operation)

    async def _create_subscription(
        self,
        operation: EntitlementOperation,
        *,
        customer_reference: str,
    ) -> AppliedEntitlement:
        repository = EntitlementOperationRepository(self._session)
        now = utc_now()
        valid_until = now + timedelta(seconds=operation.duration_delta_seconds)
        operation.expected_version = 0
        operation.intended_valid_until_utc = valid_until
        operation.requested_device_limit = operation.requested_device_limit or 1
        operation.requested_status = ENTITLEMENT_STATUS_ACTIVE
        await repository.mark_request_sent(operation)
        await self._session.commit()

        try:
            result = await self._mediator_client.create_subscription(
                external_request_id=operation.public_id,
                customer_reference=customer_reference,
                note=f"operation:{operation.public_id}",
                entitlement=EntitlementPayload(
                    version=1,
                    status=ENTITLEMENT_STATUS_ACTIVE,
                    valid_until_utc=valid_until.isoformat(),
                    max_device_tokens=operation.requested_device_limit,
                ),
            )
        except MediatorClientError as exception:
            await self._session.rollback()
            operation = await EntitlementOperationRepository(self._session).get_by_public_id(
                operation.public_id
            )
            if operation is None:
                raise
            # Creation is idempotent by external_request_id. A retry cannot duplicate access.
            await EntitlementOperationRepository(self._session).mark_failed_retriable(
                operation,
                exception.error_code or "mediator_outcome_unknown",
            )
            await self._session.commit()
            raise

        await repository.mark_external_applied(
            operation,
            result_version=1,
            result_status=ENTITLEMENT_STATUS_ACTIVE,
            result_valid_until_utc=valid_until,
            result_device_limit=operation.requested_device_limit,
            applied_at_utc=utc_now(),
            external_subscription_public_guid=result.public_guid,
        )
        operation.subscription_id = None
        await self._session.commit()
        return AppliedEntitlement(
            public_guid=result.public_guid,
            version=1,
            status=ENTITLEMENT_STATUS_ACTIVE,
            valid_until_utc=valid_until,
            max_device_tokens=operation.requested_device_limit,
            applied_at_utc=to_aware_utc(operation.external_applied_at_utc or utc_now()),
            created_subscription=True,
        )

    async def _apply_to_existing(
        self,
        operation: EntitlementOperation,
        subscription: Subscription,
        *,
        exact_valid_until_utc: datetime | None = None,
        exact_device_limit: int | None = None,
        required_current_version: int | None = None,
    ) -> AppliedEntitlement:
        repository = EntitlementOperationRepository(self._session)

        if operation.state in {"external_applied", "local_commit_pending"}:
            return self._from_operation(operation, public_guid=subscription.public_guid)

        recovered = await self.recover_remote_result(operation)
        if recovered is not None:
            return recovered

        last_conflict: MediatorClientError | None = None
        for _ in range(3):
            remote = await self._mediator_client.get_entitlement(subscription.public_guid)
            if required_current_version is not None and remote.version != required_current_version:
                await repository.mark_manual_review(
                    operation,
                    "required_entitlement_version_changed",
                )
                await self._session.commit()
                raise MediatorClientError(
                    "Entitlement version changed after the operation was planned.",
                    error_code="required_entitlement_version_changed",
                )
            intended_valid_until = self._calculate_valid_until(
                operation,
                remote,
                exact_valid_until_utc=exact_valid_until_utc,
            )
            requested_limit = (
                exact_device_limit
                if exact_device_limit is not None
                else max(
                    remote.max_device_tokens,
                    operation.requested_device_limit or remote.max_device_tokens,
                )
            )
            requested_status = operation.requested_status or remote.status
            await repository.set_intent(
                operation,
                subscription_id=subscription.id,
                expected_version=remote.version,
                intended_valid_until_utc=intended_valid_until,
                requested_device_limit=requested_limit,
                requested_status=requested_status,
            )
            await repository.mark_request_sent(operation)
            await self._session.commit()

            try:
                result = await self._mediator_client.apply_entitlement_operation(
                    subscription.public_guid,
                    operation_id=operation.public_id,
                    operation_type=operation.operation_type,
                    expected_version=remote.version,
                    status=requested_status,
                    valid_until_utc=intended_valid_until.isoformat(),
                    max_device_tokens=requested_limit,
                )
            except MediatorClientError as exception:
                last_conflict = exception
                recovered = await self.recover_remote_result(operation)
                if recovered is not None:
                    return recovered
                if exception.error_code == "entitlement_operation_version_conflict":
                    await self._session.rollback()
                    operation = await EntitlementOperationRepository(
                        self._session
                    ).get_by_public_id(operation.public_id)
                    if operation is None:
                        raise RuntimeError("entitlement_operation_disappeared") from exception
                    operation.state = "claimed"
                    operation.last_error_code = exception.error_code
                    operation.updated_at_utc = utc_now()
                    await self._session.commit()
                    continue
                await self._session.rollback()
                operation = await EntitlementOperationRepository(self._session).get_by_public_id(
                    operation.public_id
                )
                if operation is not None:
                    await EntitlementOperationRepository(self._session).mark_failed_retriable(
                        operation,
                        exception.error_code or "mediator_outcome_unknown",
                    )
                    await self._session.commit()
                raise

            await self._record_remote_result(operation, result)
            await self._session.commit()
            return self._from_operation(operation, public_guid=subscription.public_guid)

        if last_conflict is not None:
            raise last_conflict
        raise MediatorClientError(
            "Entitlement operation could not converge.",
            error_code="entitlement_operation_conflict",
        )

    async def _record_remote_result(
        self,
        operation: EntitlementOperation,
        result: MediatorEntitlementOperationResult,
    ) -> None:
        await EntitlementOperationRepository(self._session).mark_external_applied(
            operation,
            result_version=result.result_version,
            result_status=result.result_status,
            result_valid_until_utc=self._parse_required(result.result_valid_until_utc),
            result_device_limit=result.result_max_device_tokens,
            applied_at_utc=self._parse_required(result.applied_at_utc),
        )

    @staticmethod
    def _calculate_valid_until(
        operation: EntitlementOperation,
        remote: MediatorEntitlementDetails,
        *,
        exact_valid_until_utc: datetime | None,
    ) -> datetime:
        if exact_valid_until_utc is not None:
            return to_aware_utc(exact_valid_until_utc)
        remote_valid_until = EntitlementOperationCoordinator._parse_required(remote.valid_until_utc)
        if operation.duration_delta_seconds <= 0:
            return remote_valid_until
        base = max(remote_valid_until, utc_now())
        return base + timedelta(seconds=operation.duration_delta_seconds)

    @staticmethod
    def _parse_required(value: str | datetime | None) -> datetime:
        if value is None:
            raise MediatorClientError(
                "Mediator operation result is missing a required timestamp.",
                error_code="invalid_response",
            )
        if isinstance(value, datetime):
            return to_aware_utc(value)
        return to_aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))

    @staticmethod
    def _from_operation(
        operation: EntitlementOperation,
        *,
        public_guid: str | None = None,
    ) -> AppliedEntitlement:
        if (
            operation.external_result_version is None
            or operation.external_result_status is None
            or operation.external_result_valid_until_utc is None
            or operation.external_result_device_limit is None
        ):
            raise MediatorClientError(
                "Durable entitlement operation has no complete external result.",
                error_code="operation_result_incomplete",
            )
        return AppliedEntitlement(
            public_guid=public_guid or operation.external_subscription_public_guid or "",
            version=operation.external_result_version,
            status=operation.external_result_status,
            valid_until_utc=to_aware_utc(operation.external_result_valid_until_utc),
            max_device_tokens=operation.external_result_device_limit,
            applied_at_utc=to_aware_utc(operation.external_applied_at_utc or utc_now()),
        )


class EntitlementRecoveryWorker:
    def __init__(
        self,
        session: AsyncSession,
        mediator_client: MediatorClient,
    ) -> None:
        self._session = session
        self._mediator_client = mediator_client

    async def classify(self, operation: EntitlementOperation) -> str:
        result = await self._mediator_client.get_entitlement_operation(operation.public_id)
        repository = EntitlementOperationRepository(self._session)
        if result is not None:
            await repository.mark_external_applied(
                operation,
                result_version=result.result_version,
                result_status=result.result_status,
                result_valid_until_utc=EntitlementOperationCoordinator._parse_required(
                    result.result_valid_until_utc
                ),
                result_device_limit=result.result_max_device_tokens,
                applied_at_utc=EntitlementOperationCoordinator._parse_required(
                    result.applied_at_utc
                ),
            )
            return "external_applied"
        if operation.state == "external_unknown":
            await repository.mark_failed_retriable(operation, "remote_operation_not_found")
            return "retryable"
        if operation.state == "claimed":
            await repository.mark_failed_retriable(
                operation, "claim_expired_before_external_result"
            )
            return "retryable"
        return operation.state

    async def classify_stale(self, limit: int = 100) -> dict[str, int]:
        counts: dict[str, int] = {}
        operations = await EntitlementOperationRepository(self._session).list_recoverable(limit)
        for operation in operations:
            try:
                state = await self.classify(operation)
            except MediatorClientError:
                state = "remote_unavailable"
            counts[state] = counts.get(state, 0) + 1
        await self._session.commit()
        return counts
