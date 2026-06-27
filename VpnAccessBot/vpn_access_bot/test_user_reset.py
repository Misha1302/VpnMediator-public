from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from vpn_access_bot.constants import (
    ENTITLEMENT_OPERATION_ACTIVE_STATES,
    ORDER_ACTIVATION_RETRY_STATUSES,
    ORDER_STATUS_CANCELLED,
    ORDER_STATUS_PENDING,
    ORDER_STATUS_REFUNDING,
    REFUND_OPERATION_COMPLETED,
    SUBSCRIPTION_STATUS_DISABLED,
)
from vpn_access_bot.models import (
    AccessOperationLease,
    AuditEvent,
    DiscountRedemption,
    EntitlementOperation,
    OnboardingSession,
    Order,
    PaymentInbox,
    PurchaseQuote,
    ReferralReward,
    RefundOperation,
    TestUserResetOperation,
    TrialClaim,
    UserDiscount,
    utc_now,
)
from vpn_access_bot.repositories import (
    AccessOperationLeaseRepository,
    SubscriptionRepository,
    UserRepository,
)


@dataclass(frozen=True)
class TestUserResetOutcome:
    telegram_id: int
    archived_subscriptions: int
    cancelled_orders: int
    consumed_quotes: int
    revoked_discounts: int
    removed_trial_claims: int
    cancelled_referral_rewards: int


@dataclass(frozen=True)
class TestUserResetPlan:
    user_id: int
    telegram_id: int
    subscription_public_guids_to_disable: tuple[str, ...]
    completed_outcome: TestUserResetOutcome | None = None


