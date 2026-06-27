from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vpn_access_bot.config import Settings
from vpn_access_bot.constants import (
    ENTITLEMENT_STATUS_ACTIVE,
    ENTITLEMENT_STATUS_DISABLED,
    ENTITLEMENT_STATUS_EXPIRED,
    ORDER_STATUS_ACTIVATING,
    ORDER_STATUS_ACTIVATION_FAILED,
    ORDER_STATUS_CANCELLED,
    ORDER_STATUS_EXPIRED,
    ORDER_STATUS_PAID,
    ORDER_STATUS_PENDING,
    SUBSCRIPTION_STATUS_ACTIVE,
    SUBSCRIPTION_STATUS_DISABLED,
    SUPPORT_STATUS_CLOSED,
)
from vpn_access_bot.entitlement_lifecycle import (
    is_expiration_transition_pending,
    is_legacy_expiration_drift,
    lifecycle_matches_authoritative_entitlement,
)
from vpn_access_bot.formatting import escape_html
from vpn_access_bot.mediator_client import (
    MediatorClient,
    MediatorClientError,
    MediatorEntitlementDetails,
)
from vpn_access_bot.models import (
    AccessEntitlement,
    AuditEvent,
    BroadcastCampaign,
    CommercialEntitlementSegment,
    NotificationOutbox,
    OnboardingSession,
    Order,
    PaymentInbox,
    ReferralReward,
    Subscription,
    SupportRequest,
    TrialClaim,
    User,
    WorkerHealth,
    utc_now,
)
from vpn_access_bot.operations import EntitlementOperationCoordinator, EntitlementRecoveryWorker
from vpn_access_bot.readiness import readiness_failure_reason
from vpn_access_bot.repositories import (
    AccessOperationLeaseRepository,
    CommercialEntitlementAdjustmentRepository,
    CommercialEntitlementSegmentRepository,
    DiscountRedemptionRepository,
    EntitlementOperationRepository,
    EntitlementRepository,
    NotificationDeliveryRepository,
    NotificationOutboxRepository,
    OnboardingSessionRepository,
    OrderApplicationRepository,
    OrderRepository,
    PaymentInboxRepository,
    ProductEventRepository,
    ReferralRewardRepository,
    RefundOperationRepository,
    SubscriptionRepository,
    UserRepository,
    to_aware_utc,
)
from vpn_access_bot.telegram.notification_sender import NotificationRecipientUnavailable

logger = logging.getLogger(__name__)

PAID_SEGMENT_SOURCE = "paid_order"
REFERRAL_SEGMENT_SOURCE = "referral_reward"
TRIAL_SEGMENT_SOURCE = "trial"


async def user_has_paid_history(
    session: AsyncSession,
    user_id: int,
    *,
    after_utc: datetime | None = None,
) -> bool:
    conditions = [
        Order.user_id == user_id,
        Order.is_test_order.is_(False),
        Order.amount_minor_units > 0,
        (
            Order.paid_at.is_not(None)
            | Order.provider_payment_id.is_not(None)
            | (Order.status == ORDER_STATUS_PAID)
        ),
    ]
    if after_utc is not None:
        conditions.append(func.coalesce(Order.paid_at, Order.created_at) > after_utc)

    result = await session.execute(select(func.count(Order.id)).where(*conditions))
    return int(result.scalar_one()) > 0


async def bind_referrer_from_payload(
    session: AsyncSession,
    user: User,
    payload: str | None,
) -> bool:
    if not payload or not payload.startswith("ref_"):
        return False

    code = payload.removeprefix("ref_").strip().lower()

    if not code or user.referred_by_user_id is not None or user.referral_blocked:
        return False

    result = await session.execute(select(User).where(User.referral_code == code))
    referrer = result.scalar_one_or_none()

    if referrer is None or referrer.id == user.id or referrer.referral_blocked:
        return False

    if await user_has_paid_history(session, user.id):
        return False

    current: User | None = referrer

    for _ in range(32):
        if current is None or current.referred_by_user_id is None:
            break

        if current.referred_by_user_id == user.id:
            return False

        current = await UserRepository(session).get_by_id(current.referred_by_user_id)
    else:
        return False

    user.referred_by_user_id = referrer.id
    user.referred_at_utc = utc_now()
    user.updated_at = utc_now()
    return True


async def reserve_discount_for_order(session: AsyncSession, order: Order) -> None:
    await DiscountRedemptionRepository(session).reserve_for_order(order)


async def apply_discount_for_order(session: AsyncSession, order_id: int) -> None:
    await DiscountRedemptionRepository(session).apply_for_order(order_id)


async def release_discount_for_order(session: AsyncSession, order_id: int) -> None:
    await DiscountRedemptionRepository(session).release_for_order(order_id)


async def record_entitlement_segment_once(
    session: AsyncSession,
    *,
    subscription_id: int,
    source_kind: str,
    starts_at_utc: datetime,
    ends_at_utc: datetime,
    idempotency_key: str,
    source_order_id: int | None = None,
    source_entity_id: str | None = None,
) -> CommercialEntitlementSegment:
    return await CommercialEntitlementSegmentRepository(session).create_applied_once(
        subscription_id=subscription_id,
        source_kind=source_kind,
        starts_at_utc=starts_at_utc,
        ends_at_utc=ends_at_utc,
        idempotency_key=idempotency_key,
        source_order_id=source_order_id,
        source_entity_id=source_entity_id,
    )


async def remaining_paid_seconds(
    session: AsyncSession,
    subscription: Subscription,
    now: datetime | None = None,
) -> int:
    return await CommercialEntitlementSegmentRepository(session).remaining_paid_seconds(
        subscription,
        now,
    )


async def expire_pending_orders_once(session: AsyncSession) -> int:
    now = utc_now()
    result = await session.execute(
        update(Order)
        .where(
            Order.status == ORDER_STATUS_PENDING,
            Order.expires_at_utc.is_not(None),
            Order.expires_at_utc <= now,
            (
                Order.checkout_authorized_until_utc.is_(None)
                | (Order.checkout_authorized_until_utc <= now)
            ),
        )
        .values(status=ORDER_STATUS_EXPIRED, cancelled_at_utc=now)
        .returning(Order.id)
    )
    expired_order_ids = [int(order_id) for order_id in result.scalars().all()]

    for order_id in expired_order_ids:
        await release_discount_for_order(session, order_id)

    return len(expired_order_ids)


async def cancel_latest_pending_order(
    session: AsyncSession,
    user_id: int,
    public_order_id: str | None = None,
) -> Order | None:
    query = select(Order).where(
        Order.user_id == user_id,
        Order.status == ORDER_STATUS_PENDING,
    )
    if public_order_id is not None:
        query = query.where(Order.public_order_id == public_order_id)
    result = await session.execute(
        query.order_by(Order.created_at.desc(), Order.id.desc()).limit(1)
    )
    order = result.scalar_one_or_none()

    if order is None:
        return None

    order.status = ORDER_STATUS_CANCELLED
    order.cancelled_at_utc = utc_now()
    await release_discount_for_order(session, order.id)
    return order


async def close_support_request(
    session: AsyncSession,
    public_id_prefix: str,
    admin_telegram_id: int,
) -> SupportRequest | None:
    result = await session.execute(
        select(SupportRequest).where(
            SupportRequest.public_id.like(f"{public_id_prefix}%"),
            SupportRequest.status != SUPPORT_STATUS_CLOSED,
        )
    )
    requests = list(result.scalars().all())

    if len(requests) != 1:
        return None

    request = requests[0]
    request.status = SUPPORT_STATUS_CLOSED
    request.closed_at_utc = utc_now()
    request.closed_by_admin_telegram_id = admin_telegram_id
    request.updated_at_utc = utc_now()
    return request


async def _mark_worker_attempt(
    session: AsyncSession,
    worker_name: str,
    *,
    success: bool,
    error_code: str | None = None,
) -> None:
    health = await session.get(WorkerHealth, worker_name)

    if health is None:
        health = WorkerHealth(worker_name=worker_name)
        session.add(health)

    now = utc_now()
    health.last_attempt_at_utc = now

    if success:
        health.last_success_at_utc = now
        health.last_error_code = None
    else:
        health.last_failure_at_utc = now
        health.last_error_code = error_code or "worker_failed"


