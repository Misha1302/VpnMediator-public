from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import ClassVar

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vpn_access_bot.config import Settings
from vpn_access_bot.models import (
    AcquisitionCampaign,
    AcquisitionTouch,
    CapacityStateTransition,
    CommercePolicy,
    CommercePolicyChangeRequest,
    NotificationOutbox,
    Order,
    PaymentInbox,
    RefundOperation,
    Subscription,
    UserAcquisition,
    WorkerHealth,
    utc_now,
)
from vpn_access_bot.repositories import to_aware_utc


class CommerceOperationKind(StrEnum):
    NEW_PURCHASE = "new_purchase"
    TRIAL = "trial"
    RENEWAL = "renewal"
    RESUME = "resume"
    UPGRADE_DEVICES = "upgrade_devices"
    EXTEND_AND_UPGRADE = "extend_and_upgrade"
    COMPLETE_PAID_ORDER = "complete_paid_order"
    RETRY_ACTIVATION = "retry_activation"
    REFUND_PREPARE = "refund_prepare"
    REFUND_COMPENSATION = "refund_compensation"
    ISSUE_EXISTING_FEED = "issue_existing_feed"


@dataclass(frozen=True)
class CapacitySnapshot:
    captured_at_utc: datetime
    active_subscriptions: int
    active_devices: int | None
    configured_subscription_capacity: int | None
    configured_device_capacity: int | None
    payment_inbox_pending: int
    payment_inbox_oldest_age_seconds: int | None
    activation_pending: int
    activation_oldest_age_seconds: int | None
    refund_pending: int
    refund_manual_review: int
    notification_backlog: int
    worker_stale_count: int
    utilization_percent: float | None
    state: str
    reason_code: str

    def to_public_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["captured_at_utc"] = self.captured_at_utc.isoformat()
        return payload


@dataclass(frozen=True)
class CommerceAdmissionDecision:
    allowed: bool
    reason_code: str
    operation_kind: CommerceOperationKind
    policy_version: int
    snapshot_at_utc: datetime
    mediator: object | None = None
    capacity: CapacitySnapshot | None = None
    facts: dict[str, object] | None = None

    @property
    def can_sell(self) -> bool:
        return self.allowed


class CommercePolicyChangeError(ValueError):
    pass


@dataclass(frozen=True)
class CommercePolicyChangePreview:
    public_id: str
    confirmation_token: str
    switch_name: str
    requested_enabled: bool
    expected_policy_version: int
    reason_code: str
    expires_at_utc: datetime


