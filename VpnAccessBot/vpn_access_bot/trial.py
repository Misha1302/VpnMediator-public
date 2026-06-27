from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from vpn_access_bot.config import Settings
from vpn_access_bot.constants import (
    SUBSCRIPTION_STATUS_ACTIVE,
    TRIAL_STATUS_ACTIVATING,
    TRIAL_STATUS_ACTIVATION_FAILED,
)
from vpn_access_bot.models import Subscription, TrialClaim, User, utc_now
from vpn_access_bot.product_completion import user_has_paid_history
from vpn_access_bot.repositories import TrialClaimRepository, to_aware_utc


class TrialEligibilityReason(StrEnum):
    AVAILABLE = "available"
    ALREADY_USED = "already_used"
    PAID_HISTORY_EXISTS = "paid_history_exists"
    ACTIVE_SUBSCRIPTION_EXISTS = "active_subscription_exists"
    ACTIVATION_IN_PROGRESS = "activation_in_progress"
    RETRY_FAILED_ACTIVATION = "retry_failed_activation"
    FEATURE_DISABLED = "feature_disabled"
    SERVICE_UNAVAILABLE = "service_unavailable"


@dataclass(frozen=True)
class TrialEligibility:
    is_available: bool
    can_retry_failed_activation: bool
    reason: TrialEligibilityReason
    existing_claim_id: int | None = None
    existing_subscription_id: int | None = None


class TrialEligibilityService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def evaluate(
        self,
        user: User,
        subscription: Subscription | None,
    ) -> TrialEligibility:
        if not self._settings.trial_enabled:
            return TrialEligibility(False, False, TrialEligibilityReason.FEATURE_DISABLED)

        claim = await TrialClaimRepository(self._session).get_for_user(user.id)
        paid_history_exists = await user_has_paid_history(
            self._session,
            user.id,
            after_utc=user.test_user_reset_at_utc,
        )
        if claim is not None and claim.status != TRIAL_STATUS_ACTIVATION_FAILED:
            return self._from_existing_claim(claim)
        if paid_history_exists:
            return TrialEligibility(False, False, TrialEligibilityReason.PAID_HISTORY_EXISTS)

        if (
            subscription is not None
            and subscription.status == SUBSCRIPTION_STATUS_ACTIVE
            and to_aware_utc(subscription.expires_at) > utc_now()
            and (claim is None or claim.subscription_id != subscription.id)
        ):
            return TrialEligibility(
                False,
                False,
                TrialEligibilityReason.ACTIVE_SUBSCRIPTION_EXISTS,
                existing_subscription_id=subscription.id,
            )

        if claim is not None:
            return self._from_existing_claim(claim)
        return TrialEligibility(True, False, TrialEligibilityReason.AVAILABLE)

    @staticmethod
    def _from_existing_claim(claim: TrialClaim) -> TrialEligibility:
        if claim.status == TRIAL_STATUS_ACTIVATION_FAILED:
            return TrialEligibility(
                True,
                True,
                TrialEligibilityReason.RETRY_FAILED_ACTIVATION,
                existing_claim_id=claim.id,
                existing_subscription_id=claim.subscription_id,
            )
        if claim.status == TRIAL_STATUS_ACTIVATING:
            return TrialEligibility(
                False,
                False,
                TrialEligibilityReason.ACTIVATION_IN_PROGRESS,
                existing_claim_id=claim.id,
                existing_subscription_id=claim.subscription_id,
            )
        return TrialEligibility(
            False,
            False,
            TrialEligibilityReason.ALREADY_USED,
            existing_claim_id=claim.id,
            existing_subscription_id=claim.subscription_id,
        )