async def process_referral_rewards_once(
    session_factory,
    mediator_client: MediatorClient,
) -> int:
    now = utc_now()
    async with session_factory() as session:
        due_result = await session.execute(
            select(ReferralReward)
            .where(
                ReferralReward.status == "pending_hold",
                ReferralReward.available_at_utc <= now,
            )
            .order_by(ReferralReward.available_at_utc, ReferralReward.id)
            .limit(100)
        )
        for reward in due_result.scalars().all():
            source_order = await OrderRepository(session).get_by_id(reward.source_order_id)
            application = (
                await OrderApplicationRepository(session).get_for_order(reward.source_order_id)
                if source_order is not None
                else None
            )
            if (
                source_order is not None
                and source_order.status == ORDER_STATUS_PAID
                and application
            ):
                reward.status = "available"
                reward.failure_code = None
            else:
                reward.status = "cancelled"
                reward.cancelled_at_utc = now
                reward.failure_code = "source_order_not_economically_valid"
        result = await session.execute(
            select(ReferralReward.id)
            .where(ReferralReward.status.in_(["available", "failed", "applying"]))
            .order_by(ReferralReward.available_at_utc, ReferralReward.id)
            .limit(50)
        )
        reward_ids = list(result.scalars().all())

    applied_count = 0
    for reward_id in reward_ids:
        owner_key = f"referral:{reward_id}"
        user_id: int | None = None
        operation_public_id: str | None = None
        try:
            async with session_factory() as session:
                claim = await session.execute(
                    update(ReferralReward)
                    .where(
                        ReferralReward.id == reward_id,
                        ReferralReward.status.in_(["available", "failed", "applying"]),
                    )
                    .values(status="applying", failure_code=None)
                )
                if claim.rowcount != 1:
                    continue
                reward = await session.get(ReferralReward, reward_id)
                if reward is None:
                    continue
                source_order = await OrderRepository(session).get_by_id(reward.source_order_id)
                source_application = await OrderApplicationRepository(session).get_for_order(
                    reward.source_order_id
                )
                if (
                    source_order is None
                    or source_order.status != ORDER_STATUS_PAID
                    or source_application is None
                ):
                    reward.status = (
                        "reversal_required"
                        if reward.entitlement_version is not None
                        else "cancelled"
                    )
                    reward.cancelled_at_utc = utc_now()
                    reward.failure_code = "source_order_not_economically_valid"
                    continue
                user_id = reward.referrer_user_id
                referrer = await UserRepository(session).get_by_id(user_id)
                if referrer is None or referrer.referral_blocked:
                    reward.status = "cancelled"
                    reward.cancelled_at_utc = utc_now()
                    reward.failure_code = "referrer_unavailable"
                    continue
                subscription = await SubscriptionRepository(session).get_primary_for_user(referrer)
                if subscription is None:
                    reward.status = "available"
                    continue
                if (
                    subscription.status == SUBSCRIPTION_STATUS_DISABLED
                    or subscription.reconciliation_state == "blocked"
                ):
                    reward.status = "available"
                    reward.failure_code = (
                        "reconciliation_blocked"
                        if subscription.reconciliation_state == "blocked"
                        else "subscription_disabled"
                    )
                    continue
                entitlement = await EntitlementRepository(session).get_for_subscription(
                    subscription.id
                )
                if entitlement is None:
                    reward.status = "failed"
                    reward.failure_code = "entitlement_missing"
                    continue
                if not await AccessOperationLeaseRepository(session).acquire(
                    user_id=user_id,
                    owner_kind="referral",
                    owner_key=owner_key,
                ):
                    reward.status = "available"
                    reward.failure_code = "access_operation_in_progress"
                    continue
                reward.target_subscription_id = subscription.id
                reward.previous_entitlement_version = entitlement.version
                reward.previous_status = subscription.status
                reward.previous_valid_until_utc = subscription.expires_at
                reward.previous_max_devices = subscription.max_devices
                operation = await EntitlementOperationCoordinator(
                    session, mediator_client
                ).prepare_generic(
                    user_id=user_id,
                    subscription_id=subscription.id,
                    operation_type="referral_adjustment",
                    source_entity_type="referral_reward",
                    source_entity_id=str(reward.id),
                    duration_delta_seconds=reward.reward_duration_seconds,
                    requested_device_limit=subscription.max_devices,
                    requested_status=ENTITLEMENT_STATUS_ACTIVE,
                    observed_valid_until_utc=subscription.expires_at,
                )
                operation_public_id = operation.public_id
                subscription_id = subscription.id
                await session.commit()

                reward = await session.get(ReferralReward, reward_id)
                subscription = await SubscriptionRepository(session).get_by_id(subscription_id)
                operation = await EntitlementOperationRepository(session).get_by_public_id(
                    operation_public_id
                )
                source_order = await OrderRepository(session).get_by_id(
                    reward.source_order_id if reward is not None else -1
                )
                if reward is None or subscription is None or operation is None:
                    raise RuntimeError("referral_operation_state_missing")
                if source_order is None or source_order.status != ORDER_STATUS_PAID:
                    reward.status = "cancelled"
                    reward.cancelled_at_utc = utc_now()
                    reward.failure_code = "source_order_changed_before_application"
                    await AccessOperationLeaseRepository(session).release(
                        user_id=user_id, owner_key=owner_key
                    )
                    continue
                previous_expiry = to_aware_utc(subscription.expires_at)
                previous_device_limit = subscription.max_devices
                applied = await EntitlementOperationCoordinator(
                    session, mediator_client
                ).apply_generic(
                    operation,
                    subscription,
                    required_current_version=reward.previous_entitlement_version,
                )
                if applied is None:
                    raise RuntimeError("referral_operation_superseded")
                await SubscriptionRepository(session).extend(
                    subscription=subscription,
                    tariff=None,
                    new_expires_at=applied.valid_until_utc,
                    max_devices=applied.max_device_tokens,
                )
                await EntitlementRepository(session).set_authoritative(
                    subscription=subscription,
                    version=applied.version,
                    status=applied.status,
                    valid_until_utc=applied.valid_until_utc,
                    max_device_tokens=applied.max_device_tokens,
                )
                reward.status = "applied"
                reward.applied_at_utc = applied.applied_at_utc
                reward.entitlement_version = applied.version
                await CommercialEntitlementAdjustmentRepository(session).create_applied_once(
                    subscription=subscription,
                    source_kind=REFERRAL_SEGMENT_SOURCE,
                    duration_delta_seconds=reward.reward_duration_seconds,
                    device_limit_before=previous_device_limit,
                    device_limit_after=applied.max_device_tokens,
                    idempotency_key=reward.idempotency_key,
                    source_entity_id=str(reward.id),
                )
                await record_entitlement_segment_once(
                    session,
                    subscription_id=subscription.id,
                    source_kind=REFERRAL_SEGMENT_SOURCE,
                    starts_at_utc=max(previous_expiry, utc_now()),
                    ends_at_utc=applied.valid_until_utc,
                    idempotency_key=reward.idempotency_key,
                    source_entity_id=str(reward.id),
                )
                await NotificationOutboxRepository(session).enqueue_once(
                    idempotency_key=f"referral-applied:{reward.id}",
                    notification_kind="referral_reward_applied",
                    user_id=user_id,
                    subscription_id=subscription.id,
                    payload={"notification_key": f"referral:{reward.id}"},
                )
                await EntitlementOperationRepository(session).mark_completed(operation)
                await AccessOperationLeaseRepository(session).release(
                    user_id=user_id, owner_key=owner_key
                )
                applied_count += 1
        except MediatorClientError as exception:
            async with session_factory() as session:
                reward = await session.get(ReferralReward, reward_id)
                operation = (
                    await EntitlementOperationRepository(session).get_by_public_id(
                        operation_public_id
                    )
                    if operation_public_id is not None
                    else None
                )
                if reward is not None and (
                    operation is None
                    or operation.state not in {"external_unknown", "external_applied"}
                ):
                    reward.status = "failed"
                    reward.failure_code = exception.error_code or "mediator_unavailable"
                if user_id is not None:
                    await AccessOperationLeaseRepository(session).release(
                        user_id=user_id, owner_key=owner_key
                    )
        except Exception:
            logger.exception("Referral entitlement operation failed: reward_id=%s", reward_id)
            async with session_factory() as session:
                reward = await session.get(ReferralReward, reward_id)
                if reward is not None:
                    reward.status = "failed"
                    reward.failure_code = "referral_finalize_failed"
                if user_id is not None:
                    await AccessOperationLeaseRepository(session).release(
                        user_id=user_id, owner_key=owner_key
                    )

    async with session_factory() as session:
        reversal_rewards = await ReferralRewardRepository(session).list_reversal_required()
        reversal_ids = [reward.id for reward in reversal_rewards]

    for reward_id in reversal_ids:
        owner_key = f"referral-reversal:{reward_id}"
        user_id: int | None = None
        operation_public_id: str | None = None
        try:
            async with session_factory() as session:
                claim = await session.execute(
                    update(ReferralReward)
                    .where(
                        ReferralReward.id == reward_id,
                        ReferralReward.status.in_(["reversal_required", "reversal_failed"]),
                    )
                    .values(status="reversing", failure_code=None)
                )
                if claim.rowcount != 1:
                    continue
                reward = await session.get(ReferralReward, reward_id)
                if reward is None or reward.target_subscription_id is None:
                    continue
                user_id = reward.referrer_user_id
                subscription = await SubscriptionRepository(session).get_by_id(
                    reward.target_subscription_id
                )
                if subscription is None:
                    reward.status = "reversal_failed"
                    reward.failure_code = "reversal_subscription_missing"
                    continue
                if not await AccessOperationLeaseRepository(session).acquire(
                    user_id=user_id,
                    owner_kind="referral_reversal",
                    owner_key=owner_key,
                ):
                    reward.status = "reversal_required"
                    reward.failure_code = "access_operation_in_progress"
                    continue
                segment_repository = CommercialEntitlementSegmentRepository(session)
                remaining_seconds = await segment_repository.remaining_seconds_for_source_entity(
                    REFERRAL_SEGMENT_SOURCE,
                    str(reward.id),
                )
                if remaining_seconds <= 0:
                    await segment_repository.mark_reversed_for_source_entity(
                        REFERRAL_SEGMENT_SOURCE, str(reward.id)
                    )
                    await CommercialEntitlementAdjustmentRepository(
                        session
                    ).mark_reversed_for_source_entity(REFERRAL_SEGMENT_SOURCE, str(reward.id))
                    reward.status = "reversed"
                    reward.reversed_at_utc = utc_now()
                    await AccessOperationLeaseRepository(session).release(
                        user_id=user_id, owner_key=owner_key
                    )
                    continue
                entitlement = await EntitlementRepository(session).get_for_subscription(
                    subscription.id
                )
                if entitlement is None:
                    reward.status = "reversal_failed"
                    reward.failure_code = "reversal_entitlement_missing"
                    continue
                target_valid_until = to_aware_utc(subscription.expires_at) - timedelta(
                    seconds=remaining_seconds
                )
                target_status = (
                    ENTITLEMENT_STATUS_ACTIVE
                    if target_valid_until > utc_now()
                    else ENTITLEMENT_STATUS_EXPIRED
                )
                operation = await EntitlementOperationCoordinator(
                    session, mediator_client
                ).prepare_generic(
                    user_id=user_id,
                    subscription_id=subscription.id,
                    operation_type="referral_reversal",
                    source_entity_type="referral_reward_reversal",
                    source_entity_id=str(reward.id),
                    duration_delta_seconds=0,
                    requested_device_limit=subscription.max_devices,
                    requested_status=target_status,
                    observed_valid_until_utc=subscription.expires_at,
                )
                reward.reversal_operation_id = operation.id
                operation_public_id = operation.public_id
                current_version = entitlement.version
                subscription_id = subscription.id
                await session.commit()

                reward = await session.get(ReferralReward, reward_id)
                subscription = await SubscriptionRepository(session).get_by_id(subscription_id)
                operation = await EntitlementOperationRepository(session).get_by_public_id(
                    operation_public_id
                )
                if reward is None or subscription is None or operation is None:
                    raise RuntimeError("referral_reversal_state_missing")
                applied = await EntitlementOperationCoordinator(
                    session, mediator_client
                ).apply_generic(
                    operation,
                    subscription,
                    exact_valid_until_utc=target_valid_until,
                    exact_device_limit=subscription.max_devices,
                    required_current_version=current_version,
                )
                if applied is None:
                    raise RuntimeError("referral_reversal_superseded")
                subscription.expires_at = applied.valid_until_utc
                subscription.max_devices = applied.max_device_tokens
                subscription.status = target_status
                subscription.updated_at_utc = utc_now()
                await EntitlementRepository(session).set_authoritative(
                    subscription,
                    version=applied.version,
                    status=applied.status,
                    valid_until_utc=applied.valid_until_utc,
                    max_device_tokens=applied.max_device_tokens,
                )
                await CommercialEntitlementSegmentRepository(
                    session
                ).mark_reversed_for_source_entity(REFERRAL_SEGMENT_SOURCE, str(reward.id))
                await CommercialEntitlementAdjustmentRepository(
                    session
                ).mark_reversed_for_source_entity(REFERRAL_SEGMENT_SOURCE, str(reward.id))
                reward.status = "reversed"
                reward.reversed_at_utc = utc_now()
                reward.failure_code = None
                await EntitlementOperationRepository(session).mark_completed(operation)
                await AccessOperationLeaseRepository(session).release(
                    user_id=user_id, owner_key=owner_key
                )
        except Exception:
            logger.exception("Referral reversal failed: reward_id=%s", reward_id)
            async with session_factory() as session:
                reward = await session.get(ReferralReward, reward_id)
                if reward is not None:
                    reward.status = "reversal_failed"
                    reward.failure_code = "referral_reversal_failed"
                if user_id is not None:
                    await AccessOperationLeaseRepository(session).release(
                        user_id=user_id, owner_key=owner_key
                    )

    async with session_factory() as session:
        await _mark_worker_attempt(session, "referral_rewards", success=True)
    return applied_count