class CommercePolicyRepository:
    _SWITCHES: ClassVar[dict[str, str]] = {
        "new_purchases": "new_purchases_enabled",
        "trials": "trials_enabled",
        "renewals": "renewals_enabled",
        "resumes": "resumes_enabled",
        "device_upgrades": "device_upgrades_enabled",
        "extend_and_upgrade": "extend_and_upgrade_enabled",
        "referrals": "referrals_enabled",
        "campaign_tracking": "campaign_tracking_enabled",
        "capacity_enforcement": "capacity_enforcement_enabled",
    }

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self) -> CommercePolicy:
        policy = await self._session.get(CommercePolicy, 1)
        if policy is None:
            now = utc_now()
            policy = CommercePolicy(
                singleton_id=1,
                version=1,
                new_purchases_enabled=False,
                trials_enabled=False,
                renewals_enabled=True,
                resumes_enabled=False,
                device_upgrades_enabled=False,
                extend_and_upgrade_enabled=False,
                referrals_enabled=False,
                campaign_tracking_enabled=True,
                capacity_enforcement_enabled=False,
                reason_code="pre_advertising_freeze",
                updated_at_utc=now,
            )
            self._session.add(policy)
            await self._session.flush()
        if policy.expires_at_utc is not None and to_aware_utc(policy.expires_at_utc) <= utc_now():
            policy.expires_at_utc = None
            policy.reason_code = "expired_override_released"
            policy.version += 1
            policy.updated_at_utc = utc_now()
            await self._session.flush()
        return policy

    @classmethod
    def _attribute_for_switch(cls, switch_name: str) -> str:
        attribute = cls._SWITCHES.get(switch_name)
        if attribute is None:
            raise CommercePolicyChangeError("unknown_commerce_switch")
        return attribute

    async def set_switch(
        self,
        *,
        switch_name: str,
        enabled: bool,
        admin_telegram_id: int,
        reason_code: str,
        operator_note: str | None = None,
        expected_version: int | None = None,
    ) -> CommercePolicy:
        attribute = self._attribute_for_switch(switch_name)
        policy = await self.get()
        expected = policy.version if expected_version is None else expected_version
        now = utc_now()
        statement = (
            update(CommercePolicy)
            .where(
                CommercePolicy.singleton_id == 1,
                CommercePolicy.version == expected,
            )
            .values(
                **{
                    attribute: enabled,
                    "version": expected + 1,
                    "reason_code": reason_code[:64],
                    "operator_note": operator_note,
                    "updated_by_admin_telegram_id": admin_telegram_id,
                    "updated_at_utc": now,
                }
            )
        )
        result = await self._session.execute(statement)
        if result.rowcount != 1:
            raise CommercePolicyChangeError("commerce_policy_version_conflict")
        await self._session.flush()
        refreshed = await self._session.get(CommercePolicy, 1, populate_existing=True)
        if refreshed is None:
            raise CommercePolicyChangeError("commerce_policy_missing")
        return refreshed

    async def prepare_switch_change(
        self,
        *,
        switch_name: str,
        enabled: bool,
        admin_telegram_id: int,
        reason_code: str,
        operator_note: str | None = None,
        ttl_seconds: int = 300,
    ) -> CommercePolicyChangePreview:
        self._attribute_for_switch(switch_name)
        policy = await self.get()
        now = utc_now()
        await self._session.execute(
            update(CommercePolicyChangeRequest)
            .where(
                CommercePolicyChangeRequest.admin_telegram_id == admin_telegram_id,
                CommercePolicyChangeRequest.switch_name == switch_name,
                CommercePolicyChangeRequest.state == "pending",
            )
            .values(state="cancelled", failure_code="superseded")
        )
        token = secrets.token_urlsafe(18)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expires_at = now + timedelta(seconds=max(ttl_seconds, 30))
        request = CommercePolicyChangeRequest(
            public_id=secrets.token_hex(16),
            confirmation_token_hash=token_hash,
            admin_telegram_id=admin_telegram_id,
            switch_name=switch_name,
            requested_enabled=enabled,
            expected_policy_version=policy.version,
            reason_code=reason_code[:64],
            operator_note=operator_note,
            state="pending",
            expires_at_utc=expires_at,
            created_at_utc=now,
        )
        self._session.add(request)
        await self._session.flush()
        return CommercePolicyChangePreview(
            public_id=request.public_id,
            confirmation_token=token,
            switch_name=switch_name,
            requested_enabled=enabled,
            expected_policy_version=policy.version,
            reason_code=request.reason_code,
            expires_at_utc=expires_at,
        )

    async def confirm_switch_change(
        self,
        *,
        confirmation_token: str,
        admin_telegram_id: int,
    ) -> tuple[CommercePolicy, CommercePolicyChangeRequest]:
        token_hash = hashlib.sha256(confirmation_token.encode("utf-8")).hexdigest()
        result = await self._session.execute(
            select(CommercePolicyChangeRequest).where(
                CommercePolicyChangeRequest.confirmation_token_hash == token_hash
            )
        )
        request = result.scalar_one_or_none()
        if request is None or request.admin_telegram_id != admin_telegram_id:
            raise CommercePolicyChangeError("commerce_confirmation_invalid")
        if request.state != "pending":
            raise CommercePolicyChangeError("commerce_confirmation_already_used")
        now = utc_now()
        if to_aware_utc(request.expires_at_utc) <= now:
            request.state = "expired"
            request.failure_code = "confirmation_expired"
            await self._session.flush()
            raise CommercePolicyChangeError("commerce_confirmation_expired")
        try:
            policy = await self.set_switch(
                switch_name=request.switch_name,
                enabled=request.requested_enabled,
                admin_telegram_id=admin_telegram_id,
                reason_code=request.reason_code,
                operator_note=request.operator_note,
                expected_version=request.expected_policy_version,
            )
        except CommercePolicyChangeError as error:
            request.state = "stale"
            request.failure_code = str(error)
            await self._session.flush()
            raise
        request.state = "confirmed"
        request.confirmed_at_utc = now
        await self._session.flush()
        return policy, request


