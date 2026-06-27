from __future__ import annotations

from dataclasses import replace

from aiogram.types import User as TelegramUser

from vpn_access_bot.commerce import CabinetState, CabinetStateBuilder
from vpn_access_bot.config import Settings
from vpn_access_bot.db import Database
from vpn_access_bot.mediator_client import MediatorClient, MediatorClientError
from vpn_access_bot.readiness import CommerceReadinessService
from vpn_access_bot.repositories import OrderRepository, SubscriptionRepository, UserRepository
from vpn_access_bot.trial import (
    TrialEligibilityReason,
    TrialEligibilityService,
)


class HomeStateService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        mediator_client: MediatorClient,
        readiness_service: CommerceReadinessService,
    ) -> None:
        self._database = database
        self._settings = settings
        self._mediator_client = mediator_client
        self._readiness_service = readiness_service

    async def build(self, telegram_user: TelegramUser) -> CabinetState:
        async with self._database.session() as session:
            user = await UserRepository(session).get_or_create_from_message_user(
                telegram_id=telegram_user.id,
                username=telegram_user.username,
                first_name=telegram_user.first_name,
            )
            subscription = await SubscriptionRepository(session).get_primary_for_user(user)
            latest_order = await OrderRepository(session).get_relevant_unfinished_for_user(user.id)
            trial_eligibility = await TrialEligibilityService(
                session,
                self._settings,
            ).evaluate(user, subscription)

        readiness = await self._readiness_service.check()
        if not readiness.can_sell and trial_eligibility.is_available:
            trial_eligibility = replace(
                trial_eligibility,
                is_available=False,
                reason=TrialEligibilityReason.SERVICE_UNAVAILABLE,
            )

        active_device_tokens: int | None = 0
        mediator_available = True
        if subscription is not None:
            try:
                details = await self._mediator_client.get_subscription(subscription.public_guid)
            except MediatorClientError:
                mediator_available = False
                active_device_tokens = None
            else:
                active_device_tokens = details.active_device_count

        return CabinetStateBuilder().build(
            subscription=subscription,
            latest_order=latest_order,
            trial_eligibility=trial_eligibility,
            active_device_tokens=active_device_tokens,
            mediator_available=mediator_available,
            commerce_available=readiness.can_sell,
        )
