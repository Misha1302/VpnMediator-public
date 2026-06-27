from __future__ import annotations

import asyncio

from aiogram import Dispatcher

from vpn_access_bot.access_boundary import PrivateCustomerBoundaryMiddleware
from vpn_access_bot.checkout_web import CheckoutWebServer
from vpn_access_bot.config import get_settings
from vpn_access_bot.correlation import CorrelationIdMiddleware
from vpn_access_bot.db import Database
from vpn_access_bot.handlers import (
    admin,
    admin_test_purchase,
    buy,
    common,
    onboarding,
    payments,
    product_completion,
    subscription,
)
from vpn_access_bot.health import BotHealthServer
from vpn_access_bot.logging_config import configure_logging
from vpn_access_bot.mediator_client import MediatorClient
from vpn_access_bot.product_catalog import ProductCatalog
from vpn_access_bot.product_completion import run_product_workers
from vpn_access_bot.readiness import CommerceReadinessService
from vpn_access_bot.repositories import TelegramChannelRepository
from vpn_access_bot.runtime import SingleInstanceGuard
from vpn_access_bot.services import run_expiration_worker
from vpn_access_bot.telegram import BotIdentityMiddleware, BotRegistry, NotificationSender
from vpn_access_bot.telegram.reliable_polling import ReliablePollingRunner
from vpn_access_bot.user_operation_serialization import (
    UserOperationSerializationMiddleware,
)
from vpn_access_bot.yookassa import YooKassaClient


async def _register_verified_bots(database: Database, registry: BotRegistry) -> None:
    async with database.session() as session:
        repository = TelegramChannelRepository(session)
        for runtime in registry.runtimes:
            if (
                runtime.telegram_bot_id is None
                or runtime.username is None
                or runtime.last_verified_at_utc is None
            ):
                continue
            await repository.register_bot(
                bot_key=runtime.key,
                telegram_bot_id=runtime.telegram_bot_id,
                username=runtime.username,
                enabled=runtime.definition.enabled,
                required=runtime.definition.required,
                verified_at_utc=runtime.last_verified_at_utc,
            )


async def main() -> None:
    settings = get_settings()
    logging_runtime = configure_logging(settings)
    instance_guard = SingleInstanceGuard(settings.instance_lock_path)
    database: Database | None = None
    mediator_client: MediatorClient | None = None
    health_server: BotHealthServer | None = None
    registry: BotRegistry | None = None
    checkout_server: CheckoutWebServer | None = None
    yookassa_client: YooKassaClient | None = None
    tasks: list[asyncio.Task] = []

    try:
        ProductCatalog.from_settings(settings)
        instance_guard.acquire()

        database = Database(settings.database_url)
        await database.initialize()

        registry = BotRegistry(settings.telegram_bot_definitions())
        await registry.initialize()
        await _register_verified_bots(database, registry)

        mediator_client = MediatorClient(
            base_url=settings.mediator_base_url,
            admin_token=settings.mediator_admin_token,
            public_subscription_base_url=settings.public_subscription_base_url,
        )
        readiness_service = CommerceReadinessService(
            mediator_client,
            settings.readiness_cache_seconds,
            database.session_factory,
            settings,
        )
        health_server = BotHealthServer(
            settings.health_bind_host,
            settings.health_bind_port,
            database,
            readiness_service,
            registry,
        )
        await health_server.start()

        if settings.yookassa_integration_enabled:
            yookassa_client = YooKassaClient(
                settings.yookassa_shop_id,
                settings.yookassa_secret_key.get_secret_value(),
                base_url=settings.yookassa_api_base_url,
                timeout_seconds=settings.yookassa_request_timeout_seconds,
            )
            checkout_server = CheckoutWebServer(
                settings=settings,
                database=database,
                mediator_client=mediator_client,
                readiness=readiness_service,
                yookassa=yookassa_client,
            )
            await checkout_server.start()

        dispatcher = Dispatcher()
        dispatcher.update.outer_middleware(BotIdentityMiddleware(registry))
        dispatcher.update.outer_middleware(CorrelationIdMiddleware())
        dispatcher.update.outer_middleware(PrivateCustomerBoundaryMiddleware(settings))
        dispatcher.update.outer_middleware(UserOperationSerializationMiddleware())
        dispatcher.include_router(product_completion.router)
        dispatcher.include_router(onboarding.router)
        dispatcher.include_router(buy.router)
        dispatcher.include_router(payments.router)
        dispatcher.include_router(subscription.router)
        dispatcher.include_router(admin.router)
        dispatcher.include_router(admin_test_purchase.router)
        dispatcher.include_router(common.router)

        notification_sender = NotificationSender(
            registry, database.session, settings.default_bot_key
        )
        tasks = [
            asyncio.create_task(
                run_expiration_worker(
                    session_factory=database.session,
                    mediator_client=mediator_client,
                    interval_seconds=settings.expiration_check_interval_seconds,
                    failure_limit=settings.critical_worker_failure_limit,
                ),
                name="expiration-worker",
            ),
            asyncio.create_task(
                run_product_workers(
                    session_factory=database.session,
                    mediator_client=mediator_client,
                    bot=notification_sender,
                    settings=settings,
                ),
                name="product-workers",
            ),
            asyncio.create_task(
                ReliablePollingRunner(
                    dispatcher=dispatcher,
                    registry=registry,
                    database=database,
                    settings=settings,
                    workflow_data={
                        "database": database,
                        "settings": settings,
                        "mediator_client": mediator_client,
                        "readiness_service": readiness_service,
                        "bot_registry": registry,
                    },
                ).run(),
                name="telegram-polling",
            ),
        ]

        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if health_server is not None:
            await health_server.close()
        if checkout_server is not None:
            await checkout_server.close()
        if yookassa_client is not None:
            await yookassa_client.close()
        if mediator_client is not None:
            await mediator_client.close()
        if database is not None:
            await database.dispose()
        if registry is not None:
            await registry.close()
        instance_guard.release()
        logging_runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
