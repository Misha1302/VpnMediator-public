from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from vpn_access_bot.db import Database
from vpn_access_bot.migrations import (
    _add_commercial_columns,
    _commercial_schema_sql,
    _execute_many,
    _idempotency_schema_sql,
    _initial_schema_sql,
    run_migrations,
)


@pytest.mark.asyncio
async def test_sqlite_foreign_keys_are_enforced(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()

    try:
        with pytest.raises(IntegrityError):
            async with database.session() as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO orders (
                            user_id, status, amount_minor_units, currency, provider,
                            invoice_payload, created_at
                        )
                        VALUES (
                            999, 'pending', 100, 'XTR', 'telegram_stars',
                            'payload-missing-user', datetime('now')
                        )
                        """
                    )
                )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_quote_can_create_only_one_order(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, telegram_id, created_at, updated_at)
                    VALUES (1, 100, datetime('now'), datetime('now'))
                    """
                )
            )
            await session.execute(
                text(
                    """
                    INSERT INTO purchase_quotes (
                        id, public_quote_id, user_id, period_count, duration_days,
                        max_devices, amount_minor_units, currency, pricing_version,
                        order_kind, expires_at_utc, created_at_utc
                    )
                    VALUES (
                        1, 'quote-1', 1, 1, 30, 1, 100, 'XTR',
                        'test', 'purchase', datetime('now', '+1 hour'), datetime('now')
                    )
                    """
                )
            )
            await session.execute(
                text(
                    """
                    INSERT INTO orders (
                        user_id, quote_id, status, amount_minor_units, currency,
                        provider, invoice_payload, created_at
                    )
                    VALUES (
                        1, 1, 'pending', 100, 'XTR',
                        'telegram_stars', 'payload-1', datetime('now')
                    )
                    """
                )
            )

        with pytest.raises(IntegrityError):
            async with database.session() as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO orders (
                            user_id, quote_id, status, amount_minor_units, currency,
                            provider, invoice_payload, created_at
                        )
                        VALUES (
                            1, 1, 'pending', 100, 'XTR',
                            'telegram_stars', 'payload-2', datetime('now')
                        )
                        """
                    )
                )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_product_completion_migration_preserves_existing_commercial_data(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "old-bot.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")

    try:
        async with engine.begin() as connection:
            await connection.execute(text("PRAGMA foreign_keys = ON"))
            await connection.execute(
                text(
                    """
                    CREATE TABLE schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at_utc TEXT NOT NULL
                    )
                    """
                )
            )
            await _execute_many(connection, _initial_schema_sql())
            await connection.execute(
                text(
                    """
                    INSERT INTO schema_migrations(version, name, applied_at_utc)
                    VALUES(1, 'initial_schema', datetime('now'))
                    """
                )
            )
            await _add_commercial_columns(connection)
            await _execute_many(connection, _commercial_schema_sql())
            await connection.execute(
                text(
                    """
                    INSERT INTO schema_migrations(version, name, applied_at_utc)
                    VALUES(2, 'commercial_entitlement_quotes_orders', datetime('now'))
                    """
                )
            )
            await _execute_many(connection, _idempotency_schema_sql())
            await connection.execute(
                text(
                    """
                    INSERT INTO schema_migrations(version, name, applied_at_utc)
                    VALUES(3, 'order_payment_idempotency_indexes', datetime('now'))
                    """
                )
            )

            await connection.execute(
                text(
                    """
                    INSERT INTO users(id, telegram_id, username, first_name, created_at, updated_at)
                    VALUES(1, 1001, 'paid', 'Paid', datetime('now'), datetime('now')),
                          (2, 1002, 'new', 'New', datetime('now'), datetime('now'))
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO subscriptions(
                        id, user_id, public_guid, signed_url, max_devices, status,
                        starts_at, expires_at, created_at, updated_at_utc
                    )
                    VALUES(
                        10, 1, '00000000-0000-0000-0000-000000000010',
                        '', 3, 'active', datetime('now', '-1 day'),
                        datetime('now', '+29 days'), datetime('now', '-1 day'), datetime('now')
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO orders(
                        id, public_order_id, user_id, status, amount_minor_units, currency,
                        provider, provider_payment_id, invoice_payload, created_at, paid_at,
                        target_subscription_id, order_kind
                    )
                    VALUES(
                        20, 'paid-order-20', 1, 'paid', 199, 'XTR',
                        'telegram_stars', 'charge-20', 'payload-20', datetime('now'),
                        datetime('now'), 10, 'purchase'
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO order_applications(
                        id, order_id, subscription_id, applied_at_utc, duration_days,
                        selected_max_devices, resulting_valid_until_utc,
                        resulting_entitlement_version
                    )
                    VALUES(30, 20, 10, datetime('now'), 30, 3, datetime('now', '+29 days'), 1)
                    """
                )
            )

            await run_migrations(connection)
            await run_migrations(connection)

            user_count = await connection.execute(text("SELECT COUNT(*) FROM users"))
            trial_count = await connection.execute(text("SELECT COUNT(*) FROM trial_claims"))
            primary_subscription = await connection.execute(
                text("SELECT primary_subscription_id FROM users WHERE id = 1")
            )
            new_user_primary = await connection.execute(
                text("SELECT primary_subscription_id FROM users WHERE id = 2")
            )
            referral_codes = await connection.execute(
                text("SELECT COUNT(*) FROM users WHERE referral_code IS NOT NULL")
            )
            application_subscription = await connection.execute(
                text("SELECT subscription_id FROM order_applications WHERE order_id = 20")
            )
            migration_rows = await connection.execute(
                text("SELECT COUNT(*) FROM schema_migrations WHERE version = 4")
            )

            assert user_count.scalar_one() == 2
            assert trial_count.scalar_one() == 0
            assert primary_subscription.scalar_one() == 10
            assert new_user_primary.scalar_one() is None
            assert referral_codes.scalar_one() == 2
            assert application_subscription.scalar_one() == 10
            assert migration_rows.scalar_one() == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_parallel_user_upsert_creates_exactly_one_user(tmp_path: Path) -> None:
    import asyncio

    from vpn_access_bot.repositories import UserRepository

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()

    async def upsert(index: int) -> tuple[int, str | None]:
        async with database.session() as session:
            user = await UserRepository(session).get_or_create_from_message_user(
                telegram_id=777,
                username=f"user-{index}",
                first_name="Concurrent",
            )
            return user.id, user.referral_code

    try:
        results = await asyncio.gather(*(upsert(index) for index in range(20)))
        ids = {item[0] for item in results}
        referral_codes = {item[1] for item in results}

        async with database.session() as session:
            count = await session.execute(
                text("SELECT COUNT(*) FROM users WHERE telegram_id = 777")
            )

        assert len(ids) == 1
        assert len(referral_codes) == 1
        assert None not in referral_codes
        assert count.scalar_one() == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_parallel_trial_acquisition_creates_one_claim(tmp_path: Path) -> None:
    import asyncio

    from vpn_access_bot.repositories import TrialClaimRepository, UserRepository

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            user = await UserRepository(session).get_or_create_from_message_user(
                telegram_id=778,
                username="trial-user",
                first_name="Trial",
            )
            user_id = user.id

        async def acquire() -> bool:
            async with database.session() as session:
                current_user = await UserRepository(session).get_by_id(user_id)
                assert current_user is not None
                _, acquired = await TrialClaimRepository(session).acquire_activation(
                    current_user,
                    duration_seconds=2 * 86400,
                    max_devices=1,
                )
                return acquired

        acquired_results = await asyncio.gather(*(acquire() for _ in range(10)))

        async with database.session() as session:
            count = await session.execute(
                text("SELECT COUNT(*) FROM trial_claims WHERE user_id = :user_id"),
                {"user_id": user_id},
            )

        assert sum(acquired_results) == 1
        assert count.scalar_one() == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_paid_history_atomically_blocks_new_trial_claim(tmp_path: Path) -> None:
    from vpn_access_bot.models import Order, utc_now
    from vpn_access_bot.repositories import TrialClaimRepository, UserRepository

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            user = await UserRepository(session).get_or_create_from_message_user(
                telegram_id=779,
                username="paid-user",
                first_name="Paid",
            )
            session.add(
                Order(
                    user_id=user.id,
                    status="payment_received",
                    amount_minor_units=60,
                    currency="XTR",
                    provider="telegram_stars",
                    provider_payment_id="paid-before-trial",
                    invoice_payload="paid-before-trial-payload",
                    paid_at=utc_now(),
                    created_at=utc_now(),
                )
            )

        async with database.session() as session:
            user = await UserRepository(session).get_by_telegram_id(779)
            assert user is not None
            claim, acquired = await TrialClaimRepository(session).acquire_activation(
                user,
                duration_seconds=2 * 86400,
                max_devices=1,
            )

        assert acquired is False
        assert claim is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_pre_reset_paid_history_does_not_block_new_trial_claim(tmp_path: Path) -> None:
    from datetime import timedelta

    from vpn_access_bot.models import Order, utc_now
    from vpn_access_bot.repositories import TrialClaimRepository, UserRepository

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'trial-reset-epoch.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            now = utc_now()
            user = await UserRepository(session).get_or_create_from_message_user(
                telegram_id=7801,
                username="reset-paid-user",
                first_name="Reset",
            )
            session.add(
                Order(
                    user_id=user.id,
                    status="paid",
                    amount_minor_units=60,
                    currency="XTR",
                    provider="telegram_stars",
                    provider_payment_id="pre-reset-payment",
                    invoice_payload="pre-reset-payment-payload",
                    paid_at=now - timedelta(minutes=2),
                    created_at=now - timedelta(minutes=3),
                )
            )
            user.test_user_reset_generation = 1
            user.test_user_reset_at_utc = now - timedelta(minutes=1)
            user_id = user.id

        async with database.session() as session:
            user = await UserRepository(session).get_by_id(user_id)
            assert user is not None
            claim, acquired = await TrialClaimRepository(session).acquire_activation(
                user,
                duration_seconds=2 * 86400,
                max_devices=1,
            )

        assert acquired is True
        assert claim is not None
        assert claim.idempotency_key == f"trial:{user_id}:reset:1"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_post_reset_paid_history_atomically_blocks_new_trial_claim(tmp_path: Path) -> None:
    from datetime import timedelta

    from vpn_access_bot.models import Order, utc_now
    from vpn_access_bot.repositories import TrialClaimRepository, UserRepository

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'trial-reset-new-payment.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            now = utc_now()
            user = await UserRepository(session).get_or_create_from_message_user(
                telegram_id=7802,
                username="post-reset-paid-user",
                first_name="Paid",
            )
            user.test_user_reset_generation = 1
            user.test_user_reset_at_utc = now - timedelta(minutes=2)
            session.add(
                Order(
                    user_id=user.id,
                    status="paid",
                    amount_minor_units=60,
                    currency="XTR",
                    provider="telegram_stars",
                    provider_payment_id="post-reset-payment",
                    invoice_payload="post-reset-payment-payload",
                    paid_at=now - timedelta(minutes=1),
                    created_at=now - timedelta(minutes=1),
                )
            )
            user_id = user.id

        async with database.session() as session:
            user = await UserRepository(session).get_by_id(user_id)
            assert user is not None
            claim, acquired = await TrialClaimRepository(session).acquire_activation(
                user,
                duration_seconds=2 * 86400,
                max_devices=1,
            )

        assert acquired is False
        assert claim is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_zero_price_order_does_not_atomically_block_trial(tmp_path: Path) -> None:
    from vpn_access_bot.models import Order, utc_now
    from vpn_access_bot.repositories import TrialClaimRepository, UserRepository

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'zero-price-trial.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            user = await UserRepository(session).get_or_create_from_message_user(
                telegram_id=7803,
                username="zero-price-user",
                first_name="Zero",
            )
            session.add(
                Order(
                    user_id=user.id,
                    status="paid",
                    amount_minor_units=0,
                    currency="XTR",
                    provider="telegram_stars",
                    invoice_payload="zero-price-order",
                    paid_at=utc_now(),
                    created_at=utc_now(),
                )
            )
            user_id = user.id

        async with database.session() as session:
            user = await UserRepository(session).get_by_id(user_id)
            assert user is not None
            claim, acquired = await TrialClaimRepository(session).acquire_activation(
                user,
                duration_seconds=2 * 86400,
                max_devices=1,
            )

        assert acquired is True
        assert claim is not None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_onboarding_completion_is_idempotent(tmp_path: Path) -> None:
    from vpn_access_bot.models import Subscription, User, utc_now
    from vpn_access_bot.repositories import OnboardingSessionRepository

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            now = utc_now()
            user = User(
                telegram_id=780,
                referral_code="onboarding-user",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.flush()
            subscription = Subscription(
                user_id=user.id,
                public_guid="00000000-0000-0000-0000-000000000780",
                signed_url="",
                max_devices=1,
                status="active",
                starts_at=now,
                expires_at=now,
                created_at=now,
                updated_at_utc=now,
            )
            session.add(subscription)
            await session.flush()
            repository = OnboardingSessionRepository(session)
            onboarding = await repository.start_or_update(
                user=user,
                subscription=subscription,
                platform="android",
                current_step="waiting_activation",
                status="waiting_activation",
            )
            issuance_request_id = onboarding.issuance_request_id
            retried = await repository.start_or_update(
                user=user,
                subscription=subscription,
                platform="android",
                current_step="waiting_activation",
                status="waiting_activation",
            )
            first = await repository.mark_completed(onboarding.id, "device-public-id")
            completed_at = onboarding.completed_at_utc
            second = await repository.mark_completed(onboarding.id, "device-public-id")
            open_session = await repository.get_open_for_user(user.id)

        assert issuance_request_id is not None
        assert retried.id == onboarding.id
        assert retried.issuance_request_id == issuance_request_id
        assert first is True
        assert second is False
        assert onboarding.completed_at_utc == completed_at
        assert open_session is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_fresh_database_applies_all_current_migrations(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            version = await session.execute(text("SELECT MAX(version) FROM schema_migrations"))
            lease_table = await session.execute(
                text(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'access_operation_leases'"
                )
            )
            event_table = await session.execute(
                text(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'product_events'"
                )
            )
            retired_legal_consent_table = await session.execute(
                text(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'legal_consents'"
                )
            )
            expiration_columns = await session.execute(text("PRAGMA table_info(orders)"))
            expiration_column_names = {row[1] for row in expiration_columns.all()}
            onboarding_columns = await session.execute(
                text("PRAGMA table_info(onboarding_sessions)")
            )
            onboarding_column_names = {row[1] for row in onboarding_columns.all()}
            issuance_index = await session.execute(
                text(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'index' "
                    "AND name = 'ux_onboarding_sessions_issuance_request'"
                )
            )
            bot_channels_table = await session.execute(
                text(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'telegram_bot_channels'"
                )
            )
            user_channels_table = await session.execute(
                text(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'user_bot_channels'"
                )
            )
            reliability_tables = {}
            for table_name in (
                "payment_inbox",
                "telegram_update_inbox",
                "entitlement_operations",
                "refund_operations",
                "notification_outbox",
                "test_user_reset_operations",
                "broadcast_campaigns",
                "broadcast_recipients",
                "commerce_policy",
                "refund_plans",
                "acquisition_campaigns",
                "user_acquisition",
                "acquisition_touches",
                "capacity_state_transitions",
                "commerce_policy_change_requests",
            ):
                table_result = await session.execute(
                    text(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = :name"
                    ),
                    {"name": table_name},
                )
                reliability_tables[table_name] = int(table_result.scalar_one())
            entitlement_columns = await session.execute(
                text("PRAGMA table_info(entitlement_operations)")
            )
            entitlement_column_names = {row[1] for row in entitlement_columns.all()}
            payment_inbox_columns = await session.execute(text("PRAGMA table_info(payment_inbox)"))
            payment_inbox_column_names = {row[1] for row in payment_inbox_columns.all()}
            user_columns = await session.execute(text("PRAGMA table_info(users)"))
            user_column_names = {row[1] for row in user_columns.all()}
            order_columns = await session.execute(text("PRAGMA table_info(orders)"))
            order_column_names = {row[1] for row in order_columns.all()}
            quote_order_index = await session.execute(
                text(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' "
                    "AND name = 'ux_orders_quote_id_not_null'"
                )
            )
            subscription_columns = await session.execute(text("PRAGMA table_info(subscriptions)"))
            subscription_column_names = {row[1] for row in subscription_columns.all()}
            outbox_columns = await session.execute(text("PRAGMA table_info(notification_outbox)"))
            outbox_column_names = {row[1] for row in outbox_columns.all()}
            campaign_columns = await session.execute(text("PRAGMA table_info(broadcast_campaigns)"))
            campaign_column_names = {row[1] for row in campaign_columns.all()}
            commerce_policy_columns = await session.execute(
                text("PRAGMA table_info(commerce_policy)")
            )
            commerce_policy_column_names = {row[1] for row in commerce_policy_columns.all()}
            quote_columns = await session.execute(text("PRAGMA table_info(purchase_quotes)"))
            quote_column_names = {str(row[1]) for row in quote_columns.fetchall()}

        assert version.scalar_one() == 34
        assert "extend_and_upgrade_enabled" in commerce_policy_column_names
        assert lease_table.scalar_one() == 1
        assert event_table.scalar_one() == 1
        assert retired_legal_consent_table.scalar_one() == 0
        assert {
            "base_expires_at_utc",
            "purchased_duration_days",
            "expiration_policy_version",
            "target_expires_at_utc",
            "checkout_authorized_at_utc",
            "checkout_authorized_until_utc",
        } <= expiration_column_names
        assert {"provider_payment_status", "provider_confirmation_url"} <= order_column_names
        assert "remaining_paid_seconds_at_quote" in quote_column_names
        assert quote_order_index.scalar_one() == 1
        assert "issuance_request_id" in onboarding_column_names
        assert issuance_index.scalar_one() == 1
        assert bot_channels_table.scalar_one() == 1
        assert user_channels_table.scalar_one() == 1
        assert reliability_tables == {
            "payment_inbox": 1,
            "telegram_update_inbox": 1,
            "entitlement_operations": 1,
            "refund_operations": 1,
            "notification_outbox": 1,
            "test_user_reset_operations": 1,
            "broadcast_campaigns": 1,
            "broadcast_recipients": 1,
            "commerce_policy": 1,
            "refund_plans": 1,
            "acquisition_campaigns": 1,
            "user_acquisition": 1,
            "acquisition_touches": 1,
            "capacity_state_transitions": 1,
            "commerce_policy_change_requests": 1,
        }
        assert "external_subscription_public_guid" in entitlement_column_names
        assert {
            "invoice_payload",
            "provider_occurred_at_utc",
            "origin_bot_key",
            "attempt_count",
            "last_attempt_at_utc",
            "next_attempt_at_utc",
            "claimed_by",
            "claim_expires_at_utc",
            "payment_bot_key",
        } <= payment_inbox_column_names
        assert {
            "test_user_reset_generation",
            "test_user_reset_at_utc",
        } <= user_column_names
        assert "payment_bot_key" in order_column_names
        assert "test_reset_at_utc" in subscription_column_names
        assert "delivery_bot_key" in outbox_column_names
        assert "broadcast_campaign_id" in outbox_column_names
        assert "source_bot_key" in campaign_column_names
        assert "origin_bot_key" in onboarding_column_names
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_broadcast_campaign_migration_upgrades_existing_v25_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "upgrade-v25.db"
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.initialize()
    try:
        async with database.session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO users (
                        telegram_id, referral_code, created_at, updated_at,
                        test_user_reset_generation
                    ) VALUES (919191, 'upgrade-v25', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)
                    """
                )
            )
            user_id = int(
                (
                    await session.execute(text("SELECT id FROM users WHERE telegram_id = 919191"))
                ).scalar_one()
            )
            await session.execute(
                text(
                    """
                    INSERT INTO notification_outbox (
                        public_id, idempotency_key, user_id, bot_key,
                        notification_kind, payload_json, state, attempt_count,
                        available_at_utc, created_at_utc, updated_at_utc
                    ) VALUES (
                        '00000000-0000-0000-0000-000000919191',
                        'legacy-before-v26', :user_id, 'primary',
                        'legacy_notice', NULL, 'pending', 0,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {"user_id": user_id},
            )
    finally:
        await database.dispose()

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP INDEX IF EXISTS ix_notification_outbox_broadcast_campaign")
        connection.execute("ALTER TABLE notification_outbox DROP COLUMN broadcast_campaign_id")
        connection.execute("DROP TABLE broadcast_recipients")
        connection.execute("DROP TABLE broadcast_campaigns")
        connection.execute("DELETE FROM schema_migrations WHERE version = 26")
        connection.commit()

    upgraded = Database(f"sqlite+aiosqlite:///{database_path}")
    await upgraded.initialize()
    try:
        async with upgraded.session() as session:
            version_26 = int(
                (
                    await session.execute(
                        text("SELECT COUNT(*) FROM schema_migrations WHERE version = 26")
                    )
                ).scalar_one()
            )
            outbox_columns = {
                row[1]
                for row in (
                    await session.execute(text("PRAGMA table_info(notification_outbox)"))
                ).all()
            }
            campaign_columns = {
                row[1]
                for row in (
                    await session.execute(text("PRAGMA table_info(broadcast_campaigns)"))
                ).all()
            }
            legacy_count = int(
                (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM notification_outbox "
                            "WHERE idempotency_key = 'legacy-before-v26'"
                        )
                    )
                ).scalar_one()
            )

        assert version_26 == 1
        assert "broadcast_campaign_id" in outbox_columns
        assert "source_bot_key" in campaign_columns
        assert legacy_count == 1
    finally:
        await upgraded.dispose()


@pytest.mark.asyncio
async def test_device_upgrade_constraint_migration_replaces_legacy_triggers(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()

    try:
        async with database.engine.begin() as connection:
            await connection.execute(text("DELETE FROM schema_migrations WHERE version = 11"))
            await connection.execute(
                text("DROP TRIGGER IF EXISTS trg_quotes_domain_constraints_insert")
            )
            await connection.execute(
                text(
                    """
                    CREATE TRIGGER trg_quotes_domain_constraints_insert
                    BEFORE INSERT ON purchase_quotes
                    WHEN NEW.amount_minor_units < 0
                      OR NEW.duration_days <= 0
                      OR NEW.max_devices <= 0
                    BEGIN
                        SELECT RAISE(ABORT, 'purchase_quotes_domain_constraint_failed');
                    END
                    """
                )
            )

            await run_migrations(connection)

            trigger_sql = await connection.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'trigger' "
                    "AND name = 'trg_quotes_domain_constraints_insert'"
                )
            )
            trigger_definition = str(trigger_sql.scalar_one())
            migration_row = await connection.execute(
                text("SELECT COUNT(*) FROM schema_migrations WHERE version = 11")
            )

            await connection.execute(
                text(
                    """
                    INSERT INTO users(id, telegram_id, created_at, updated_at)
                    VALUES(1, 1101, datetime('now'), datetime('now'))
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO purchase_quotes(
                        public_quote_id, user_id, period_count, duration_days,
                        max_devices, amount_minor_units, currency, pricing_version,
                        order_kind, expires_at_utc, created_at_utc
                    )
                    VALUES(
                        'upgrade-quote', 1, 0, 0, 12, 100, 'XTR', 'test',
                        'upgrade_devices', datetime('now', '+1 hour'), datetime('now')
                    )
                    """
                )
            )

        assert "NEW.order_kind = 'upgrade_devices'" in trigger_definition
        assert "NEW.duration_days <> 0" in trigger_definition
        assert migration_row.scalar_one() == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_active_access_operation_lease_cannot_be_reentered_or_stolen(
    tmp_path: Path,
) -> None:
    from vpn_access_bot.repositories import AccessOperationLeaseRepository, UserRepository

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            user = await UserRepository(session).get_or_create_from_message_user(
                telegram_id=781,
                username="lease-user",
                first_name="Lease",
            )
            user_id = user.id

        async with database.session() as session:
            acquired = await AccessOperationLeaseRepository(session).acquire(
                user_id=user_id,
                owner_kind="order",
                owner_key="order:first",
            )

        async with database.session() as session:
            same_owner_reentry = await AccessOperationLeaseRepository(session).acquire(
                user_id=user_id,
                owner_kind="order",
                owner_key="order:first",
            )

        async with database.session() as session:
            competing_owner = await AccessOperationLeaseRepository(session).acquire(
                user_id=user_id,
                owner_kind="trial",
                owner_key="trial:second",
            )

        assert acquired is True
        assert same_owner_reentry is False
        assert competing_owner is False
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_stale_onboarding_session_is_abandoned(tmp_path: Path) -> None:
    from datetime import timedelta

    from vpn_access_bot.models import OnboardingSession, Subscription, User, utc_now
    from vpn_access_bot.repositories import OnboardingSessionRepository

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            now = utc_now()
            user = User(
                telegram_id=782,
                referral_code="stale-onboarding-user",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.flush()
            subscription = Subscription(
                user_id=user.id,
                public_guid="00000000-0000-0000-0000-000000000782",
                signed_url="",
                max_devices=1,
                status="active",
                starts_at=now,
                expires_at=now + timedelta(days=1),
                created_at=now,
                updated_at_utc=now,
            )
            session.add(subscription)
            await session.flush()
            repository = OnboardingSessionRepository(session)
            onboarding = await repository.start_or_update(
                user=user,
                subscription=subscription,
                platform="android",
                current_step="waiting_activation",
                status="waiting_activation",
            )
            onboarding.updated_at_utc = now - timedelta(hours=73)
            onboarding_id = onboarding.id

        async with database.session() as session:
            abandoned = await OnboardingSessionRepository(session).abandon_stale_sessions(
                utc_now() - timedelta(hours=72)
            )
            stale = await session.get(OnboardingSession, onboarding_id)

        assert abandoned == 1
        assert stale is not None
        assert stale.status == "abandoned"
        assert stale.current_step == "abandoned"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_runtime_migrator_enforces_domain_constraints(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'constraints.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO users(id, telegram_id, created_at, updated_at)
                    VALUES(99, 9099, datetime('now'), datetime('now'))
                    """
                )
            )

        with pytest.raises(IntegrityError, match="orders_domain_constraint_failed"):
            async with database.session() as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO orders(
                            user_id, status, amount_minor_units, currency, provider,
                            invoice_payload, created_at, duration_days,
                            selected_max_devices, order_kind
                        ) VALUES(
                            99, 'pending', -1, 'XTR', 'telegram_stars',
                            'negative-order', datetime('now'), 30, 1, 'purchase'
                        )
                        """
                    )
                )

        with pytest.raises(IntegrityError, match="purchase_quotes_domain_constraint_failed"):
            async with database.session() as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO purchase_quotes(
                            public_quote_id, user_id, period_count, duration_days,
                            max_devices, amount_minor_units, currency, pricing_version,
                            order_kind, expires_at_utc, created_at_utc
                        ) VALUES(
                            'invalid-quote', 99, 1, 30, 0, 10, 'XTR', 'test',
                            'purchase', datetime('now', '+1 hour'), datetime('now')
                        )
                        """
                    )
                )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_notification_migration_preserves_legacy_provider_acceptance_semantics(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'notification-migration.db'}")
    await database.initialize()
    try:
        async with database.engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO users(id, telegram_id, created_at, updated_at)
                    VALUES(501, 90501, datetime('now'), datetime('now'))
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO subscriptions(
                        id, user_id, public_guid, signed_url, max_devices, status,
                        starts_at, expires_at, created_at, updated_at_utc
                    )
                    VALUES(
                        502, 501, '00000000-0000-0000-0000-000000000502', '', 1,
                        'active', datetime('now'), datetime('now', '+1 day'),
                        datetime('now'), datetime('now')
                    )
                    """
                )
            )
            await connection.execute(text("DROP TABLE notification_deliveries"))
            await connection.execute(
                text(
                    """
                    CREATE TABLE notification_deliveries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        subscription_id INTEGER NOT NULL,
                        notification_kind TEXT NOT NULL,
                        delivery_key TEXT NOT NULL,
                        delivery_bot_key TEXT NULL,
                        status TEXT NOT NULL DEFAULT 'delivered',
                        attempt_count INTEGER NOT NULL DEFAULT 1,
                        last_error_code TEXT NULL,
                        claimed_at_utc TEXT NULL,
                        delivered_at_utc TEXT NULL,
                        FOREIGN KEY(subscription_id) REFERENCES subscriptions(id),
                        UNIQUE(subscription_id, notification_kind, delivery_key)
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO notification_deliveries(
                        subscription_id, notification_kind, delivery_key,
                        status, attempt_count, claimed_at_utc, delivered_at_utc
                    )
                    VALUES(
                        502, 'subscription_1d', 'legacy-delivery', 'delivered', 1,
                        '2026-06-01T10:00:00+00:00', '2026-06-01T10:00:01+00:00'
                    )
                    """
                )
            )
            await connection.execute(text("DELETE FROM schema_migrations WHERE version >= 16"))
            await run_migrations(connection)

            row = (
                await connection.execute(
                    text(
                        """
                        SELECT status, send_started_at_utc, provider_accepted_at_utc,
                               delivered_at_utc
                        FROM notification_deliveries
                        WHERE delivery_key = 'legacy-delivery'
                        """
                    )
                )
            ).one()
            assert row.status == "provider_accepted"
            assert row.send_started_at_utc == "2026-06-01T10:00:00+00:00"
            assert row.provider_accepted_at_utc == "2026-06-01T10:00:01+00:00"
            assert row.delivered_at_utc is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_migration_22_adds_test_user_reset_epoch_columns(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'migration-22.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            await session.execute(
                text(
                    "INSERT INTO users (telegram_id, created_at, updated_at) "
                    "VALUES (2200, datetime('now'), datetime('now'))"
                )
            )
            await session.execute(text("DELETE FROM schema_migrations WHERE version = 22"))
            await session.execute(text("ALTER TABLE users DROP COLUMN test_user_reset_at_utc"))
            await session.execute(text("ALTER TABLE users DROP COLUMN test_user_reset_generation"))

        async with database.engine.begin() as connection:
            await run_migrations(connection)

        async with database.session() as session:
            columns = await session.execute(text("PRAGMA table_info(users)"))
            column_names = {row[1] for row in columns.all()}
            version = await session.execute(
                text("SELECT COUNT(*) FROM schema_migrations WHERE version = 22")
            )
            defaults = await session.execute(
                text(
                    "SELECT test_user_reset_generation, test_user_reset_at_utc "
                    "FROM users WHERE telegram_id = 2200"
                )
            )

        assert {
            "test_user_reset_generation",
            "test_user_reset_at_utc",
        } <= column_names
        assert version.scalar_one() == 1
        assert defaults.one() == (0, None)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_migration_21_backfills_payment_bot_and_creates_reset_operations(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'migration-21.db'}")
    await database.initialize()
    try:
        async with database.session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, telegram_id, created_at, updated_at)
                    VALUES (100, 100, datetime('now'), datetime('now'))
                    """
                )
            )
            await session.execute(
                text(
                    """
                    INSERT INTO orders (
                        id, user_id, origin_bot_key, payment_bot_key, status,
                        amount_minor_units, currency, provider, invoice_payload, created_at
                    ) VALUES (
                        100, 100, 'razakov', NULL, 'pending',
                        60, 'XTR', 'telegram_stars', 'migration-21-order', datetime('now')
                    )
                    """
                )
            )
            await session.execute(
                text(
                    """
                    INSERT INTO payment_inbox (
                        provider, provider_charge_id, invoice_payload_hash,
                        invoice_payload, payer_external_id, amount_minor_units,
                        currency, received_at_utc, origin_bot_key, payment_bot_key,
                        reconciliation_status, attempt_count, updated_at_utc
                    ) VALUES (
                        'telegram_stars', 'migration-21-charge', 'hash',
                        'migration-21-order', '100', 60,
                        'XTR', datetime('now'), 'razaltush', NULL,
                        'received', 0, datetime('now')
                    )
                    """
                )
            )
            await session.execute(text("DELETE FROM schema_migrations WHERE version = 21"))
            await session.execute(text("DROP TABLE test_user_reset_operations"))

        async with database.engine.begin() as connection:
            await run_migrations(connection)

        async with database.session() as session:
            payment_bot = await session.execute(
                text(
                    "SELECT payment_bot_key FROM payment_inbox "
                    "WHERE provider_charge_id = 'migration-21-charge'"
                )
            )
            reset_table = await session.execute(
                text(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'test_user_reset_operations'"
                )
            )
            version = await session.execute(
                text("SELECT COUNT(*) FROM schema_migrations WHERE version = 21")
            )

        assert payment_bot.scalar_one() == "razaltush"
        assert reset_table.scalar_one() == 1
        assert version.scalar_one() == 1
    finally:
        await database.dispose()