def _parse_remote_entitlement_snapshot(
    remote: MediatorEntitlementDetails,
    *,
    expected_public_guid: str,
) -> datetime:
    if remote.public_guid != expected_public_guid:
        raise ValueError("public_guid_mismatch")
    if remote.version < 1:
        raise ValueError("invalid_version")
    if remote.max_device_tokens < 1:
        raise ValueError("invalid_device_limit")
    if remote.status not in {
        ENTITLEMENT_STATUS_ACTIVE,
        ENTITLEMENT_STATUS_DISABLED,
        ENTITLEMENT_STATUS_EXPIRED,
    }:
        raise ValueError("invalid_status")
    if not remote.valid_until_utc:
        raise ValueError("missing_valid_until")

    parsed = datetime.fromisoformat(remote.valid_until_utc.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("naive_valid_until")
    return to_aware_utc(parsed)


async def _record_reconciliation_read_failure(
    session_factory,
    *,
    subscription_id: int,
    public_guid: str,
    error_code: str,
    quarantine: bool,
) -> None:
    async with session_factory() as session:
        subscription = await SubscriptionRepository(session).get_by_id(subscription_id)
        if subscription is None:
            return
        now = utc_now()
        if quarantine:
            subscription.reconciliation_state = "blocked"
            subscription.reconciliation_reason = "invalid_remote_entitlement"
            subscription.reconciliation_blocked_at_utc = now
            await NotificationOutboxRepository(session).enqueue_once(
                idempotency_key=f"reconciliation-invalid-remote:{subscription_id}",
                notification_kind="operator_reconciliation_blocked",
                user_id=subscription.user_id,
                subscription_id=subscription.id,
                payload={
                    "reason_code": "invalid_remote_entitlement",
                    "public_guid": public_guid,
                    "suggested_action": "reconcile_status",
                },
            )
        session.add(
            AuditEvent(
                created_at_utc=now,
                event_type="entitlement.reconciliation_failed",
                subscription_id=subscription_id,
                public_guid=public_guid,
                error_code=error_code,
            )
        )


async def reconcile_entitlements_once(
    session_factory,
    mediator_client: MediatorClient,
) -> int:
    async with session_factory() as session:
        result = await session.execute(
            select(Subscription.id, Subscription.public_guid)
            .where(Subscription.test_reset_at_utc.is_(None))
            .order_by(Subscription.id)
        )
        snapshots = list(result.all())

    synchronized = 0
    for subscription_id, public_guid in snapshots:
        try:
            remote = await mediator_client.get_entitlement(public_guid)
            remote_until = _parse_remote_entitlement_snapshot(
                remote,
                expected_public_guid=public_guid,
            )
        except MediatorClientError as exception:
            error_code = exception.error_code or "mediator_unavailable"
            await _record_reconciliation_read_failure(
                session_factory,
                subscription_id=subscription_id,
                public_guid=public_guid,
                error_code=error_code,
                quarantine=error_code == "invalid_response",
            )
            continue
        except (TypeError, ValueError):
            await _record_reconciliation_read_failure(
                session_factory,
                subscription_id=subscription_id,
                public_guid=public_guid,
                error_code="invalid_remote_entitlement",
                quarantine=True,
            )
            continue

        async with session_factory() as session:
            subscription = await SubscriptionRepository(session).get_by_id(subscription_id)
            if subscription is None:
                continue

            entitlement_repository = EntitlementRepository(session)
            local = await entitlement_repository.get_for_subscription(subscription_id)
            if local is None:
                local = await entitlement_repository.set_authoritative(
                    subscription,
                    version=remote.version,
                    status=remote.status,
                    valid_until_utc=remote_until,
                    max_device_tokens=remote.max_device_tokens,
                )
                session.add(
                    AuditEvent(
                        created_at_utc=utc_now(),
                        event_type="entitlement.reconciliation_mirror_backfilled",
                        subscription_id=subscription_id,
                        public_guid=public_guid,
                        error_code=None,
                    )
                )

            now = utc_now()
            local_until = to_aware_utc(local.valid_until_utc)
            same_payload = (
                local.version == remote.version
                and local.status == remote.status
                and remote_until == local_until
                and local.max_device_tokens == remote.max_device_tokens
            )
            subscription_payload_consistent = (
                to_aware_utc(subscription.expires_at) == local_until
                and subscription.max_devices == local.max_device_tokens
            )
            lifecycle_consistent = (
                subscription_payload_consistent
                and lifecycle_matches_authoritative_entitlement(
                    subscription_status=subscription.status,
                    entitlement_status=local.status,
                    valid_until_utc=local_until,
                    now_utc=now,
                )
            )
            expiration_pending = same_payload and is_expiration_transition_pending(
                subscription_status=subscription.status,
                subscription_expires_at_utc=subscription.expires_at,
                local_status=local.status,
                local_valid_until_utc=local_until,
                remote_status=remote.status,
                remote_valid_until_utc=remote_until,
                now_utc=now,
            )
            if same_payload and (lifecycle_consistent or expiration_pending):
                subscription.reconciliation_state = "healthy"
                subscription.reconciliation_reason = None
                subscription.reconciliation_blocked_at_utc = None
                synchronized += 1
                continue

            operation_repository = EntitlementOperationRepository(session)
            known_operation = await operation_repository.find_matching_unfinished_result(
                subscription_id=subscription_id,
                version=remote.version,
                status=remote.status,
                valid_until_utc=remote_until,
                max_device_tokens=remote.max_device_tokens,
            )
            if known_operation is not None:
                subscription.reconciliation_state = "recovering"
                subscription.reconciliation_reason = "known_operation_pending_local_commit"
                subscription.reconciliation_blocked_at_utc = None
                await NotificationOutboxRepository(session).enqueue_once(
                    idempotency_key=(
                        f"reconciliation-recovery:{subscription_id}:{known_operation.public_id}"
                    ),
                    notification_kind="operator_reconciliation_recovery",
                    user_id=subscription.user_id,
                    subscription_id=subscription_id,
                    payload={
                        "operation_public_id": known_operation.public_id,
                        "reason_code": "known_operation_pending_local_commit",
                    },
                )
                continue

            if same_payload:
                reason = "lifecycle_payload_mismatch"
            elif is_legacy_expiration_drift(
                subscription_status=subscription.status,
                subscription_expires_at_utc=subscription.expires_at,
                local_status=local.status,
                local_version=local.version,
                local_valid_until_utc=local_until,
                local_max_device_tokens=local.max_device_tokens,
                remote_status=remote.status,
                remote_version=remote.version,
                remote_valid_until_utc=remote_until,
                remote_max_device_tokens=remote.max_device_tokens,
                now_utc=now,
            ):
                reason = "legacy_expiration_drift"
            elif remote.version > local.version:
                reason = "remote_newer_unknown_origin"
            elif local.version > remote.version:
                reason = "local_newer_without_operation"
            else:
                reason = "same_version_payload_mismatch"

            subscription.reconciliation_state = "blocked"
            subscription.reconciliation_reason = reason
            subscription.reconciliation_blocked_at_utc = now
            session.add(
                AuditEvent(
                    created_at_utc=now,
                    event_type="entitlement.reconciliation_quarantined",
                    subscription_id=subscription_id,
                    public_guid=public_guid,
                    error_code=reason,
                    details_json=json.dumps(
                        {
                            "subscription_status": subscription.status,
                            "subscription_valid_until_utc": to_aware_utc(
                                subscription.expires_at
                            ).isoformat(),
                            "subscription_max_devices": subscription.max_devices,
                            "local_version": local.version,
                            "local_status": local.status,
                            "local_valid_until_utc": local_until.isoformat(),
                            "local_max_device_tokens": local.max_device_tokens,
                            "remote_version": remote.version,
                            "remote_status": remote.status,
                            "remote_valid_until_utc": remote_until.isoformat(),
                            "remote_max_device_tokens": remote.max_device_tokens,
                        },
                        sort_keys=True,
                    ),
                )
            )
            await NotificationOutboxRepository(session).enqueue_once(
                idempotency_key=f"reconciliation-blocked:{subscription_id}:{remote.version}:{reason}",
                notification_kind="operator_reconciliation_blocked",
                user_id=subscription.user_id,
                subscription_id=subscription_id,
                payload={
                    "reason_code": reason,
                    "public_guid": public_guid,
                    "telegram_id": subscription.user.telegram_id,
                    "username": subscription.user.username,
                    "subscription_status": subscription.status,
                    "local_version": local.version,
                    "local_status": local.status,
                    "local_valid_until_utc": local_until.isoformat(),
                    "local_max_device_tokens": local.max_device_tokens,
                    "remote_version": remote.version,
                    "remote_status": remote.status,
                    "remote_valid_until_utc": remote_until.isoformat(),
                    "remote_max_device_tokens": remote.max_device_tokens,
                    "suggested_action": (
                        "reconcile_adopt_expired"
                        if reason == "legacy_expiration_drift"
                        else "reconcile_status"
                    ),
                },
            )

    async with session_factory() as session:
        await _mark_worker_attempt(session, "entitlement_reconciliation", success=True)
    return synchronized


async def _quarantine_legacy_activating_order(
    session: AsyncSession,
    order: Order,
    mediator_client: MediatorClient,
    *,
    reason: str,
) -> None:
    operation = await EntitlementOperationCoordinator(session, mediator_client).prepare_order(
        order, enforce_refund_block=False
    )
    await EntitlementOperationRepository(session).mark_manual_review(operation, reason)
    subscription = None
    if order.target_subscription_id is not None:
        subscription = await SubscriptionRepository(session).get_by_id(order.target_subscription_id)
        if subscription is not None:
            subscription.reconciliation_state = "blocked"
            subscription.reconciliation_reason = reason
            subscription.reconciliation_blocked_at_utc = utc_now()
    session.add(
        AuditEvent(
            created_at_utc=utc_now(),
            event_type="order.legacy_activation_quarantined",
            telegram_id=order.user.telegram_id,
            order_id=order.id,
            subscription_id=order.target_subscription_id,
            public_guid=subscription.public_guid if subscription is not None else None,
            error_code=reason,
        )
    )
    await NotificationOutboxRepository(session).enqueue_once(
        idempotency_key=f"legacy-activating-quarantined:{order.public_order_id}",
        notification_kind="operator_legacy_activation_quarantined",
        user_id=order.user_id,
        subscription_id=order.target_subscription_id,
        order_id=order.id,
        bot_key=order.origin_bot_key,
        payload={
            "order_public_id": order.public_order_id,
            "reason_code": reason,
            "operation_public_id": operation.public_id,
        },
    )


def _entitlement_matches_local(
    local: AccessEntitlement,
    remote: MediatorEntitlementDetails,
) -> bool:
    if remote.valid_until_utc is None:
        return False
    remote_valid_until = to_aware_utc(
        datetime.fromisoformat(remote.valid_until_utc.replace("Z", "+00:00"))
    )
    return (
        remote.version == local.version
        and remote.status == local.status
        and remote_valid_until == to_aware_utc(local.valid_until_utc)
        and remote.max_device_tokens == local.max_device_tokens
    )


async def recover_legacy_activating_orders_once(
    session_factory,
    mediator_client: MediatorClient,
    settings: Settings,
) -> int:
    from vpn_access_bot.services import PurchaseService

    """Classify pre-operation-journal activating orders without guessing remote state.

    Existing OrderApplication rows are durable proof that local entitlement finalization happened.
    A retry is otherwise allowed only when the authoritative entitlement still exactly equals the
    order's captured base version and the local mirror. Any remote-newer or incomplete evidence is
    quarantined for an operator instead of blindly applying purchased duration again.
    """

    async with session_factory() as session:
        orders = await OrderRepository(session).list_legacy_activating_without_operation(limit=100)
        order_ids = [order.id for order in orders]

    resolved = 0
    for order_id in order_ids:
        async with session_factory() as session:
            order = await OrderRepository(session).get_by_id(order_id)
            if order is None or order.status != ORDER_STATUS_ACTIVATING:
                continue

            application = await OrderApplicationRepository(session).get_for_order(order.id)
            if application is not None:
                # The application row is the durable local proof. PurchaseService only completes
                # commerce state/outbox in this branch and cannot call Mediator again.
                try:
                    outcome = await PurchaseService(
                        session, settings, mediator_client
                    ).activate_order(order)
                except (MediatorClientError, RuntimeError, ValueError):
                    await session.rollback()
                    order = await OrderRepository(session).get_by_id(order_id)
                    if order is not None:
                        await _quarantine_legacy_activating_order(
                            session,
                            order,
                            mediator_client,
                            reason="legacy_application_finalization_failed",
                        )
                        await session.commit()
                    continue
                if outcome.activated or outcome.already_paid:
                    resolved += 1
                continue

            if order.target_subscription_id is None:
                await _quarantine_legacy_activating_order(
                    session,
                    order,
                    mediator_client,
                    reason="legacy_new_purchase_remote_state_unknown",
                )
                await session.commit()
                continue

            subscription = await SubscriptionRepository(session).get_by_id(
                order.target_subscription_id
            )
            local = await EntitlementRepository(session).get_for_subscription(
                order.target_subscription_id
            )
            if subscription is None or local is None:
                await _quarantine_legacy_activating_order(
                    session,
                    order,
                    mediator_client,
                    reason="legacy_activation_local_state_incomplete",
                )
                await session.commit()
                continue

            try:
                remote = await mediator_client.get_entitlement(subscription.public_guid)
            except MediatorClientError:
                # Keep the order untouched. A later recovery cycle can classify it with evidence.
                continue

            base_is_unchanged = (
                order.base_entitlement_version is not None
                and order.base_entitlement_version == local.version
                and _entitlement_matches_local(local, remote)
                and (
                    order.base_valid_until_utc is None
                    or to_aware_utc(order.base_valid_until_utc)
                    == to_aware_utc(local.valid_until_utc)
                )
            )
            if not base_is_unchanged:
                await _quarantine_legacy_activating_order(
                    session,
                    order,
                    mediator_client,
                    reason="legacy_activating_remote_state_ambiguous",
                )
                await session.commit()
                continue

            # The version captured by the paid order is still authoritative, so no old apply can
            # have advanced the entitlement. Creating the operation before retry makes the next
            # request durable and idempotent.
            await EntitlementOperationCoordinator(session, mediator_client).prepare_order(order)
            await session.commit()
            refreshed = await OrderRepository(session).get_by_id(order.id)
            if refreshed is None:
                continue
            outcome = await PurchaseService(session, settings, mediator_client).activate_order(
                refreshed
            )
            if outcome.activated or outcome.already_paid:
                resolved += 1

    return resolved


async def recover_entitlement_operations_once(
    session_factory,
    mediator_client: MediatorClient,
    settings: Settings,
) -> int:
    from vpn_access_bot.services import PurchaseService

    recovered = await recover_legacy_activating_orders_once(
        session_factory,
        mediator_client,
        settings,
    )
    async with session_factory() as session:
        operations = await EntitlementOperationRepository(session).list_recoverable(limit=100)
        operation_ids = [operation.public_id for operation in operations]

    for public_id in operation_ids:
        async with session_factory() as session:
            operation = await EntitlementOperationRepository(session).get_by_public_id(public_id)
            if operation is None:
                continue
            try:
                await EntitlementRecoveryWorker(session, mediator_client).classify(operation)
                await session.commit()
            except MediatorClientError:
                continue

            operation = await EntitlementOperationRepository(session).get_by_public_id(public_id)
            if operation is None:
                continue
            source_type = operation.source_entity_type
            source_id = operation.source_entity_id
            user = await UserRepository(session).get_by_id(operation.user_id)

            if source_type == "order":
                order = await OrderRepository(session).get_by_public_id_for_user(
                    source_id, operation.user_id
                )
                if order is None:
                    await EntitlementOperationRepository(session).mark_manual_review(
                        operation, "recovery_order_missing"
                    )
                    continue
                outcome = await PurchaseService(session, settings, mediator_client).activate_order(
                    order
                )
                if outcome.activated or outcome.already_paid:
                    recovered += 1
                continue

            if source_type == "trial_claim" and user is not None:
                from vpn_access_bot.services import TrialService

                claim = await session.get(TrialClaim, int(source_id))
                if claim is None:
                    await EntitlementOperationRepository(session).mark_manual_review(
                        operation, "recovery_trial_claim_missing"
                    )
                    continue
                if claim.status != "active":
                    claim.status = "activation_failed"
                    claim.failure_code = "recovery_retry"
                    await AccessOperationLeaseRepository(session).release(
                        user_id=user.id, owner_key=claim.idempotency_key
                    )
                    await session.commit()
                    outcome = await TrialService(session, settings, mediator_client).activate_trial(
                        user.telegram_id, user.username, user.first_name
                    )
                    if outcome.activated:
                        recovered += 1
                continue

            if source_type == "subscription_expiry":
                from vpn_access_bot.services import ExpirationService

                expiration_service = ExpirationService(session, mediator_client)
                recovered += await expiration_service.expire_due_subscriptions()
                continue

            if source_type == "admin_command":
                from vpn_access_bot.services import AdminEntitlementAdjustmentService

                try:
                    await AdminEntitlementAdjustmentService(session, mediator_client).recover(
                        operation.public_id
                    )
                    recovered += 1
                except (MediatorClientError, RuntimeError, ValueError):
                    continue
                continue

            if source_type == "reconciliation_repair":
                from vpn_access_bot.services import ReconciliationRepairService

                try:
                    await ReconciliationRepairService(session, mediator_client).recover(
                        operation.public_id
                    )
                    recovered += 1
                except (MediatorClientError, RuntimeError, ValueError):
                    continue
                continue

            if source_type in {"referral_reward", "refund"}:
                # Owning workers consume the operation and finalize their domain state.
                continue

    async with session_factory() as session:
        await _mark_worker_attempt(session, "entitlement_recovery", success=True)
    return recovered


async def recover_refund_operations_once(
    session_factory,
    mediator_client: MediatorClient,
    settings: Settings,
) -> int:
    from vpn_access_bot.services import PurchaseService

    cutoff = utc_now() - timedelta(minutes=15)
    async with session_factory() as session:
        operations = await RefundOperationRepository(session).list_recoverable(
            provider_unknown_cutoff=cutoff,
            limit=100,
        )
        operation_ids = [
            (operation.id, operation.order_id, operation.state) for operation in operations
        ]

    recovered = 0
    for operation_id, order_id, state in operation_ids:
        if state == "provider_unknown":
            async with session_factory() as session:
                operation = await session.get(
                    __import__(
                        "vpn_access_bot.models", fromlist=["RefundOperation"]
                    ).RefundOperation,
                    operation_id,
                )
                if operation is None or operation.state != "provider_unknown":
                    continue
                await RefundOperationRepository(session).mark_manual_review(
                    operation, "provider_outcome_unknown_after_restart"
                )
                await NotificationOutboxRepository(session).enqueue_once(
                    idempotency_key=f"refund-unknown:{operation.public_id}",
                    notification_kind="operator_refund_unknown_alert",
                    user_id=operation.user_id,
                    subscription_id=operation.subscription_id,
                    order_id=operation.order_id,
                    payload={
                        "operation_public_id": operation.public_id,
                        "reason_code": "provider_outcome_unknown_after_restart",
                    },
                )
                order = await OrderRepository(session).get_by_id(order_id)
                if order is not None:
                    await AccessOperationLeaseRepository(session).release(
                        user_id=operation.user_id,
                        owner_key=f"refund:{order.public_order_id}",
                    )
                await session.commit()
            continue

        async with session_factory() as session:
            try:
                await PurchaseService(
                    session, settings, mediator_client
                ).complete_refund_after_provider(order_id)
                recovered += 1
            except (MediatorClientError, RuntimeError, ValueError) as exception:
                logger.warning(
                    "Refund recovery remains unresolved: order_id=%s error=%s",
                    order_id,
                    type(exception).__name__,
                )

    async with session_factory() as session:
        await _mark_worker_attempt(session, "refund_recovery", success=True)
    return recovered


def _outbox_message(kind: str, payload_json: str | None = None) -> str:
    payload: dict[str, object] = {}
    if payload_json:
        try:
            decoded = json.loads(payload_json)
            if isinstance(decoded, dict):
                payload = decoded
        except (TypeError, ValueError):
            payload = {}

    if kind == "admin_broadcast":
        message_text = payload.get("message_text")
        return message_text if isinstance(message_text, str) else ""

    if kind == "operator_reconciliation_blocked" and payload:
        username = payload.get("username")
        username_text = f"@{escape_html(username)}" if username else "не указан"
        public_guid = payload.get("public_guid", "—")
        remote_version = payload.get("remote_version", "—")
        suggested_action = payload.get("suggested_action", "reconcile_status")
        if suggested_action == "reconcile_adopt_expired":
            action = (
                f"/reconcile_adopt_expired {public_guid} {remote_version} "
                "confirmed_legacy_expiration"
            )
        else:
            action = f"/reconcile_status {public_guid}"
        local_valid_until = escape_html(payload.get("local_valid_until_utc", "—"))
        local_devices = escape_html(payload.get("local_max_device_tokens", "—"))
        remote_valid_until = escape_html(payload.get("remote_valid_until_utc", "—"))
        remote_devices = escape_html(payload.get("remote_max_device_tokens", "—"))
        return (
            "⚠️ <b>Обнаружено расхождение подписки</b>\n\n"
            f"Пользователь: {username_text} "
            f"(<code>{escape_html(payload.get('telegram_id', '—'))}</code>)\n"
            f"GUID: <code>{escape_html(public_guid)}</code>\n"
            f"Причина: <code>{escape_html(payload.get('reason_code', 'unknown'))}</code>\n\n"
            "Bot:\n"
            "  subscription = "
            f"<code>{escape_html(payload.get('subscription_status', '—'))}</code>\n"
            f"  entitlement = <code>{escape_html(payload.get('local_status', '—'))}</code>, "
            f"version <b>{escape_html(payload.get('local_version', '—'))}</b>\n"
            f"  valid until = <code>{local_valid_until}</code>\n"
            f"  devices = <b>{local_devices}</b>\n\n"
            "Mediator:\n"
            f"  entitlement = <code>{escape_html(payload.get('remote_status', '—'))}</code>, "
            f"version <b>{escape_html(remote_version)}</b>\n"
            f"  valid until = <code>{remote_valid_until}</code>\n"
            f"  devices = <b>{remote_devices}</b>\n\n"
            f"Рекомендуемое действие: <code>{escape_html(action)}</code>"
        )

    if kind == "operator_reconciliation_repaired" and payload:
        return (
            "ℹ️ <b>Расхождение entitlement устранено</b>\n\n"
            f"GUID: <code>{escape_html(payload.get('public_guid', '—'))}</code>\n"
            f"Бизнес-состояние: "
            f"<code>{escape_html(payload.get('subscription_status', '—'))}</code>\n"
            f"Entitlement: <code>{escape_html(payload.get('entitlement_status', '—'))}</code>, "
            f"version <b>{escape_html(payload.get('entitlement_version', '—'))}</b>"
        )

    if kind == "operator_paid_order_reconciliation_blocked" and payload:
        return (
            "🚨 <b>Оплаченный заказ ожидает восстановления синхронизации</b>\n\n"
            f"Заказ: <code>{escape_html(payload.get('order_public_id', '—'))}</code>\n"
            f"GUID: <code>{escape_html(payload.get('public_guid', '—'))}</code>\n"
            f"Причина: <code>{escape_html(payload.get('reason_code', 'unknown'))}</code>\n\n"
            "Платёж уже получен. Не создавайте новый заказ и не применяйте срок вручную. "
            "Сначала устраните расхождение через проверяемую reconciliation operation; "
            "после этого штатный recovery завершит исходный заказ ровно один раз."
        )

    messages = {
        "payment_received": (
            "<b>Оплата подтверждена и сохранена</b>\n\n"
            "Доступ активируется автоматически. Повторно оплачивать заказ не нужно."
        ),
        "payment_manual_review": (
            "Оплата подтверждена Telegram и сохранена, но её данные требуют проверки.\n\n"
            "Не оплачивайте заказ повторно. Откройте поддержку, и платёж будет проверен "
            "по данным Telegram."
        ),
        "order_activated": (
            "<b>Оплата подтверждена</b>\n\nДоступ активирован. Можно подключить устройство."
        ),
        "trial_activated": (
            "<b>Пробный доступ активирован</b>\n\nПериод начался после фактической выдачи доступа."
        ),
        "refund_started": (
            "Возврат принят в обработку. До завершения операции повторная активация заблокирована."
        ),
        "refund_completed": (
            "<b>Возврат завершён</b>\n\nОплаченный доступ отозван согласованно с возвратом."
        ),
        "referral_reward_applied": "Реферальное продление применено к подписке.",
        "admin_subscription_adjusted": (
            "Администратор скорректировал срок или лимит устройств подписки."
        ),
        "admin_subscription_revoked": "Доступ по подписке отключён администратором.",
        "operator_refund_unknown_alert": (
            "⚠️ Результат возврата у провайдера неизвестен. Требуется ручная проверка."
        ),
        "operator_reconciliation_blocked": (
            "⚠️ Подписка помещена в quarantine из-за неизвестного расхождения entitlement."
        ),
        "operator_reconciliation_recovery": (
            "ℹ️ Обнаружена известная незавершённая entitlement operation; запущено восстановление."
        ),
        "operator_expiration_race_alert": (
            "⚠️ Обнаружена гонка expiration и renewal; автоматические mutation заблокированы."
        ),
        "operator_legacy_activation_quarantined": (
            "⚠️ Старый activating order помещён в quarantine: безопасный результат операции "
            "нельзя доказать автоматически."
        ),
        "operator_reconciliation_repaired": (
            "ℹ️ Расхождение entitlement устранено явной аудируемой operator operation."
        ),
    }
    return messages.get(kind, "Состояние подписки обновлено.")


def _outbox_retry_policy(
    settings: Settings,
    notification_kind: str,
) -> tuple[int, int, int]:
    if notification_kind == "admin_broadcast":
        return (
            getattr(settings, "broadcast_max_delivery_attempts", 8),
            getattr(settings, "broadcast_retry_base_seconds", 30),
            getattr(settings, "broadcast_retry_max_seconds", 3600),
        )

    return (
        getattr(settings, "notification_outbox_max_delivery_attempts", 8),
        getattr(settings, "notification_outbox_retry_base_seconds", 30),
        getattr(settings, "notification_outbox_retry_max_seconds", 3600),
    )


async def dispatch_notification_outbox_once(
    session_factory,
    bot: Bot,
    settings: Settings,
) -> int:
    async with session_factory() as session:
        items = await NotificationOutboxRepository(session).claim_batch(limit=50)
        item_ids = [item.id for item in items]
        await session.commit()

    accepted = 0
    for item_id in item_ids:
        async with session_factory() as session:
            item = await session.get(NotificationOutbox, item_id)
            if item is None or item.state != "sending":
                continue
            user = (
                await UserRepository(session).get_by_id(item.user_id)
                if item.user_id is not None
                else None
            )
            is_operator = item.notification_kind.startswith("operator_")
            recipients = list(settings.admin_telegram_ids) if is_operator else []
            if not is_operator and user is not None:
                recipients = [user.telegram_id]
            if not recipients:
                repository = NotificationOutboxRepository(session)
                await repository.mark_terminal_failed(item, "notification_recipient_missing")
                await session.commit()
                continue
            message_text = _outbox_message(item.notification_kind, item.payload_json)
            if (
                item.notification_kind == "admin_broadcast"
                and item.broadcast_campaign_id is not None
            ):
                campaign = await session.get(BroadcastCampaign, item.broadcast_campaign_id)
                message_text = (
                    campaign.message_text
                    if campaign is not None and campaign.state in {"enqueuing", "queued"}
                    else ""
                )
            if item.notification_kind == "admin_broadcast" and not message_text:
                await NotificationOutboxRepository(session).mark_terminal_failed(
                    item, "broadcast_payload_invalid"
                )
                await session.commit()
                continue
            try:
                for recipient in recipients:
                    kwargs = {}
                    if item.bot_key is not None:
                        kwargs["bot_key"] = item.bot_key
                    if item.notification_kind == "order_activated":
                        from vpn_access_bot.keyboards import after_purchase_keyboard

                        kwargs["reply_markup"] = after_purchase_keyboard()
                    if item.notification_kind == "admin_broadcast":
                        kwargs["parse_mode"] = None
                    send_result = await bot.send_message(
                        recipient,
                        message_text,
                        **kwargs,
                    )
                    delivery_bot_key = getattr(send_result, "delivery_bot_key", None)
            except TelegramRetryAfter as exception:
                logger.warning(
                    "Telegram rate limited outbox delivery: kind=%s retry_after=%s",
                    item.notification_kind,
                    exception.retry_after,
                )
                repository = NotificationOutboxRepository(session)
                max_attempts, retry_base_seconds, retry_max_seconds = _outbox_retry_policy(
                    settings,
                    item.notification_kind,
                )
                await repository.mark_failed_bounded(
                    item,
                    "telegram_retry_after",
                    max_attempts=max_attempts,
                    retry_base_seconds=retry_base_seconds,
                    retry_max_seconds=retry_max_seconds,
                    retry_after_seconds=int(exception.retry_after),
                )
                await session.commit()
                continue
            except NotificationRecipientUnavailable:
                repository = NotificationOutboxRepository(session)
                await repository.mark_terminal_failed(
                    item,
                    "notification_recipient_unavailable",
                )
                await session.commit()
                continue
            except TelegramForbiddenError:
                logger.warning(
                    "Telegram permanently rejected outbox delivery: kind=%s error=%s",
                    item.notification_kind,
                    "telegram_forbidden",
                )
                repository = NotificationOutboxRepository(session)
                await repository.mark_terminal_failed(item, "telegram_forbidden")
                await session.commit()
                continue
            except TelegramBadRequest:
                logger.warning(
                    "Telegram permanently rejected outbox delivery: kind=%s error=%s",
                    item.notification_kind,
                    "telegram_bad_request",
                )
                repository = NotificationOutboxRepository(session)
                await repository.mark_terminal_failed(item, "telegram_bad_request")
                await session.commit()
                continue
            except Exception as exception:
                logger.exception(
                    "Transactional outbox delivery failed: kind=%s",
                    item.notification_kind,
                )
                repository = NotificationOutboxRepository(session)
                max_attempts, retry_base_seconds, retry_max_seconds = _outbox_retry_policy(
                    settings,
                    item.notification_kind,
                )
                await repository.mark_failed_bounded(
                    item,
                    type(exception).__name__,
                    max_attempts=max_attempts,
                    retry_base_seconds=retry_base_seconds,
                    retry_max_seconds=retry_max_seconds,
                )
                await session.commit()
                continue
            await NotificationOutboxRepository(session).mark_provider_accepted(
                item, delivery_bot_key
            )
            await session.commit()
            accepted += 1
            if item.notification_kind == "admin_broadcast":
                await asyncio.sleep(0.05)

    return accepted


async def send_notifications_once(
    session_factory,
    bot: Bot,
    settings: Settings,
) -> int:
    now = utc_now()
    sent = 0

    async with session_factory() as session:
        result = await session.execute(
            select(Subscription, User)
            .join(User, User.id == Subscription.user_id)
            .where(
                Subscription.status == SUBSCRIPTION_STATUS_ACTIVE,
                Subscription.test_reset_at_utc.is_(None),
                Subscription.expires_at > now,
            )
        )
        subscriptions = list(result.all())

    for subscription, user in subscriptions:
        remaining = to_aware_utc(subscription.expires_at) - now
        candidates: list[tuple[str, str]] = []

        if timedelta(hours=23) < remaining <= timedelta(days=1):
            candidates.append(
                (
                    "subscription_1d",
                    "<b>Доступ закончится завтра</b>\n\nПродление займёт меньше минуты.",
                )
            )
        elif timedelta(days=2) < remaining <= timedelta(days=3):
            candidates.append(
                (
                    "subscription_3d",
                    "<b>Доступ закончится через 3 дня</b>\n\n"
                    "Продлите сейчас, чтобы VPN не отключился.",
                )
            )

        for kind, text in candidates:
            key = f"{kind}:{subscription.expires_at.isoformat()}"

            async with session_factory() as session:
                delivery = await NotificationDeliveryRepository(session).claim(
                    subscription.id,
                    kind,
                    key,
                )
                delivery_id = delivery.id if delivery is not None else None

            if delivery_id is None:
                continue

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Продлить доступ", callback_data="buy:renew")]
                ]
            )
            try:
                send_result = await bot.send_message(user.telegram_id, text, reply_markup=keyboard)
            except Exception as exception:
                logger.exception(
                    "Notification delivery failed: kind=%s telegram_id=%s",
                    kind,
                    user.telegram_id,
                )
                async with session_factory() as session:
                    await NotificationDeliveryRepository(session).mark_failed(
                        delivery_id,
                        type(exception).__name__,
                    )
                continue

            async with session_factory() as session:
                await NotificationDeliveryRepository(session).mark_delivered(
                    delivery_id, getattr(send_result, "delivery_bot_key", None)
                )
            sent += 1

    onboarding_cutoff = now - timedelta(minutes=settings.onboarding_reminder_delay_minutes)
    async with session_factory() as session:
        result = await session.execute(
            select(OnboardingSession, Subscription, User)
            .join(Subscription, Subscription.id == OnboardingSession.subscription_id)
            .join(User, User.id == OnboardingSession.user_id)
            .where(
                OnboardingSession.status.in_(
                    [
                        "platform_selection",
                        "app_installation",
                        "access_delivery",
                        "waiting_first_fetch",
                        "waiting_activation",
                    ]
                ),
                OnboardingSession.updated_at_utc <= onboarding_cutoff,
                Subscription.status == SUBSCRIPTION_STATUS_ACTIVE,
                Subscription.test_reset_at_utc.is_(None),
                Subscription.expires_at > now,
            )
        )
        onboarding_rows = list(result.all())

    for onboarding_session, subscription, user in onboarding_rows:
        delivery_key = f"onboarding_incomplete:{onboarding_session.id}"
        async with session_factory() as session:
            delivery = await NotificationDeliveryRepository(session).claim(
                subscription.id,
                "onboarding_incomplete",
                delivery_key,
                onboarding_session.origin_bot_key,
            )
            delivery_id = delivery.id if delivery is not None else None
        if delivery_id is None:
            continue
        waiting_for_client = onboarding_session.status in {
            "waiting_first_fetch",
            "waiting_activation",
        }
        callback_data = "credential:check" if waiting_for_client else "credential:create"
        button_text = "Не появилось в Happ?" if waiting_for_client else "Открыть в Happ"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=button_text, callback_data=callback_data)]]
        )
        try:
            send_result = await bot.send_message(
                user.telegram_id,
                "Похоже, подключение не было закончено.\n\n"
                "Доступ уже активен — осталось добавить его в Happ.",
                reply_markup=keyboard,
                bot_key=onboarding_session.origin_bot_key,
            )
        except Exception as exception:
            logger.exception(
                "Onboarding reminder failed: telegram_id=%s session_id=%s",
                user.telegram_id,
                onboarding_session.id,
            )
            async with session_factory() as session:
                await NotificationDeliveryRepository(session).mark_failed(
                    delivery_id,
                    type(exception).__name__,
                )
        else:
            async with session_factory() as session:
                await NotificationDeliveryRepository(session).mark_delivered(
                    delivery_id, getattr(send_result, "delivery_bot_key", None)
                )
            sent += 1

    async with session_factory() as session:
        result = await session.execute(
            select(TrialClaim, Subscription, User)
            .join(Subscription, Subscription.id == TrialClaim.subscription_id)
            .join(User, User.id == TrialClaim.user_id)
            .where(
                TrialClaim.status == "active",
                TrialClaim.ends_at_utc > now,
            )
        )
        trial_rows = list(result.all())

    for claim, subscription, user in trial_rows:
        remaining = to_aware_utc(claim.ends_at_utc) - now
        candidate: tuple[str, str] | None = None
        if timedelta(hours=20) < remaining <= timedelta(hours=28):
            candidate = (
                "trial_day_one",
                "Бесплатный доступ работает. Можно заранее выбрать срок, "
                f"чтобы {settings.product_name} не отключился после пробного периода.",
            )
        elif timedelta(0) < remaining <= timedelta(hours=4):
            candidate = (
                "trial_ending",
                "Бесплатный период скоро закончится. "
                f"Точная дата указана в разделе «{settings.product_name}».",
            )
        if candidate is None:
            continue
        kind, text = candidate
        key = f"{kind}:{claim.id}"
        async with session_factory() as session:
            delivery = await NotificationDeliveryRepository(session).claim(
                subscription.id,
                kind,
                key,
            )
            delivery_id = delivery.id if delivery is not None else None
        if delivery_id is None:
            continue
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Купить доступ", callback_data="buy:menu")]]
        )
        try:
            send_result = await bot.send_message(user.telegram_id, text, reply_markup=keyboard)
        except Exception as exception:
            logger.exception(
                "Trial notification failed: kind=%s telegram_id=%s",
                kind,
                user.telegram_id,
            )
            async with session_factory() as session:
                await NotificationDeliveryRepository(session).mark_failed(
                    delivery_id,
                    type(exception).__name__,
                )
        else:
            async with session_factory() as session:
                await NotificationDeliveryRepository(session).mark_delivered(
                    delivery_id, getattr(send_result, "delivery_bot_key", None)
                )
            sent += 1

    async with session_factory() as session:
        await _mark_worker_attempt(session, "notifications", success=True)

    return sent