class TestUserResetService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def prepare(
        self,
        telegram_id: int,
        *,
        actor_telegram_id: int | None = None,
        source_request_id: str | None = None,
    ) -> TestUserResetPlan:
        operation = await self._ensure_operation(
            telegram_id,
            actor_telegram_id=actor_telegram_id,
            source_request_id=source_request_id,
        )
        user = await UserRepository(self._session).get_by_telegram_id(telegram_id)
        if user is None:
            raise ValueError("test_user_not_found")

        if operation is not None and operation.state == "completed":
            return TestUserResetPlan(
                user.id,
                telegram_id,
                (),
                completed_outcome=self._parse_outcome(operation.outcome_json),
            )

        await self._ensure_no_conflicting_operations(
            user_id=user.id,
            telegram_id=telegram_id,
        )

        subscriptions = await SubscriptionRepository(self._session).list_visible_for_user(user.id)
        to_disable = tuple(
            subscription.public_guid
            for subscription in subscriptions
            if subscription.status != SUBSCRIPTION_STATUS_DISABLED
        )
        return TestUserResetPlan(user.id, telegram_id, to_disable)

    async def finalize(
        self,
        telegram_id: int,
        *,
        actor_telegram_id: int,
        source_request_id: str,
    ) -> TestUserResetOutcome:
        operation = await self._ensure_operation(
            telegram_id,
            actor_telegram_id=actor_telegram_id,
            source_request_id=source_request_id,
        )
        if operation is None:
            raise ValueError("test_user_reset_operation_missing")
        if operation.state == "completed":
            return self._parse_outcome(operation.outcome_json)

        user = await UserRepository(self._session).get_by_telegram_id(telegram_id)
        if user is None:
            raise ValueError("test_user_not_found")

        reset_owner_key = f"test-user-reset:{operation.id}"
        lease_repository = AccessOperationLeaseRepository(self._session)
        if not await lease_repository.acquire(
            user_id=user.id,
            owner_kind="test_user_reset",
            owner_key=reset_owner_key,
            lease_seconds=300,
        ):
            raise ValueError("test_user_reset_has_active_access_lease")

        await self._ensure_no_conflicting_operations(
            user_id=user.id,
            telegram_id=telegram_id,
            ignored_lease_owner_key=reset_owner_key,
        )

        subscriptions = await SubscriptionRepository(self._session).list_visible_for_user(user.id)
        if any(
            subscription.status != SUBSCRIPTION_STATUS_DISABLED for subscription in subscriptions
        ):
            raise ValueError("test_user_reset_subscription_not_disabled")

        now = utc_now()
        pending_result = await self._session.execute(
            select(Order).where(
                Order.user_id == user.id,
                Order.status == ORDER_STATUS_PENDING,
            )
        )
        pending_orders = list(pending_result.scalars().all())
        for order in pending_orders:
            order.status = ORDER_STATUS_CANCELLED
            order.cancelled_at_utc = now
            redemption_result = await self._session.execute(
                select(DiscountRedemption).where(DiscountRedemption.order_id == order.id)
            )
            redemption = redemption_result.scalar_one_or_none()
            if redemption is not None and redemption.status == "reserved":
                redemption.status = "released"
                redemption.released_at_utc = now

        quote_result = await self._session.execute(
            select(PurchaseQuote).where(
                PurchaseQuote.user_id == user.id,
                PurchaseQuote.consumed_at_utc.is_(None),
            )
        )
        quotes = list(quote_result.scalars().all())
        for quote in quotes:
            quote.consumed_at_utc = now

        discount_result = await self._session.execute(
            select(UserDiscount).where(
                UserDiscount.user_id == user.id,
                UserDiscount.status == "active",
            )
        )
        discounts = list(discount_result.scalars().all())
        for discount in discounts:
            discount.status = "revoked"
            discount.revoked_at_utc = now
            discount.revoked_by_admin_telegram_id = actor_telegram_id

        onboarding_result = await self._session.execute(
            select(OnboardingSession).where(
                OnboardingSession.user_id == user.id,
                OnboardingSession.status.not_in(["completed", "cancelled"]),
            )
        )
        for onboarding in onboarding_result.scalars().all():
            onboarding.status = "cancelled"
            onboarding.completed_at_utc = now
            onboarding.updated_at_utc = now
            onboarding.last_error_code = "test_user_reset"

        trial_delete = await self._session.execute(
            delete(TrialClaim).where(TrialClaim.user_id == user.id)
        )
        referral_result = await self._session.execute(
            select(ReferralReward).where(
                ReferralReward.referrer_user_id == user.id,
                ReferralReward.status.in_(["pending_hold", "available", "failed"]),
            )
        )
        referral_rewards = list(referral_result.scalars().all())
        for reward in referral_rewards:
            reward.status = "cancelled"
            reward.cancelled_at_utc = now
            reward.failure_code = "test_user_reset"

        await lease_repository.release(
            user_id=user.id,
            owner_key=reset_owner_key,
        )

        for subscription in subscriptions:
            subscription.test_reset_at_utc = now
            subscription.updated_at_utc = now

        user.primary_subscription_id = None
        user.platform_preference = None
        user.test_user_reset_generation += 1
        user.test_user_reset_at_utc = now
        user.updated_at = now

        outcome = TestUserResetOutcome(
            telegram_id=telegram_id,
            archived_subscriptions=len(subscriptions),
            cancelled_orders=len(pending_orders),
            consumed_quotes=len(quotes),
            revoked_discounts=len(discounts),
            removed_trial_claims=int(trial_delete.rowcount or 0),
            cancelled_referral_rewards=len(referral_rewards),
        )
        outcome_json = json.dumps(asdict(outcome), ensure_ascii=False, sort_keys=True)
        self._session.add(
            AuditEvent(
                created_at_utc=now,
                event_type="test.user_reset_completed",
                telegram_id=telegram_id,
                details_json=json.dumps(
                    {
                        "actor_telegram_id": actor_telegram_id,
                        "source_request_id": source_request_id,
                        **asdict(outcome),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        operation.state = "completed"
        operation.outcome_json = outcome_json
        operation.completed_at_utc = now
        await self._session.flush()
        return outcome

    async def _ensure_no_conflicting_operations(
        self,
        *,
        user_id: int,
        telegram_id: int,
        ignored_lease_owner_key: str | None = None,
    ) -> None:
        unsafe_order_result = await self._session.execute(
            select(Order.id).where(
                Order.user_id == user_id,
                Order.status.in_([*ORDER_ACTIVATION_RETRY_STATUSES, ORDER_STATUS_REFUNDING]),
            )
        )
        if unsafe_order_result.first() is not None:
            raise ValueError("test_user_reset_has_inflight_order")

        active_entitlement_result = await self._session.execute(
            select(EntitlementOperation.id).where(
                EntitlementOperation.user_id == user_id,
                EntitlementOperation.state.in_(ENTITLEMENT_OPERATION_ACTIVE_STATES),
            )
        )
        if active_entitlement_result.first() is not None:
            raise ValueError("test_user_reset_has_active_entitlement_operation")

        active_refund_result = await self._session.execute(
            select(RefundOperation.id).where(
                RefundOperation.user_id == user_id,
                RefundOperation.state != REFUND_OPERATION_COMPLETED,
            )
        )
        if active_refund_result.first() is not None:
            raise ValueError("test_user_reset_has_active_refund_operation")

        lease_conditions = [
            AccessOperationLease.user_id == user_id,
            AccessOperationLease.lease_expires_at_utc > utc_now(),
        ]
        if ignored_lease_owner_key is not None:
            lease_conditions.append(AccessOperationLease.owner_key != ignored_lease_owner_key)
        active_lease_result = await self._session.execute(
            select(AccessOperationLease.user_id).where(*lease_conditions)
        )
        if active_lease_result.first() is not None:
            raise ValueError("test_user_reset_has_active_access_lease")

        applying_reward_result = await self._session.execute(
            select(ReferralReward.id).where(
                ReferralReward.referrer_user_id == user_id,
                ReferralReward.status == "applying",
            )
        )
        if applying_reward_result.first() is not None:
            raise ValueError("test_user_reset_has_applying_referral_reward")

        unmatched_payment_result = await self._session.execute(
            select(PaymentInbox.id).where(
                PaymentInbox.payer_external_id == str(telegram_id),
                PaymentInbox.reconciliation_status.in_(["received", "matched"]),
            )
        )
        if unmatched_payment_result.first() is not None:
            raise ValueError("test_user_reset_has_unreconciled_payment")

    async def _ensure_operation(
        self,
        telegram_id: int,
        *,
        actor_telegram_id: int | None,
        source_request_id: str | None,
    ) -> TestUserResetOperation | None:
        if source_request_id is None:
            return None
        if actor_telegram_id is None:
            raise ValueError("test_user_reset_actor_missing")
        normalized_source = source_request_id.strip()
        if not normalized_source:
            raise ValueError("test_user_reset_operation_missing")

        now = utc_now()
        await self._session.execute(
            sqlite_insert(TestUserResetOperation)
            .values(
                source_request_id=normalized_source,
                target_telegram_id=telegram_id,
                actor_telegram_id=actor_telegram_id,
                state="pending",
                outcome_json=None,
                created_at_utc=now,
                completed_at_utc=None,
            )
            .on_conflict_do_nothing(index_elements=[TestUserResetOperation.source_request_id])
        )
        result = await self._session.execute(
            select(TestUserResetOperation).where(
                TestUserResetOperation.source_request_id == normalized_source
            )
        )
        operation = result.scalar_one()
        if (
            operation.target_telegram_id != telegram_id
            or operation.actor_telegram_id != actor_telegram_id
        ):
            raise ValueError("test_user_reset_operation_conflict")
        return operation

    @staticmethod
    def _parse_outcome(payload: str | None) -> TestUserResetOutcome:
        if payload is None:
            raise ValueError("test_user_reset_outcome_missing")
        raw = json.loads(payload)
        return TestUserResetOutcome(
            telegram_id=int(raw["telegram_id"]),
            archived_subscriptions=int(raw["archived_subscriptions"]),
            cancelled_orders=int(raw["cancelled_orders"]),
            consumed_quotes=int(raw["consumed_quotes"]),
            revoked_discounts=int(raw["revoked_discounts"]),
            removed_trial_claims=int(raw["removed_trial_claims"]),
            cancelled_referral_rewards=int(raw.get("cancelled_referral_rewards", 0)),
        )