class CapacityService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def capture(self, mediator: object | None = None) -> CapacitySnapshot:
        now = utc_now()
        active_subscriptions = await self._count(
            select(func.count(Subscription.id)).where(Subscription.status == "active")
        )
        payment_pending = await self._count(
            select(func.count(PaymentInbox.id)).where(
                PaymentInbox.reconciliation_status.in_(["received", "matched", "failed"])
            )
        )
        activation_pending = await self._count(
            select(func.count(Order.id)).where(
                Order.status.in_(["payment_received", "activating", "activation_failed"])
            )
        )
        refund_pending = await self._count(
            select(func.count(RefundOperation.id)).where(
                RefundOperation.state.not_in(["completed", "manual_review"])
            )
        )
        refund_manual_review = await self._count(
            select(func.count(RefundOperation.id)).where(RefundOperation.state == "manual_review")
        )
        notification_backlog = await self._count(
            select(func.count(NotificationOutbox.id)).where(
                NotificationOutbox.state.in_(["pending", "failed", "sending"])
            )
        )
        payment_oldest = await self._oldest_age(
            select(func.min(PaymentInbox.received_at_utc)).where(
                PaymentInbox.reconciliation_status.in_(["received", "matched", "failed"])
            ),
            now,
        )
        activation_oldest = await self._oldest_age(
            select(func.min(Order.paid_at)).where(
                Order.status.in_(["payment_received", "activating", "activation_failed"]),
                Order.paid_at.is_not(None),
            ),
            now,
        )
        stale_before = now.timestamp() - self._settings.worker_stale_after_seconds
        worker_result = await self._session.execute(select(WorkerHealth))
        worker_stale_count = sum(
            1
            for worker in worker_result.scalars().all()
            if worker.last_success_at_utc is None
            or to_aware_utc(worker.last_success_at_utc).timestamp() < stale_before
        )

        active_devices = getattr(mediator, "active_devices", None)
        configured_subscription_capacity = (
            getattr(mediator, "configured_subscription_capacity", None)
            or self._settings.configured_subscription_capacity
            or None
        )
        configured_device_capacity = (
            getattr(mediator, "configured_device_capacity", None)
            or self._settings.configured_device_capacity
            or None
        )

        state = "healthy"
        reason = "within_capacity"
        ratios: list[float] = []
        if configured_subscription_capacity:
            ratios.append(active_subscriptions / configured_subscription_capacity)
        if configured_device_capacity and active_devices is not None:
            ratios.append(active_devices / configured_device_capacity)
        utilization_percent = max(ratios) * 100 if ratios else None
        if not ratios:
            state = "unknown"
            reason = "capacity_not_configured"
        else:
            utilization = max(ratios)
            if utilization >= self._settings.capacity_saturated_ratio:
                state = "saturated"
                reason = "capacity_high_watermark"
            elif utilization >= self._settings.capacity_constrained_ratio:
                state = "constrained"
                reason = "capacity_constrained"
        if (
            payment_oldest is not None
            and payment_oldest >= self._settings.capacity_backlog_slo_seconds
        ):
            state = "saturated"
            reason = "payment_backlog_slo_exceeded"
        if (
            activation_oldest is not None
            and activation_oldest >= self._settings.capacity_backlog_slo_seconds
        ):
            state = "saturated"
            reason = "activation_backlog_slo_exceeded"
        if notification_backlog >= self._settings.capacity_notification_backlog_limit:
            state = "saturated"
            reason = "notification_backlog_limit_exceeded"
        if refund_manual_review >= self._settings.capacity_refund_manual_review_limit:
            state = "saturated"
            reason = "refund_manual_review_limit_exceeded"
        if worker_stale_count >= self._settings.capacity_worker_stale_limit:
            state = "saturated"
            reason = "critical_worker_stale"

        latest_result = await self._session.execute(
            select(CapacityStateTransition)
            .order_by(CapacityStateTransition.captured_at_utc.desc())
            .limit(1)
        )
        latest = latest_result.scalar_one_or_none()
        if (
            state == "healthy"
            and latest is not None
            and latest.new_state
            in {
                "constrained",
                "saturated",
            }
        ):
            elapsed = (now - to_aware_utc(latest.captured_at_utc)).total_seconds()
            below_recovery = bool(ratios) and max(ratios) < self._settings.capacity_recovery_ratio
            if elapsed < self._settings.capacity_min_dwell_seconds or not below_recovery:
                state = latest.new_state
                reason = "capacity_hysteresis_hold"

        snapshot = CapacitySnapshot(
            captured_at_utc=now,
            active_subscriptions=active_subscriptions,
            active_devices=active_devices,
            configured_subscription_capacity=configured_subscription_capacity,
            configured_device_capacity=configured_device_capacity,
            payment_inbox_pending=payment_pending,
            payment_inbox_oldest_age_seconds=payment_oldest,
            activation_pending=activation_pending,
            activation_oldest_age_seconds=activation_oldest,
            refund_pending=refund_pending,
            refund_manual_review=refund_manual_review,
            notification_backlog=notification_backlog,
            worker_stale_count=worker_stale_count,
            utilization_percent=utilization_percent,
            state=state,
            reason_code=reason,
        )
        if latest is None or latest.new_state != state:
            transition = CapacityStateTransition(
                previous_state=latest.new_state if latest is not None else None,
                new_state=state,
                reason_code=reason,
                snapshot_json=serialize_snapshot(snapshot),
                captured_at_utc=now,
            )
            self._session.add(transition)
            await self._session.flush()
        return snapshot

    async def _count(self, statement) -> int:
        result = await self._session.execute(statement)
        return int(result.scalar_one() or 0)

    async def _oldest_age(self, statement, now: datetime) -> int | None:
        result = await self._session.execute(statement)
        value = result.scalar_one_or_none()
        if value is None:
            return None
        return max(int((now - to_aware_utc(value)).total_seconds()), 0)