async def _deliver_onboarding_completed_notification(
    session_factory,
    bot: Bot,
    *,
    onboarding_session_id: int,
    subscription_id: int,
    telegram_id: int,
    origin_bot_key: str | None = None,
) -> bool:
    delivery_key = f"onboarding_completed:{onboarding_session_id}"
    async with session_factory() as session:
        delivery = await NotificationDeliveryRepository(session).claim(
            subscription_id,
            "onboarding_completed",
            delivery_key,
            origin_bot_key,
        )
        delivery_id = delivery.id if delivery is not None else None

    if delivery_id is None:
        return False

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Готово", callback_data="menu:main")],
            [InlineKeyboardButton(text="Подарить другу VPN", callback_data="referral:show")],
        ]
    )
    try:
        send_result = await bot.send_message(
            telegram_id,
            "<b>Подписка добавлена в Happ</b>\n\n"
            "Список подключений загружен. Теперь откройте Happ и включите VPN "
            "большой кнопкой на главном экране приложения.",
            reply_markup=keyboard,
            bot_key=origin_bot_key,
        )
    except Exception as exception:
        logger.exception(
            "Onboarding completion notification failed: telegram_id=%s session_id=%s",
            telegram_id,
            onboarding_session_id,
        )
        async with session_factory() as session:
            await NotificationDeliveryRepository(session).mark_failed(
                delivery_id,
                type(exception).__name__,
            )
        return False

    async with session_factory() as session:
        await NotificationDeliveryRepository(session).mark_delivered(
            delivery_id, getattr(send_result, "delivery_bot_key", None)
        )
    return True


async def process_onboarding_completions_once(
    session_factory,
    mediator_client: MediatorClient,
    bot: Bot,
) -> int:
    async with session_factory() as session:
        waiting_sessions = await OnboardingSessionRepository(session).list_waiting_first_fetch()
        waiting_plans: list[tuple[int, int, int, int, str, str, str | None, bool]] = []
        for onboarding_session in waiting_sessions:
            if onboarding_session.subscription_id is None or (
                onboarding_session.device_public_id is None
                and onboarding_session.handoff_claim_id is None
            ):
                continue
            subscription = await SubscriptionRepository(session).get_by_id(
                onboarding_session.subscription_id
            )
            user = await UserRepository(session).get_by_id(onboarding_session.user_id)
            if subscription is None or user is None:
                continue
            waiting_plans.append(
                (
                    onboarding_session.id,
                    onboarding_session.user_id,
                    subscription.id,
                    user.telegram_id,
                    subscription.public_guid,
                    onboarding_session.device_public_id or onboarding_session.handoff_claim_id,
                    onboarding_session.origin_bot_key,
                    onboarding_session.device_public_id is None,
                )
            )

    completed_count = 0
    for (
        onboarding_session_id,
        user_id,
        subscription_id,
        telegram_id,
        subscription_guid,
        claim_public_id,
        origin_bot_key,
        legacy_identifier,
    ) in waiting_plans:
        try:
            devices = await mediator_client.list_device_tokens(subscription_guid)
            target_device = next(
                (
                    device
                    for device in devices
                    if device.public_id == claim_public_id
                    and device.first_fetched_at_utc is not None
                ),
                None,
            )
        except MediatorClientError:
            continue

        if target_device is None:
            continue

        async with session_factory() as session:
            completed = await OnboardingSessionRepository(session).mark_completed(
                onboarding_session_id,
                target_device.public_id,
            )
            if completed:
                first_fetch_event = (
                    "legacy_credential_first_fetched"
                    if legacy_identifier
                    else "credential_first_fetched"
                )
                await ProductEventRepository(session).record(
                    event_name=first_fetch_event,
                    user_id=user_id,
                    idempotency_key=f"{first_fetch_event}:{target_device.public_id}",
                    payload={"platform": target_device.platform or "unknown"},
                )
                await ProductEventRepository(session).record(
                    event_name="subscription_observed_by_client",
                    user_id=user_id,
                    idempotency_key=(f"subscription_observed_by_client:{target_device.public_id}"),
                    payload={"platform": target_device.platform or "unknown"},
                )
                await ProductEventRepository(session).record(
                    event_name="onboarding_completed",
                    user_id=user_id,
                    idempotency_key=f"onboarding_completed:{onboarding_session_id}",
                )
        if completed:
            await _deliver_onboarding_completed_notification(
                session_factory,
                bot,
                onboarding_session_id=onboarding_session_id,
                subscription_id=subscription_id,
                telegram_id=telegram_id,
                origin_bot_key=origin_bot_key,
            )
            completed_count += 1

    # Retry notification delivery for recently completed sessions. Manual completion claims and
    # marks the same key as delivered, so this scan cannot create duplicates.
    async with session_factory() as session:
        completed_sessions = await OnboardingSessionRepository(session).list_recent_completed(
            utc_now() - timedelta(days=7)
        )
        notification_plans: list[tuple[int, int, int, str | None]] = []
        for onboarding_session in completed_sessions:
            if onboarding_session.subscription_id is None:
                continue
            user = await UserRepository(session).get_by_id(onboarding_session.user_id)
            if user is None:
                continue
            notification_plans.append(
                (
                    onboarding_session.id,
                    onboarding_session.subscription_id,
                    user.telegram_id,
                    onboarding_session.origin_bot_key,
                )
            )

    for (
        onboarding_session_id,
        subscription_id,
        telegram_id,
        origin_bot_key,
    ) in notification_plans:
        await _deliver_onboarding_completed_notification(
            session_factory,
            bot,
            onboarding_session_id=onboarding_session_id,
            subscription_id=subscription_id,
            telegram_id=telegram_id,
            origin_bot_key=origin_bot_key,
        )

    async with session_factory() as session:
        await _mark_worker_attempt(session, "onboarding_completion", success=True)

    return completed_count