class AcquisitionService:
    CAMPAIGN_PREFIX = "c_"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_campaign(
        self,
        *,
        channel: str,
        placement: str | None = None,
        creative: str | None = None,
        landing_variant: str | None = None,
        currency: str = "RUB",
        planned_spend_minor_units: int = 0,
    ) -> AcquisitionCampaign:
        now = utc_now()
        campaign = AcquisitionCampaign(
            public_token=secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16],
            channel=channel[:64],
            placement=placement[:128] if placement else None,
            creative=creative[:128] if creative else None,
            landing_variant=landing_variant[:64] if landing_variant else None,
            status="active",
            planned_spend_minor_units=max(planned_spend_minor_units, 0),
            actual_spend_minor_units=0,
            currency=currency.upper()[:8],
            created_at_utc=now,
            updated_at_utc=now,
        )
        self._session.add(campaign)
        await self._session.flush()
        return campaign

    async def record_start(
        self,
        *,
        user_id: int,
        payload: str | None,
        bot_key: str | None,
    ) -> AcquisitionCampaign | None:
        if not payload or not payload.startswith(self.CAMPAIGN_PREFIX):
            return None
        token = payload.removeprefix(self.CAMPAIGN_PREFIX)
        if not token or len(token) > 48:
            return None
        result = await self._session.execute(
            select(AcquisitionCampaign).where(
                AcquisitionCampaign.public_token == token,
                AcquisitionCampaign.status == "active",
            )
        )
        campaign = result.scalar_one_or_none()
        if campaign is None:
            return None
        now = utc_now()
        if campaign.starts_at_utc is not None and to_aware_utc(campaign.starts_at_utc) > now:
            return None
        if campaign.ends_at_utc is not None and to_aware_utc(campaign.ends_at_utc) <= now:
            return None
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        acquisition = await self._session.get(UserAcquisition, user_id)
        if acquisition is None:
            acquisition = UserAcquisition(
                user_id=user_id,
                first_campaign_id=campaign.id,
                first_touch_at_utc=now,
                first_bot_key=bot_key,
                first_start_payload_hash=payload_hash,
                last_campaign_id=campaign.id,
                last_touch_at_utc=now,
                last_bot_key=bot_key,
            )
            self._session.add(acquisition)
        else:
            acquisition.last_campaign_id = campaign.id
            acquisition.last_touch_at_utc = now
            acquisition.last_bot_key = bot_key
        existing = await self._session.execute(
            select(AcquisitionTouch.id).where(
                AcquisitionTouch.user_id == user_id,
                AcquisitionTouch.campaign_id == campaign.id,
                AcquisitionTouch.touch_kind == "start",
                AcquisitionTouch.payload_hash == payload_hash,
            )
        )
        if existing.scalar_one_or_none() is None:
            self._session.add(
                AcquisitionTouch(
                    user_id=user_id,
                    campaign_id=campaign.id,
                    touched_at_utc=now,
                    bot_key=bot_key,
                    touch_kind="start",
                    payload_hash=payload_hash,
                )
            )
            # Make the idempotency row visible to a repeated /start within the same
            # transaction instead of relying only on the database unique constraint.
            await self._session.flush()
        return campaign

    @staticmethod
    def campaign_deep_link(bot_username: str, campaign: AcquisitionCampaign) -> str:
        normalized = bot_username.strip().lstrip("@")
        return f"https://t.me/{normalized}?start=c_{campaign.public_token}"


def serialize_snapshot(snapshot: CapacitySnapshot) -> str:
    return json.dumps(snapshot.to_public_dict(), ensure_ascii=False, sort_keys=True)