async def _send_admin_alert_once(
    session_factory,
    bot: Bot,
    *,
    admin_telegram_id: int,
    alert_key: str,
    text: str,
    reason_code: str,
) -> bool:
    idempotency_key = f"operational_alert:{admin_telegram_id}:{alert_key}"
    async with session_factory() as session:
        if await ProductEventRepository(session).exists_idempotency_key(idempotency_key):
            return False

    try:
        await bot.send_message(admin_telegram_id, text)
    except Exception:
        logger.exception(
            "Operational alert delivery failed: admin_telegram_id=%s reason_code=%s",
            admin_telegram_id,
            reason_code,
        )
        return False

    async with session_factory() as session:
        await ProductEventRepository(session).record(
            event_name="operational_alert_sent",
            idempotency_key=idempotency_key,
            payload={"reason_code": reason_code},
        )
        await session.commit()
    return True


async def send_operational_alerts_once(
    session_factory,
    mediator_client: MediatorClient,
    bot: Bot,
    settings: Settings,
) -> int:
    if not settings.admin_telegram_ids:
        async with session_factory() as session:
            await _mark_worker_attempt(session, "operational_alerts", success=True)
            await session.commit()
        return 0

    now = utc_now()
    alerts: list[tuple[str, str, str]] = []

    try:
        readiness = await mediator_client.get_readiness()
    except MediatorClientError:
        hour_key = now.strftime("%Y%m%d%H")
        alerts.append(
            (
                f"mediator_unavailable:{hour_key}",
                "mediator_unavailable",
                "⚠️ Mediator недоступен. Новые оплаты и trial должны быть заблокированы. "
                f"Время: {now:%d.%m.%Y %H:%M} UTC.",
            )
        )
    else:
        reason = readiness_failure_reason(readiness)
        if reason is not None:
            hour_key = now.strftime("%Y%m%d%H")
            details = (
                f"серверов: <b>{readiness.server_count}</b>; "
                f"состояние каталога: <code>{readiness.catalog_state}</code>"
            )
            alerts.append(
                (
                    f"{reason}:{hour_key}",
                    reason,
                    f"⚠️ Нельзя выдавать новые подключения. Код: <code>{reason}</code>; {details}.",
                )
            )

    async with session_factory() as session:
        result = await session.execute(
            select(Order)
            .where(
                Order.status == ORDER_STATUS_ACTIVATION_FAILED,
                (Order.paid_at.is_not(None) | Order.provider_payment_id.is_not(None)),
            )
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(50)
        )
        failed_orders = list(result.scalars().all())
        worker_result = await session.execute(select(WorkerHealth))
        worker_rows = list(worker_result.scalars().all())
        payment_result = await session.execute(
            select(PaymentInbox)
            .where(PaymentInbox.reconciliation_status.in_(["received", "matched", "manual_review"]))
            .order_by(PaymentInbox.received_at_utc, PaymentInbox.id)
            .limit(50)
        )
        unresolved_payments = list(payment_result.scalars().all())

    for order in failed_orders:
        alerts.append(
            (
                f"paid_activation_failed:{order.public_order_id}",
                "paid_activation_failed",
                "⚠️ Оплата получена, но доступ не применён. "
                f"Заказ: <code>{order.public_order_id}</code>; код: "
                f"<code>{order.last_activation_error_code or 'activation_failed'}</code>.",
            )
        )

    stale_payment_before = now - timedelta(minutes=2)
    for payment in unresolved_payments:
        received_at = to_aware_utc(payment.received_at_utc)
        if payment.reconciliation_status != "manual_review" and received_at > stale_payment_before:
            continue
        age_minutes = max(int((now - received_at).total_seconds() // 60), 0)
        if payment.reconciliation_status == "manual_review":
            alert_key = f"payment_manual_review:{payment.id}"
            reason_code = "payment_manual_review"
        else:
            hour_key = now.strftime("%Y%m%d%H")
            alert_key = f"payment_unresolved:{payment.id}:{hour_key}"
            reason_code = "payment_unresolved"
        alerts.append(
            (
                alert_key,
                reason_code,
                "⚠️ Платёж требует внимания. "
                f"Inbox ID: <code>{payment.id}</code>; "
                f"заказ: <code>{payment.matched_order_id or 'не сопоставлен'}</code>; "
                f"статус: <code>{payment.reconciliation_status}</code>; "
                f"код: <code>{payment.failure_code or 'нет'}</code>; "
                f"возраст: <b>{age_minutes} мин.</b>",
            )
        )

    stale_after = now - timedelta(
        seconds=max(
            settings.notification_check_interval_seconds,
            settings.referral_check_interval_seconds,
            settings.entitlement_reconciliation_interval_seconds,
            60,
        )
        * 3
    )
    for worker in worker_rows:
        last_success = (
            to_aware_utc(worker.last_success_at_utc)
            if worker.last_success_at_utc is not None
            else None
        )
        if last_success is not None and last_success >= stale_after:
            continue
        hour_key = now.strftime("%Y%m%d%H")
        alerts.append(
            (
                f"worker_stale:{worker.worker_name}:{hour_key}",
                "worker_stale",
                "⚠️ Фоновая задача не подтверждала успешную работу. "
                f"Задача: <code>{worker.worker_name}</code>; последний код: "
                f"<code>{worker.last_error_code or 'нет данных'}</code>.",
            )
        )

    sent = 0
    for alert_key, reason_code, text in alerts:
        for admin_telegram_id in settings.admin_telegram_ids:
            if await _send_admin_alert_once(
                session_factory,
                bot,
                admin_telegram_id=admin_telegram_id,
                alert_key=alert_key,
                text=text,
                reason_code=reason_code,
            ):
                sent += 1

    async with session_factory() as session:
        await _mark_worker_attempt(session, "operational_alerts", success=True)
        await session.commit()
    return sent


async def reconcile_payment_inbox_once(
    session_factory,
    mediator_client: MediatorClient,
    settings: Settings,
    *,
    worker_id: str = "payment-reconciliation",
    limit: int = 20,
) -> int:
    from vpn_access_bot.services import PurchaseService

    async with session_factory() as session:
        claimed = await PaymentInboxRepository(session).claim_due(
            worker_id=worker_id,
            limit=limit,
            lease_seconds=max(settings.payment_reconciliation_interval_seconds * 4, 30),
        )
        inbox_ids = [inbox.id for inbox in claimed]
        await session.commit()

    processed = 0
    for inbox_id in inbox_ids:
        try:
            async with session_factory() as session:
                await PurchaseService(
                    session, settings, mediator_client
                ).reconcile_payment_inbox_by_id(inbox_id)
                await session.commit()
            processed += 1
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            logger.exception("Payment reconciliation failed: inbox_id=%s", inbox_id)
            async with session_factory() as session:
                repository = PaymentInboxRepository(session)
                inbox = await repository.get_by_id(inbox_id)
                if inbox is not None and inbox.reconciliation_status == "received":
                    delay = min(2 ** min(max(inbox.attempt_count, 1), 8), 300)
                    await repository.mark_retry(
                        inbox, type(exception).__name__, delay_seconds=delay
                    )
                    await session.commit()
    return processed


async def activate_paid_orders_once(
    session_factory,
    mediator_client: MediatorClient,
    settings: Settings,
    *,
    limit: int = 20,
) -> int:
    from vpn_access_bot.services import PurchaseService

    async with session_factory() as session:
        orders = await OrderRepository(session).list_activation_candidates(
            now=utc_now(), limit=limit
        )
        order_ids = [order.id for order in orders]

    processed = 0
    for order_id in order_ids:
        try:
            async with session_factory() as session:
                outcome = await PurchaseService(
                    session, settings, mediator_client
                ).activate_order_by_id(order_id)
            if outcome.activated or outcome.already_paid:
                processed += 1
        except asyncio.CancelledError:
            raise
        except ValueError:
            logger.warning("Activation candidate changed state: order_id=%s", order_id)
        except Exception:
            logger.exception("Paid order activation worker failed: order_id=%s", order_id)
    return processed


async def _run_worker_loop(
    *,
    worker_name: str,
    interval_seconds: int,
    callback: Callable[[], Awaitable[object]],
    failure_callback: Callable[[str], Awaitable[None]],
    success_callback: Callable[[], Awaitable[None]] | None = None,
    critical: bool = False,
    failure_limit: int = 5,
) -> None:
    backoff_seconds = min(max(interval_seconds, 1), 60)

    consecutive_failures = 0

    while True:
        try:
            await callback()
            if success_callback is not None:
                try:
                    await success_callback()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Failed to record successful worker health: worker=%s",
                        worker_name,
                    )
            consecutive_failures = 0
            backoff_seconds = min(max(interval_seconds, 1), 60)
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            consecutive_failures += 1
            logger.exception("Product worker failed: worker=%s", worker_name)
            try:
                await failure_callback(type(exception).__name__)
            except Exception:
                logger.exception("Failed to record worker health: worker=%s", worker_name)
            if critical and consecutive_failures >= max(failure_limit, 1):
                raise RuntimeError(
                    f"Critical worker {worker_name} exceeded its failure limit."
                ) from exception
            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, 300)


async def _record_worker_health(
    session_factory,
    worker_name: str,
    *,
    success: bool,
    error_code: str | None = None,
) -> None:
    async with session_factory() as session:
        await _mark_worker_attempt(
            session,
            worker_name,
            success=success,
            error_code=error_code,
        )
        await session.commit()


async def run_product_workers(
    session_factory,
    mediator_client: MediatorClient,
    bot: Bot,
    settings: Settings,
) -> None:
    async def housekeeping() -> None:
        async with session_factory() as session:
            await expire_pending_orders_once(session)
            await OnboardingSessionRepository(session).abandon_stale_sessions(
                utc_now() - timedelta(hours=settings.onboarding_stale_hours)
            )
            await _mark_worker_attempt(session, "order_housekeeping", success=True)
            await session.commit()

    async def payment_reconciliation() -> None:
        await reconcile_payment_inbox_once(session_factory, mediator_client, settings)
        async with session_factory() as session:
            await _mark_worker_attempt(session, "payment_reconciliation", success=True)
            await session.commit()

    async def paid_order_activation() -> None:
        await activate_paid_orders_once(session_factory, mediator_client, settings)
        async with session_factory() as session:
            await _mark_worker_attempt(session, "paid_order_activation", success=True)
            await session.commit()

    async def mark_failure(worker_name: str, error_code: str) -> None:
        await _record_worker_health(
            session_factory,
            worker_name,
            success=False,
            error_code=error_code,
        )

    async def mark_success(worker_name: str) -> None:
        await _record_worker_health(
            session_factory,
            worker_name,
            success=True,
        )

    tasks = [
        asyncio.create_task(
            _run_worker_loop(
                worker_name="order_housekeeping",
                interval_seconds=min(settings.notification_check_interval_seconds, 300),
                callback=housekeeping,
                failure_callback=lambda error: mark_failure("order_housekeeping", error),
                critical=True,
                failure_limit=settings.critical_worker_failure_limit,
            )
        ),
        asyncio.create_task(
            _run_worker_loop(
                worker_name="payment_reconciliation",
                interval_seconds=settings.payment_reconciliation_interval_seconds,
                callback=payment_reconciliation,
                failure_callback=lambda error: mark_failure("payment_reconciliation", error),
                critical=True,
                failure_limit=settings.critical_worker_failure_limit,
            )
        ),
        asyncio.create_task(
            _run_worker_loop(
                worker_name="paid_order_activation",
                interval_seconds=settings.payment_reconciliation_interval_seconds,
                callback=paid_order_activation,
                failure_callback=lambda error: mark_failure("paid_order_activation", error),
                critical=True,
                failure_limit=settings.critical_worker_failure_limit,
            )
        ),
        asyncio.create_task(
            _run_worker_loop(
                worker_name="referral_rewards",
                interval_seconds=settings.referral_check_interval_seconds,
                callback=lambda: process_referral_rewards_once(
                    session_factory,
                    mediator_client,
                ),
                failure_callback=lambda error: mark_failure("referral_rewards", error),
            )
        ),
        asyncio.create_task(
            _run_worker_loop(
                worker_name="entitlement_reconciliation",
                interval_seconds=settings.entitlement_reconciliation_interval_seconds,
                callback=lambda: reconcile_entitlements_once(
                    session_factory,
                    mediator_client,
                ),
                failure_callback=lambda error: mark_failure("entitlement_reconciliation", error),
                critical=True,
                failure_limit=settings.critical_worker_failure_limit,
            )
        ),
        asyncio.create_task(
            _run_worker_loop(
                worker_name="entitlement_recovery",
                interval_seconds=min(settings.entitlement_reconciliation_interval_seconds, 60),
                callback=lambda: recover_entitlement_operations_once(
                    session_factory, mediator_client, settings
                ),
                failure_callback=lambda error: mark_failure("entitlement_recovery", error),
                critical=True,
                failure_limit=settings.critical_worker_failure_limit,
            )
        ),
        asyncio.create_task(
            _run_worker_loop(
                worker_name="refund_recovery",
                interval_seconds=min(settings.entitlement_reconciliation_interval_seconds, 60),
                callback=lambda: recover_refund_operations_once(
                    session_factory, mediator_client, settings
                ),
                failure_callback=lambda error: mark_failure("refund_recovery", error),
                critical=True,
                failure_limit=settings.critical_worker_failure_limit,
            )
        ),
        asyncio.create_task(
            _run_worker_loop(
                worker_name="notification_outbox",
                interval_seconds=min(settings.notification_check_interval_seconds, 30),
                callback=lambda: dispatch_notification_outbox_once(session_factory, bot, settings),
                success_callback=lambda: mark_success("notification_outbox"),
                failure_callback=lambda error: mark_failure("notification_outbox", error),
            )
        ),
        asyncio.create_task(
            _run_worker_loop(
                worker_name="notifications",
                interval_seconds=settings.notification_check_interval_seconds,
                callback=lambda: send_notifications_once(session_factory, bot, settings),
                failure_callback=lambda error: mark_failure("notifications", error),
            )
        ),
        asyncio.create_task(
            _run_worker_loop(
                worker_name="onboarding_completion",
                interval_seconds=min(settings.notification_check_interval_seconds, 60),
                callback=lambda: process_onboarding_completions_once(
                    session_factory,
                    mediator_client,
                    bot,
                ),
                failure_callback=lambda error: mark_failure("onboarding_completion", error),
            )
        ),
        asyncio.create_task(
            _run_worker_loop(
                worker_name="operational_alerts",
                interval_seconds=60,
                callback=lambda: send_operational_alerts_once(
                    session_factory,
                    mediator_client,
                    bot,
                    settings,
                ),
                failure_callback=lambda error: mark_failure("operational_alerts", error),
            )
        ),
    ]

    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
