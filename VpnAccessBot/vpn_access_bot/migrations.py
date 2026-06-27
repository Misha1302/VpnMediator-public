from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def run_migrations(connection: AsyncConnection) -> None:
    await connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at_utc TEXT NOT NULL
            )
            """
        )
    )

    if not await _is_applied(connection, 1):
        await _execute_many(connection, _initial_schema_sql())
        await _mark_applied(connection, 1, "initial_schema")

    if not await _is_applied(connection, 2):
        await _add_commercial_columns(connection)
        await _execute_many(connection, _commercial_schema_sql())
        await _mark_applied(connection, 2, "commercial_entitlement_quotes_orders")

    if not await _is_applied(connection, 3):
        await _execute_many(connection, _idempotency_schema_sql())
        await _mark_applied(connection, 3, "order_payment_idempotency_indexes")

    if not await _is_applied(connection, 4):
        await _add_product_completion_columns(connection)
        await _execute_many(connection, _product_completion_schema_sql())
        await _mark_applied(connection, 4, "product_completion_state")

    if not await _is_applied(connection, 5):
        await _execute_many(connection, _product_completion_v2_schema_sql())
        await _mark_applied(connection, 5, "product_completion_v2")

    if not await _is_applied(connection, 6):
        await _add_column_if_missing(
            connection,
            "purchase_quotes",
            "is_test_order",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await _add_column_if_missing(
            connection,
            "orders",
            "is_test_order",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await _add_column_if_missing(
            connection,
            "notification_deliveries",
            "status",
            "TEXT NOT NULL DEFAULT 'delivered'",
        )
        await _add_column_if_missing(
            connection,
            "notification_deliveries",
            "attempt_count",
            "INTEGER NOT NULL DEFAULT 1",
        )
        await _add_column_if_missing(
            connection,
            "notification_deliveries",
            "last_error_code",
            "TEXT NULL",
        )
        await _add_column_if_missing(
            connection,
            "notification_deliveries",
            "claimed_at_utc",
            "TEXT NULL",
        )
        await _mark_applied(connection, 6, "test_orders_and_notification_delivery_state")

    if not await _is_applied(connection, 7):
        await _add_column_if_missing(
            connection,
            "orders",
            "activation_attempt_count",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await _add_column_if_missing(
            connection,
            "orders",
            "last_activation_attempt_at_utc",
            "TEXT NULL",
        )
        await _add_column_if_missing(
            connection,
            "orders",
            "next_activation_retry_at_utc",
            "TEXT NULL",
        )
        await _add_column_if_missing(
            connection,
            "orders",
            "last_activation_error_code",
            "TEXT NULL",
        )
        await _add_column_if_missing(
            connection,
            "trial_claims",
            "activation_attempt_count",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await _add_column_if_missing(
            connection,
            "trial_claims",
            "last_activation_attempt_at_utc",
            "TEXT NULL",
        )
        await _execute_many(connection, _activation_safety_schema_sql())
        await _mark_applied(connection, 7, "activation_safety_and_leases")

    if not await _is_applied(connection, 8):
        await _add_column_if_missing(
            connection,
            "audit_events",
            "correlation_id",
            "TEXT NULL",
        )
        await _execute_many(connection, _product_events_schema_sql())
        await _mark_applied(connection, 8, "product_events_and_correlation")

    if not await _is_applied(connection, 9):
        await _add_column_if_missing(
            connection,
            "orders",
            "base_expires_at_utc",
            "TEXT NULL",
        )
        await _add_column_if_missing(
            connection,
            "orders",
            "purchased_duration_days",
            "INTEGER NULL",
        )
        await _add_column_if_missing(
            connection,
            "orders",
            "expiration_policy_version",
            "TEXT NULL",
        )
        await _add_column_if_missing(
            connection,
            "orders",
            "target_expires_at_utc",
            "TEXT NULL",
        )
        await connection.execute(
            text(
                """
                UPDATE orders
                SET base_expires_at_utc = COALESCE(
                        base_expires_at_utc,
                        CASE
                            WHEN base_valid_until_utc IS NOT NULL
                                 AND base_valid_until_utc > created_at
                            THEN base_valid_until_utc
                            ELSE created_at
                        END
                    ),
                    purchased_duration_days = COALESCE(
                        purchased_duration_days,
                        CASE
                            WHEN order_kind = 'upgrade_devices' THEN 0
                            ELSE COALESCE(requested_duration_days, duration_days, 0)
                        END
                    ),
                    expiration_policy_version = COALESCE(
                        expiration_policy_version,
                        'legacy-exact-duration-v1'
                    );
                """
            )
        )
        await connection.execute(
            text(
                """
                UPDATE orders
                SET target_expires_at_utc = COALESCE(
                    target_expires_at_utc,
                    (
                        SELECT resulting_valid_until_utc
                        FROM order_applications
                        WHERE order_applications.order_id = orders.id
                    ),
                    CASE
                        WHEN order_kind = 'upgrade_devices' THEN base_expires_at_utc
                        ELSE datetime(
                            base_expires_at_utc,
                            '+' || purchased_duration_days || ' days'
                        )
                    END
                );
                """
            )
        )
        await _mark_applied(connection, 9, "immutable_order_expiration_snapshot")

    if not await _is_applied(connection, 10):
        await _execute_many(connection, _domain_constraint_schema_sql())
        await _mark_applied(connection, 10, "domain_constraints_and_worker_indexes")

    if not await _is_applied(connection, 11):
        await _execute_many(connection, _drop_domain_constraint_triggers_sql())
        await _execute_many(connection, _domain_constraint_schema_sql())
        await _mark_applied(connection, 11, "device_upgrade_zero_duration_constraints")

    if not await _is_applied(connection, 12):
        await _add_column_if_missing(
            connection,
            "onboarding_sessions",
            "issuance_request_id",
            "TEXT NULL",
        )
        await connection.execute(
            text(
                """
                UPDATE onboarding_sessions
                SET issuance_request_id = lower(hex(randomblob(16)))
                WHERE issuance_request_id IS NULL
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_onboarding_sessions_issuance_request
                ON onboarding_sessions(issuance_request_id)
                WHERE issuance_request_id IS NOT NULL
                """
            )
        )
        await _mark_applied(connection, 12, "device_issuance_identity")

    if not await _is_applied(connection, 13):
        for table, column, definition in (
            ("purchase_quotes", "origin_bot_key", "TEXT NULL"),
            ("orders", "origin_bot_key", "TEXT NULL"),
            ("onboarding_sessions", "origin_bot_key", "TEXT NULL"),
            ("support_requests", "origin_bot_key", "TEXT NULL"),
            ("support_messages", "bot_key", "TEXT NULL"),
            ("notification_deliveries", "delivery_bot_key", "TEXT NULL"),
            ("audit_events", "bot_key", "TEXT NULL"),
            ("product_events", "bot_key", "TEXT NULL"),
        ):
            await _add_column_if_missing(connection, table, column, definition)
        await _execute_many(connection, _multi_bot_schema_sql())
        await _mark_applied(connection, 13, "multi_bot_channels_and_routing")

    if not await _is_applied(connection, 14):
        await _execute_many(connection, _payment_inbox_schema_sql())
        await _mark_applied(connection, 14, "durable_payment_inbox")

    if not await _is_applied(connection, 15):
        for table, column, definition in (
            ("subscriptions", "reconciliation_state", "TEXT NOT NULL DEFAULT 'healthy'"),
            ("subscriptions", "reconciliation_reason", "TEXT NULL"),
            ("subscriptions", "reconciliation_blocked_at_utc", "TEXT NULL"),
        ):
            await _add_column_if_missing(connection, table, column, definition)
        await _execute_many(connection, _entitlement_operation_schema_sql())
        await _mark_applied(connection, 15, "durable_entitlement_and_refund_operations")

    if not await _is_applied(connection, 16):
        await _migrate_notification_delivery_semantics(connection)
        await _execute_many(connection, _notification_outbox_schema_sql())
        await _mark_applied(connection, 16, "notification_timestamps_and_transactional_outbox")

    if not await _is_applied(connection, 17):
        await _add_column_if_missing(
            connection,
            "trial_claims",
            "reserved_at_utc",
            "TEXT NULL",
        )
        await _add_column_if_missing(
            connection,
            "trial_claims",
            "usable_started_at_utc",
            "TEXT NULL",
        )
        await connection.execute(
            text(
                """
                UPDATE trial_claims
                SET reserved_at_utc = COALESCE(reserved_at_utc, created_at_utc),
                    usable_started_at_utc = COALESCE(usable_started_at_utc, started_at_utc)
                """
            )
        )
        await connection.execute(text("DROP INDEX IF EXISTS ux_orders_one_open_order_per_user"))
        await connection.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_one_open_order_per_user
                ON orders(user_id)
                WHERE status IN (
                    'pending', 'payment_received', 'activating',
                    'activation_failed', 'refunding'
                )
                """
            )
        )
        await _mark_applied(connection, 17, "trial_reservation_and_refund_order_serialization")

    if not await _is_applied(connection, 18):
        await _add_column_if_missing(
            connection,
            "entitlement_operations",
            "external_subscription_public_guid",
            "TEXT NULL",
        )
        await _mark_applied(connection, 18, "recoverable_subscription_creation_result")

    if not await _is_applied(connection, 19):
        await _execute_many(connection, _drop_domain_constraint_triggers_sql())
        await _execute_many(connection, _domain_constraint_schema_sql())
        await _mark_applied(connection, 19, "refunding_order_domain_state")

    if not await _is_applied(connection, 20):
        for table, column, definition in (
            ("orders", "checkout_authorized_at_utc", "TEXT NULL"),
            ("orders", "checkout_authorized_until_utc", "TEXT NULL"),
            ("payment_inbox", "invoice_payload", "TEXT NULL"),
            ("payment_inbox", "provider_occurred_at_utc", "TEXT NULL"),
            ("payment_inbox", "origin_bot_key", "TEXT NULL"),
            ("payment_inbox", "attempt_count", "INTEGER NOT NULL DEFAULT 0"),
            ("payment_inbox", "last_attempt_at_utc", "TEXT NULL"),
            ("payment_inbox", "next_attempt_at_utc", "TEXT NULL"),
            ("payment_inbox", "claimed_by", "TEXT NULL"),
            ("payment_inbox", "claim_expires_at_utc", "TEXT NULL"),
        ):
            await _add_column_if_missing(connection, table, column, definition)
        await connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_payment_inbox_recovery
                ON payment_inbox(
                    reconciliation_status, next_attempt_at_utc, claim_expires_at_utc
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_payment_inbox_origin_bot
                ON payment_inbox(origin_bot_key)
                """
            )
        )
        await _mark_applied(connection, 20, "payment_recovery_and_checkout_authorization")

    if not await _is_applied(connection, 21):
        for table, column, definition in (
            ("orders", "payment_bot_key", "TEXT NULL"),
            ("payment_inbox", "payment_bot_key", "TEXT NULL"),
            ("subscriptions", "test_reset_at_utc", "TEXT NULL"),
            ("notification_outbox", "delivery_bot_key", "TEXT NULL"),
        ):
            await _add_column_if_missing(connection, table, column, definition)
        await connection.execute(
            text(
                """
                UPDATE payment_inbox
                SET payment_bot_key = origin_bot_key
                WHERE payment_bot_key IS NULL
                  AND origin_bot_key IS NOT NULL
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_orders_payment_bot
                ON orders(payment_bot_key)
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_payment_inbox_payment_bot
                ON payment_inbox(payment_bot_key)
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_subscriptions_test_reset
                ON subscriptions(user_id, test_reset_at_utc)
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_notification_outbox_delivery_bot
                ON notification_outbox(delivery_bot_key)
                """
            )
        )
        await _execute_many(
            connection,
            [
                """
                CREATE TABLE IF NOT EXISTS test_user_reset_operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_request_id TEXT NOT NULL UNIQUE,
                    target_telegram_id INTEGER NOT NULL,
                    actor_telegram_id INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    outcome_json TEXT NULL,
                    created_at_utc TEXT NOT NULL,
                    completed_at_utc TEXT NULL
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS ix_test_user_reset_operations_target
                ON test_user_reset_operations(target_telegram_id, created_at_utc)
                """,
                """
                CREATE INDEX IF NOT EXISTS ix_test_user_reset_operations_state
                ON test_user_reset_operations(state, created_at_utc)
                """,
            ],
        )
        await _mark_applied(connection, 21, "payment_bot_binding_and_test_user_reset")

    if not await _is_applied(connection, 22):
        await _add_column_if_missing(
            connection,
            "users",
            "test_user_reset_generation",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await _add_column_if_missing(
            connection,
            "users",
            "test_user_reset_at_utc",
            "TEXT NULL",
        )
        await _mark_applied(connection, 22, "test_user_reset_trial_epoch")

    if not await _is_applied(connection, 23):
        await _execute_many(connection, _telegram_update_inbox_schema_sql())
        await _mark_applied(connection, 23, "durable_telegram_update_inbox")

    if not await _is_applied(connection, 24):
        await _mark_applied(connection, 24, "retired_feature_slot")

    if not await _is_applied(connection, 25):
        now = _now_iso()
        await connection.execute(
            text(
                """
                UPDATE entitlement_operations
                SET state = 'manual_review',
                    last_error_code = 'reconciliation_blocked',
                    last_error_at_utc = :now,
                    claimed_by = NULL,
                    claim_expires_at_utc = NULL,
                    updated_at_utc = :now
                WHERE state = 'pending'
                  AND source_entity_type = 'order'
                  AND external_request_sent_at_utc IS NULL
                  AND external_result_version IS NULL
                  AND external_result_status IS NULL
                  AND external_result_valid_until_utc IS NULL
                  AND external_result_device_limit IS NULL
                  AND EXISTS (
                      SELECT 1
                      FROM orders AS orders_to_repair
                      WHERE orders_to_repair.public_order_id =
                            entitlement_operations.source_entity_id
                        AND orders_to_repair.status = 'activation_failed'
                        AND orders_to_repair.last_activation_error_code =
                            'reconciliation_blocked'
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM subscriptions AS blocked_subscriptions
                      WHERE blocked_subscriptions.id =
                            entitlement_operations.subscription_id
                        AND blocked_subscriptions.reconciliation_state IN (
                            'blocked', 'recovering'
                        )
                  )
                """
            ),
            {"now": now},
        )
        await _mark_applied(
            connection,
            25,
            "quarantine_pre_request_reconciliation_blocked_operations",
        )

    if not await _is_applied(connection, 26):
        await _execute_many(connection, _broadcast_campaign_schema_sql())
        await _add_column_if_missing(
            connection,
            "notification_outbox",
            "broadcast_campaign_id",
            "INTEGER NULL REFERENCES broadcast_campaigns(id)",
        )
        await connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_notification_outbox_broadcast_campaign
                ON notification_outbox(broadcast_campaign_id)
                """
            )
        )
        await _mark_applied(connection, 26, "durable_admin_broadcast_campaigns")

    if not await _is_applied(connection, 27):
        await _execute_many(connection, _commerce_policy_schema_sql())
        await _mark_applied(connection, 27, "operation_specific_commerce_policy")

    if not await _is_applied(connection, 28):
        for column, definition in (
            ("previous_entitlement_version", "INTEGER NULL"),
            ("previous_status", "TEXT NULL"),
            ("previous_valid_until_utc", "TEXT NULL"),
            ("previous_max_devices", "INTEGER NULL"),
        ):
            await _add_column_if_missing(connection, "order_applications", column, definition)
        await connection.execute(
            text(
                """
                UPDATE order_applications
                SET previous_entitlement_version = COALESCE(
                        previous_entitlement_version,
                        (SELECT base_entitlement_version FROM orders
                         WHERE orders.id = order_applications.order_id)
                    ),
                    previous_valid_until_utc = COALESCE(
                        previous_valid_until_utc,
                        (SELECT base_valid_until_utc FROM orders
                         WHERE orders.id = order_applications.order_id)
                    ),
                    previous_max_devices = COALESCE(
                        previous_max_devices,
                        (SELECT base_max_devices FROM orders
                         WHERE orders.id = order_applications.order_id)
                    )
                """
            )
        )
        await _execute_many(connection, _refund_plan_schema_sql())
        await _mark_applied(connection, 28, "immutable_refund_plans_and_before_snapshots")

    if not await _is_applied(connection, 29):
        for column, definition in (
            ("previous_entitlement_version", "INTEGER NULL"),
            ("previous_status", "TEXT NULL"),
            ("previous_valid_until_utc", "TEXT NULL"),
            ("previous_max_devices", "INTEGER NULL"),
            ("reversal_operation_id", "INTEGER NULL"),
        ):
            await _add_column_if_missing(connection, "referral_rewards", column, definition)
        await connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_referral_rewards_source_state
                ON referral_rewards(source_order_id, status, available_at_utc)
                """
            )
        )
        await _mark_applied(connection, 29, "referral_cancellation_and_reversal_lifecycle")

    if not await _is_applied(connection, 30):
        await _execute_many(connection, _acquisition_schema_sql())
        await _mark_applied(connection, 30, "campaign_attribution_and_cohort_evidence")

    if not await _is_applied(connection, 31):
        await _execute_many(connection, _capacity_state_schema_sql())
        await _mark_applied(connection, 31, "capacity_admission_observability")

    if not await _is_applied(connection, 32):
        await _execute_many(connection, _commerce_policy_confirmation_schema_sql())
        await _mark_applied(connection, 32, "durable_confirmed_commerce_policy_changes")

    if not await _is_applied(connection, 33):
        await _add_column_if_missing(
            connection,
            "commerce_policy",
            "extend_and_upgrade_enabled",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await connection.execute(
            text(
                """
                UPDATE commerce_policy
                SET extend_and_upgrade_enabled = 0
                WHERE singleton_id = 1
                """
            )
        )
        await _mark_applied(connection, 33, "split_extend_and_upgrade_commerce_switch")

    if not await _is_applied(connection, 34):
        await _add_column_if_missing(
            connection,
            "purchase_quotes",
            "remaining_paid_seconds_at_quote",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await _add_column_if_missing(
            connection,
            "orders",
            "provider_payment_status",
            "TEXT NULL",
        )
        await _add_column_if_missing(
            connection,
            "orders",
            "provider_confirmation_url",
            "TEXT NULL",
        )
        await connection.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_quote_id_not_null
                ON orders(quote_id)
                WHERE quote_id IS NOT NULL
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_orders_provider_payment_status
                ON orders(provider, provider_payment_status)
                """
            )
        )
        await _mark_applied(connection, 34, "yookassa_sbp_checkout_state")


async def _is_applied(connection: AsyncConnection, version: int) -> bool:
    result = await connection.execute(
        text("SELECT COUNT(*) FROM schema_migrations WHERE version = :version"),
        {"version": version},
    )
    return int(result.scalar_one()) > 0


async def _mark_applied(connection: AsyncConnection, version: int, name: str) -> None:
    await connection.execute(
        text(
            """
            INSERT OR IGNORE INTO schema_migrations(version, name, applied_at_utc)
            VALUES(:version, :name, :applied_at_utc)
            """
        ),
        {"version": version, "name": name, "applied_at_utc": _now_iso()},
    )


async def _execute_many(connection: AsyncConnection, statements: Iterable[str]) -> None:
    for statement in statements:
        await connection.execute(text(statement))


async def _has_column(connection: AsyncConnection, table: str, column: str) -> bool:
    result = await connection.execute(text(f"PRAGMA table_info({table})"))
    return any(row[1] == column for row in result.fetchall())


async def _add_column_if_missing(
    connection: AsyncConnection,
    table: str,
    column: str,
    definition: str,
) -> None:
    if await _has_column(connection, table, column):
        return

    await connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))


async def _add_commercial_columns(connection: AsyncConnection) -> None:
    await _add_column_if_missing(
        connection,
        "orders",
        "public_order_id",
        "TEXT NULL",
    )
    await _add_column_if_missing(connection, "orders", "quote_id", "INTEGER NULL")
    await _add_column_if_missing(connection, "orders", "period_count", "INTEGER NOT NULL DEFAULT 1")
    await _add_column_if_missing(
        connection,
        "orders",
        "duration_days",
        "INTEGER NOT NULL DEFAULT 30",
    )
    await _add_column_if_missing(
        connection, "orders", "selected_max_devices", "INTEGER NOT NULL DEFAULT 3"
    )
    await _add_column_if_missing(
        connection, "orders", "pricing_version", "TEXT NOT NULL DEFAULT 'legacy-tariff'"
    )
    await _add_column_if_missing(connection, "orders", "target_subscription_id", "INTEGER NULL")
    await _add_column_if_missing(
        connection, "orders", "order_kind", "TEXT NOT NULL DEFAULT 'purchase'"
    )
    await _add_column_if_missing(connection, "orders", "expires_at_utc", "TEXT NULL")
    await _add_column_if_missing(connection, "orders", "cancelled_at_utc", "TEXT NULL")
    await _add_column_if_missing(connection, "orders", "completed_at_utc", "TEXT NULL")
    await _add_column_if_missing(
        connection,
        "subscriptions",
        "updated_at_utc",
        "TEXT NULL",
    )

    await connection.execute(
        text(
            """
            UPDATE orders
            SET public_order_id = 'legacy-' || lower(hex(randomblob(16)))
            WHERE public_order_id IS NULL OR public_order_id = ''
            """
        )
    )
    await connection.execute(
        text(
            """
            UPDATE subscriptions
            SET updated_at_utc = COALESCE(
                updated_at_utc,
                created_at,
                datetime('now')
            )
            WHERE updated_at_utc IS NULL OR updated_at_utc = ''
            """
        )
    )
    await connection.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_public_order_id
            ON orders(public_order_id)
            """
        )
    )


async def _add_product_completion_columns(connection: AsyncConnection) -> None:
    await _add_column_if_missing(connection, "users", "primary_subscription_id", "INTEGER NULL")
    await _add_column_if_missing(connection, "users", "platform_preference", "TEXT NULL")
    await _add_column_if_missing(connection, "users", "referral_code", "TEXT NULL")
    await _add_column_if_missing(connection, "users", "referred_by_user_id", "INTEGER NULL")
    await _add_column_if_missing(connection, "users", "referred_at_utc", "TEXT NULL")
    await _add_column_if_missing(
        connection, "users", "referral_blocked", "INTEGER NOT NULL DEFAULT 0"
    )

    for table in ("purchase_quotes", "orders"):
        await _add_column_if_missing(connection, table, "base_entitlement_version", "INTEGER NULL")
        await _add_column_if_missing(connection, table, "base_valid_until_utc", "TEXT NULL")
        await _add_column_if_missing(connection, table, "base_max_devices", "INTEGER NULL")
        await _add_column_if_missing(connection, table, "requested_max_devices", "INTEGER NULL")
        await _add_column_if_missing(connection, table, "requested_duration_days", "INTEGER NULL")
        await _add_column_if_missing(
            connection, table, "upgrade_amount_minor_units", "INTEGER NOT NULL DEFAULT 0"
        )
        await _add_column_if_missing(
            connection, table, "extension_amount_minor_units", "INTEGER NOT NULL DEFAULT 0"
        )
        await _add_column_if_missing(
            connection, table, "price_before_personal_discount", "INTEGER NOT NULL DEFAULT 0"
        )
        await _add_column_if_missing(connection, table, "personal_discount_id", "INTEGER NULL")
        await _add_column_if_missing(
            connection, table, "personal_discount_bps", "INTEGER NOT NULL DEFAULT 0"
        )
        await _add_column_if_missing(
            connection,
            table,
            "personal_discount_amount_minor_units",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await _add_column_if_missing(connection, table, "final_amount_minor_units", "INTEGER NULL")
        await _add_column_if_missing(
            connection, table, "referral_eligible", "INTEGER NOT NULL DEFAULT 1"
        )
        await _add_column_if_missing(connection, table, "trial_claim_id", "INTEGER NULL")
        await _add_column_if_missing(
            connection,
            table,
            "trial_seconds_remaining_at_quote",
            "INTEGER NOT NULL DEFAULT 0",
        )

    await connection.execute(
        text(
            """
            UPDATE users
            SET referral_code = lower(hex(randomblob(9)))
            WHERE referral_code IS NULL OR referral_code = ''
            """
        )
    )
    await connection.execute(
        text(
            """
            UPDATE users
            SET primary_subscription_id = (
                SELECT s.id
                FROM subscriptions s
                WHERE s.user_id = users.id
                ORDER BY
                    CASE WHEN s.status = 'active' THEN 0 ELSE 1 END,
                    s.expires_at DESC,
                    s.id ASC
                LIMIT 1
            )
            WHERE primary_subscription_id IS NULL
              AND EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id = users.id)
            """
        )
    )
    await connection.execute(
        text(
            """
            UPDATE purchase_quotes
            SET requested_max_devices = COALESCE(requested_max_devices, max_devices),
                requested_duration_days = COALESCE(requested_duration_days, duration_days),
                final_amount_minor_units = COALESCE(final_amount_minor_units, amount_minor_units),
                price_before_personal_discount = CASE
                    WHEN price_before_personal_discount = 0 THEN amount_minor_units
                    ELSE price_before_personal_discount
                END
            """
        )
    )
    await connection.execute(
        text(
            """
            UPDATE orders
            SET requested_max_devices = COALESCE(requested_max_devices, selected_max_devices),
                requested_duration_days = COALESCE(requested_duration_days, duration_days),
                final_amount_minor_units = COALESCE(final_amount_minor_units, amount_minor_units),
                price_before_personal_discount = CASE
                    WHEN price_before_personal_discount = 0 THEN amount_minor_units
                    ELSE price_before_personal_discount
                END
            """
        )
    )


def _product_completion_v2_schema_sql() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS commercial_entitlement_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL,
            source_kind TEXT NOT NULL,
            starts_at_utc TEXT NOT NULL,
            ends_at_utc TEXT NOT NULL,
            source_order_id INTEGER NULL,
            source_entity_id TEXT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'applied',
            created_at_utc TEXT NOT NULL,
            reversed_at_utc TEXT NULL,
            FOREIGN KEY(subscription_id) REFERENCES subscriptions(id),
            FOREIGN KEY(source_order_id) REFERENCES orders(id)
        )
        """,
        (
            "CREATE INDEX IF NOT EXISTS ix_entitlement_segments_subscription_end "
            "ON commercial_entitlement_segments(subscription_id, source_kind, status, ends_at_utc)"
        ),
        """
        INSERT OR IGNORE INTO commercial_entitlement_segments (
            subscription_id,
            source_kind,
            starts_at_utc,
            ends_at_utc,
            source_order_id,
            source_entity_id,
            idempotency_key,
            status,
            created_at_utc
        )
        SELECT
            oa.subscription_id,
            'paid_order',
            datetime(oa.resulting_valid_until_utc, '-' || oa.duration_days || ' days'),
            oa.resulting_valid_until_utc,
            oa.order_id,
            CAST(oa.order_id AS TEXT),
            'order:' || o.public_order_id,
            'applied',
            oa.applied_at_utc
        FROM order_applications oa
        JOIN orders o ON o.id = oa.order_id
        WHERE oa.duration_days > 0
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_referral_rewards_status_available
        ON referral_rewards(status, available_at_utc)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_user_discounts_active
        ON user_discounts(user_id, status, starts_at_utc, expires_at_utc)
        """,
    ]


def _drop_domain_constraint_triggers_sql() -> list[str]:
    return [
        "DROP TRIGGER IF EXISTS trg_orders_domain_constraints_insert",
        "DROP TRIGGER IF EXISTS trg_orders_domain_constraints_update",
        "DROP TRIGGER IF EXISTS trg_quotes_domain_constraints_insert",
        "DROP TRIGGER IF EXISTS trg_quotes_domain_constraints_update",
    ]


def _domain_constraint_schema_sql() -> list[str]:
    valid_order_statuses = (
        "'pending','payment_received','activating','paid','activation_failed',"
        "'failed','refunding','refunded','cancelled','expired'"
    )
    valid_order_kinds = "'purchase','extend','resume','upgrade_devices','extend_and_upgrade'"
    return [
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_orders_domain_constraints_insert
        BEFORE INSERT ON orders
        WHEN NEW.amount_minor_units < 0
          OR COALESCE(NEW.duration_days, 0) < 0
          OR (
              NEW.order_kind = 'upgrade_devices'
              AND COALESCE(NEW.duration_days, 0) <> 0
          )
          OR (
              NEW.order_kind <> 'upgrade_devices'
              AND COALESCE(NEW.duration_days, 0) <= 0
          )
          OR COALESCE(NEW.selected_max_devices, 0) <= 0
          OR COALESCE(NEW.personal_discount_bps, 0) NOT BETWEEN 0 AND 10000
          OR COALESCE(NEW.activation_attempt_count, 0) < 0
          OR NEW.status NOT IN ({valid_order_statuses})
          OR NEW.order_kind NOT IN ({valid_order_kinds})
        BEGIN
            SELECT RAISE(ABORT, 'orders_domain_constraint_failed');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_orders_domain_constraints_update
        BEFORE UPDATE ON orders
        WHEN NEW.amount_minor_units < 0
          OR COALESCE(NEW.duration_days, 0) < 0
          OR (
              NEW.order_kind = 'upgrade_devices'
              AND COALESCE(NEW.duration_days, 0) <> 0
          )
          OR (
              NEW.order_kind <> 'upgrade_devices'
              AND COALESCE(NEW.duration_days, 0) <= 0
          )
          OR COALESCE(NEW.selected_max_devices, 0) <= 0
          OR COALESCE(NEW.personal_discount_bps, 0) NOT BETWEEN 0 AND 10000
          OR COALESCE(NEW.activation_attempt_count, 0) < 0
          OR NEW.status NOT IN ({valid_order_statuses})
          OR NEW.order_kind NOT IN ({valid_order_kinds})
        BEGIN
            SELECT RAISE(ABORT, 'orders_domain_constraint_failed');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_quotes_domain_constraints_insert
        BEFORE INSERT ON purchase_quotes
        WHEN NEW.amount_minor_units < 0
          OR NEW.duration_days < 0
          OR (
              NEW.order_kind = 'upgrade_devices'
              AND NEW.duration_days <> 0
          )
          OR (
              NEW.order_kind <> 'upgrade_devices'
              AND NEW.duration_days <= 0
          )
          OR NEW.max_devices <= 0
          OR COALESCE(NEW.personal_discount_bps, 0) NOT BETWEEN 0 AND 10000
          OR NEW.order_kind NOT IN ({valid_order_kinds})
        BEGIN
            SELECT RAISE(ABORT, 'purchase_quotes_domain_constraint_failed');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_quotes_domain_constraints_update
        BEFORE UPDATE ON purchase_quotes
        WHEN NEW.amount_minor_units < 0
          OR NEW.duration_days < 0
          OR (
              NEW.order_kind = 'upgrade_devices'
              AND NEW.duration_days <> 0
          )
          OR (
              NEW.order_kind <> 'upgrade_devices'
              AND NEW.duration_days <= 0
          )
          OR NEW.max_devices <= 0
          OR COALESCE(NEW.personal_discount_bps, 0) NOT BETWEEN 0 AND 10000
          OR NEW.order_kind NOT IN ({valid_order_kinds})
        BEGIN
            SELECT RAISE(ABORT, 'purchase_quotes_domain_constraint_failed');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_tariffs_domain_constraints_insert
        BEFORE INSERT ON tariffs
        WHEN NEW.price_minor_units < 0
          OR NEW.duration_days <= 0
          OR NEW.max_devices <= 0
        BEGIN
            SELECT RAISE(ABORT, 'tariffs_domain_constraint_failed');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_tariffs_domain_constraints_update
        BEFORE UPDATE ON tariffs
        WHEN NEW.price_minor_units < 0
          OR NEW.duration_days <= 0
          OR NEW.max_devices <= 0
        BEGIN
            SELECT RAISE(ABORT, 'tariffs_domain_constraint_failed');
        END
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_orders_activation_retry
        ON orders(status, next_activation_retry_at_utc, id)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_notification_deliveries_claim
        ON notification_deliveries(status, claimed_at_utc, delivered_at_utc, id)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_trial_claims_activation_retry
        ON trial_claims(status, last_activation_attempt_at_utc, id)
        """,
    ]


def _initial_schema_sql() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL UNIQUE,
            username TEXT NULL,
            first_name TEXT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_users_telegram_id ON users(telegram_id)",
        """
        CREATE TABLE IF NOT EXISTS tariffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            price_minor_units INTEGER NOT NULL,
            currency TEXT NOT NULL,
            duration_days INTEGER NOT NULL,
            max_devices INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_tariffs_code ON tariffs(code)",
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            tariff_id INTEGER NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            amount_minor_units INTEGER NOT NULL,
            currency TEXT NOT NULL,
            provider TEXT NOT NULL,
            provider_payment_id TEXT NULL,
            invoice_payload TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            paid_at TEXT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(tariff_id) REFERENCES tariffs(id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_orders_user_id ON orders(user_id)",
        "CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status)",
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            tariff_id INTEGER NULL,
            public_guid TEXT NOT NULL UNIQUE,
            signed_url TEXT NOT NULL,
            max_devices INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            starts_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            disabled_at TEXT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(tariff_id) REFERENCES tariffs(id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_subscriptions_user_id ON subscriptions(user_id)",
        "CREATE INDEX IF NOT EXISTS ix_subscriptions_public_guid ON subscriptions(public_guid)",
        "CREATE INDEX IF NOT EXISTS ix_subscriptions_expires_at ON subscriptions(expires_at)",
        "CREATE INDEX IF NOT EXISTS ix_subscriptions_status ON subscriptions(status)",
        """
        CREATE TABLE IF NOT EXISTS device_reset_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(subscription_id) REFERENCES subscriptions(id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            UNIQUE(subscription_id, created_at)
        )
        """,
        (
            "CREATE INDEX IF NOT EXISTS ix_device_reset_subscription "
            "ON device_reset_events(subscription_id)"
        ),
    ]


def _commercial_schema_sql() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS purchase_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_quote_id TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            period_count INTEGER NOT NULL,
            duration_days INTEGER NOT NULL,
            max_devices INTEGER NOT NULL,
            amount_minor_units INTEGER NOT NULL,
            currency TEXT NOT NULL,
            pricing_version TEXT NOT NULL,
            target_subscription_id INTEGER NULL,
            order_kind TEXT NOT NULL,
            expires_at_utc TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            consumed_at_utc TEXT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(target_subscription_id) REFERENCES subscriptions(id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_purchase_quotes_user_id ON purchase_quotes(user_id)",
        (
            "CREATE INDEX IF NOT EXISTS ix_purchase_quotes_public_quote_id "
            "ON purchase_quotes(public_quote_id)"
        ),
        """
        CREATE TABLE IF NOT EXISTS access_entitlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL UNIQUE,
            version INTEGER NOT NULL,
            status TEXT NOT NULL,
            valid_until_utc TEXT NOT NULL,
            max_device_tokens INTEGER NOT NULL,
            updated_at_utc TEXT NOT NULL,
            FOREIGN KEY(subscription_id) REFERENCES subscriptions(id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS order_applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL UNIQUE,
            subscription_id INTEGER NOT NULL,
            applied_at_utc TEXT NOT NULL,
            duration_days INTEGER NOT NULL,
            selected_max_devices INTEGER NOT NULL,
            resulting_valid_until_utc TEXT NOT NULL,
            resulting_entitlement_version INTEGER NOT NULL,
            FOREIGN KEY(order_id) REFERENCES orders(id),
            FOREIGN KEY(subscription_id) REFERENCES subscriptions(id)
        )
        """,
        (
            "CREATE INDEX IF NOT EXISTS ix_order_applications_subscription "
            "ON order_applications(subscription_id)"
        ),
        """
        CREATE TABLE IF NOT EXISTS notification_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL,
            notification_kind TEXT NOT NULL,
            delivery_key TEXT NOT NULL,
            delivered_at_utc TEXT NOT NULL,
            FOREIGN KEY(subscription_id) REFERENCES subscriptions(id),
            UNIQUE(subscription_id, notification_kind, delivery_key)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at_utc TEXT NOT NULL,
            event_type TEXT NOT NULL,
            telegram_id INTEGER NULL,
            order_id INTEGER NULL,
            subscription_id INTEGER NULL,
            public_guid TEXT NULL,
            error_code TEXT NULL,
            details_json TEXT NULL,
            FOREIGN KEY(order_id) REFERENCES orders(id),
            FOREIGN KEY(subscription_id) REFERENCES subscriptions(id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_audit_events_event_type ON audit_events(event_type)",
        "CREATE INDEX IF NOT EXISTS ix_audit_events_telegram_id ON audit_events(telegram_id)",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_provider_payment_id_not_null
        ON orders(provider, provider_payment_id)
        WHERE provider_payment_id IS NOT NULL
        """,
    ]


def _activation_safety_schema_sql() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS access_operation_leases (
            user_id INTEGER PRIMARY KEY,
            owner_kind TEXT NOT NULL,
            owner_key TEXT NOT NULL,
            lease_expires_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_access_operation_leases_expiry
        ON access_operation_leases(lease_expires_at_utc)
        """,
    ]


def _product_events_schema_sql() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS product_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT NOT NULL UNIQUE,
            user_id INTEGER NULL,
            event_name TEXT NOT NULL,
            occurred_at_utc TEXT NOT NULL,
            correlation_id TEXT NULL,
            idempotency_key TEXT NULL UNIQUE,
            payload_json TEXT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_product_events_user ON product_events(user_id)",
        "CREATE INDEX IF NOT EXISTS ix_product_events_name ON product_events(event_name)",
        "CREATE INDEX IF NOT EXISTS ix_audit_events_correlation ON audit_events(correlation_id)",
    ]


def _idempotency_schema_sql() -> list[str]:
    return [
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_quote_id_not_null
        ON orders(quote_id)
        WHERE quote_id IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_one_open_order_per_user
        ON orders(user_id)
        WHERE status IN ('pending', 'payment_received', 'activating', 'activation_failed')
        """,
    ]


def _product_completion_schema_sql() -> list[str]:
    return [
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_referral_code ON users(referral_code)",
        (
            "CREATE INDEX IF NOT EXISTS ix_users_primary_subscription "
            "ON users(primary_subscription_id)"
        ),
        "CREATE INDEX IF NOT EXISTS ix_users_referred_by ON users(referred_by_user_id)",
        """
        CREATE TABLE IF NOT EXISTS commercial_entitlement_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL,
            source_kind TEXT NOT NULL,
            duration_delta_seconds INTEGER NOT NULL,
            device_limit_before INTEGER NOT NULL,
            device_limit_after INTEGER NOT NULL,
            source_order_id INTEGER NULL,
            source_entity_id TEXT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            applied_at_utc TEXT NULL,
            reversed_by_adjustment_id INTEGER NULL,
            FOREIGN KEY(subscription_id) REFERENCES subscriptions(id),
            FOREIGN KEY(source_order_id) REFERENCES orders(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_commercial_adjustments_subscription
        ON commercial_entitlement_adjustments(subscription_id)
        """,
        """
        CREATE TABLE IF NOT EXISTS onboarding_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subscription_id INTEGER NULL,
            device_public_id TEXT NULL,
            platform TEXT NULL,
            current_step TEXT NOT NULL,
            status TEXT NOT NULL,
            handoff_claim_id TEXT NULL,
            issuance_request_id TEXT NULL,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            completed_at_utc TEXT NULL,
            last_error_code TEXT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(subscription_id) REFERENCES subscriptions(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_onboarding_sessions_user_status
        ON onboarding_sessions(user_id, status)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_onboarding_sessions_issuance_request
        ON onboarding_sessions(issuance_request_id)
        WHERE issuance_request_id IS NOT NULL
        """,
        """
        CREATE TABLE IF NOT EXISTS trial_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            subscription_id INTEGER NULL,
            status TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL,
            max_devices INTEGER NOT NULL,
            started_at_utc TEXT NULL,
            ends_at_utc TEXT NULL,
            entitlement_version INTEGER NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at_utc TEXT NOT NULL,
            activated_at_utc TEXT NULL,
            converted_at_utc TEXT NULL,
            expired_at_utc TEXT NULL,
            revoked_at_utc TEXT NULL,
            failure_code TEXT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(subscription_id) REFERENCES subscriptions(id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_trial_claims_status ON trial_claims(status)",
        """
        CREATE TABLE IF NOT EXISTS user_discounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            discount_bps INTEGER NOT NULL,
            scope TEXT NOT NULL DEFAULT 'all',
            starts_at_utc TEXT NULL,
            expires_at_utc TEXT NULL,
            max_uses INTEGER NULL,
            used_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            reason TEXT NULL,
            created_by_admin_telegram_id INTEGER NOT NULL,
            created_at_utc TEXT NOT NULL,
            revoked_at_utc TEXT NULL,
            revoked_by_admin_telegram_id INTEGER NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_user_discounts_one_active
        ON user_discounts(user_id)
        WHERE status = 'active'
        """,
        """
        CREATE TABLE IF NOT EXISTS discount_redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discount_id INTEGER NOT NULL,
            order_id INTEGER NOT NULL UNIQUE,
            status TEXT NOT NULL,
            reserved_at_utc TEXT NOT NULL,
            applied_at_utc TEXT NULL,
            released_at_utc TEXT NULL,
            discount_amount_minor_units INTEGER NOT NULL,
            FOREIGN KEY(discount_id) REFERENCES user_discounts(id),
            FOREIGN KEY(order_id) REFERENCES orders(id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS referral_rewards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_user_id INTEGER NOT NULL,
            referred_user_id INTEGER NOT NULL,
            source_order_id INTEGER NOT NULL UNIQUE,
            reward_percent INTEGER NOT NULL,
            reward_duration_seconds INTEGER NOT NULL,
            status TEXT NOT NULL,
            available_at_utc TEXT NOT NULL,
            target_subscription_id INTEGER NULL,
            entitlement_version INTEGER NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at_utc TEXT NOT NULL,
            applied_at_utc TEXT NULL,
            cancelled_at_utc TEXT NULL,
            reversed_at_utc TEXT NULL,
            failure_code TEXT NULL,
            FOREIGN KEY(referrer_user_id) REFERENCES users(id),
            FOREIGN KEY(referred_user_id) REFERENCES users(id),
            FOREIGN KEY(source_order_id) REFERENCES orders(id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_referral_rewards_status ON referral_rewards(status)",
        """
        CREATE TABLE IF NOT EXISTS support_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            support_chat_id INTEGER NULL,
            support_root_message_id INTEGER NULL,
            closed_at_utc TEXT NULL,
            closed_by_admin_telegram_id INTEGER NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_support_requests_user_status
        ON support_requests(user_id, status)
        """,
        """
        CREATE TABLE IF NOT EXISTS support_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            support_request_id INTEGER NOT NULL,
            direction TEXT NOT NULL,
            telegram_message_id INTEGER NOT NULL,
            telegram_chat_id INTEGER NOT NULL,
            message_type TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            FOREIGN KEY(support_request_id) REFERENCES support_requests(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_support_messages_telegram
        ON support_messages(telegram_chat_id, telegram_message_id)
        """,
        """
        CREATE TABLE IF NOT EXISTS worker_health (
            worker_name TEXT PRIMARY KEY,
            last_attempt_at_utc TEXT NULL,
            last_success_at_utc TEXT NULL,
            last_failure_at_utc TEXT NULL,
            last_error_code TEXT NULL
        )
        """,
    ]


async def _migrate_notification_delivery_semantics(connection: AsyncConnection) -> None:
    await _execute_many(
        connection,
        [
            """
            CREATE TABLE IF NOT EXISTS notification_deliveries_v16 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL,
                notification_kind TEXT NOT NULL,
                delivery_key TEXT NOT NULL,
                delivery_bot_key TEXT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error_code TEXT NULL,
                claimed_at_utc TEXT NULL,
                send_started_at_utc TEXT NULL,
                provider_accepted_at_utc TEXT NULL,
                delivered_at_utc TEXT NULL,
                failed_at_utc TEXT NULL,
                FOREIGN KEY(subscription_id) REFERENCES subscriptions(id),
                UNIQUE(subscription_id, notification_kind, delivery_key)
            )
            """,
            """
            INSERT OR IGNORE INTO notification_deliveries_v16(
                id, subscription_id, notification_kind, delivery_key,
                delivery_bot_key, status, attempt_count, last_error_code,
                claimed_at_utc, send_started_at_utc, provider_accepted_at_utc,
                delivered_at_utc, failed_at_utc
            )
            SELECT
                id, subscription_id, notification_kind, delivery_key,
                delivery_bot_key,
                CASE WHEN status = 'delivered' THEN 'provider_accepted' ELSE status END,
                attempt_count, last_error_code, claimed_at_utc,
                CASE WHEN status IN ('sending', 'delivered') THEN claimed_at_utc ELSE NULL END,
                CASE WHEN status = 'delivered' THEN delivered_at_utc ELSE NULL END,
                NULL,
                CASE WHEN status = 'failed' THEN claimed_at_utc ELSE NULL END
            FROM notification_deliveries
            """,
            "DROP TABLE notification_deliveries",
            "ALTER TABLE notification_deliveries_v16 RENAME TO notification_deliveries",
            """
            CREATE INDEX IF NOT EXISTS ix_notification_delivery_bot
            ON notification_deliveries(delivery_bot_key)
            """,
        ],
    )


def _payment_inbox_schema_sql() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS payment_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            provider_charge_id TEXT NOT NULL,
            invoice_payload_hash TEXT NOT NULL,
            payer_external_id TEXT NOT NULL,
            amount_minor_units INTEGER NOT NULL,
            currency TEXT NOT NULL,
            received_at_utc TEXT NOT NULL,
            matched_order_id INTEGER NULL,
            reconciliation_status TEXT NOT NULL,
            failure_code TEXT NULL,
            processed_at_utc TEXT NULL,
            updated_at_utc TEXT NOT NULL,
            FOREIGN KEY(matched_order_id) REFERENCES orders(id),
            UNIQUE(provider, provider_charge_id),
            CHECK(amount_minor_units >= 0)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_payment_inbox_reconciliation
        ON payment_inbox(reconciliation_status, received_at_utc)
        """,
    ]


def _telegram_update_inbox_schema_sql() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS telegram_update_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_key TEXT NOT NULL,
            update_id INTEGER NOT NULL,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            received_at_utc TEXT NOT NULL,
            last_attempt_at_utc TEXT NULL,
            next_attempt_at_utc TEXT NULL,
            claimed_by TEXT NULL,
            claim_expires_at_utc TEXT NULL,
            processed_at_utc TEXT NULL,
            failure_code TEXT NULL,
            last_error_message TEXT NULL,
            updated_at_utc TEXT NOT NULL,
            UNIQUE(bot_key, update_id),
            CHECK(attempt_count >= 0),
            CHECK(status IN ('pending', 'processing', 'retry', 'processed', 'quarantined'))
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_telegram_update_inbox_dispatch
        ON telegram_update_inbox(status, next_attempt_at_utc, claim_expires_at_utc, received_at_utc)
        """,
    ]


def _entitlement_operation_schema_sql() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS entitlement_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT NOT NULL UNIQUE,
            subscription_id INTEGER NULL,
            user_id INTEGER NOT NULL,
            operation_type TEXT NOT NULL,
            source_entity_type TEXT NOT NULL,
            source_entity_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            expected_version INTEGER NULL,
            duration_delta_seconds INTEGER NOT NULL DEFAULT 0,
            requested_device_limit INTEGER NULL,
            requested_status TEXT NULL,
            observed_valid_until_utc TEXT NULL,
            intended_valid_until_utc TEXT NULL,
            state TEXT NOT NULL,
            external_result_version INTEGER NULL,
            external_result_status TEXT NULL,
            external_result_valid_until_utc TEXT NULL,
            external_result_device_limit INTEGER NULL,
            external_subscription_public_guid TEXT NULL,
            external_request_sent_at_utc TEXT NULL,
            external_applied_at_utc TEXT NULL,
            local_commit_completed_at_utc TEXT NULL,
            claimed_by TEXT NULL,
            claim_expires_at_utc TEXT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error_code TEXT NULL,
            last_error_at_utc TEXT NULL,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            FOREIGN KEY(subscription_id) REFERENCES subscriptions(id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            UNIQUE(source_entity_type, source_entity_id, operation_type),
            CHECK(attempt_count >= 0)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_entitlement_operations_recovery
        ON entitlement_operations(state, claim_expires_at_utc, updated_at_utc)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_entitlement_operation_active_subscription
        ON entitlement_operations(subscription_id)
        WHERE subscription_id IS NOT NULL AND state IN (
            'pending', 'claimed', 'external_unknown', 'external_applied',
            'local_commit_pending', 'failed_retriable', 'compensating'
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS refund_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT NOT NULL UNIQUE,
            order_id INTEGER NOT NULL UNIQUE,
            subscription_id INTEGER NULL,
            user_id INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL,
            provider TEXT NOT NULL,
            provider_charge_reference_hash TEXT NOT NULL,
            provider_requested_at_utc TEXT NULL,
            provider_refunded_at_utc TEXT NULL,
            compensation_operation_id INTEGER NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error_code TEXT NULL,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            completed_at_utc TEXT NULL,
            FOREIGN KEY(order_id) REFERENCES orders(id),
            FOREIGN KEY(subscription_id) REFERENCES subscriptions(id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(compensation_operation_id) REFERENCES entitlement_operations(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_refund_operations_recovery
        ON refund_operations(state, updated_at_utc)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_subscriptions_reconciliation_state
        ON subscriptions(reconciliation_state, reconciliation_blocked_at_utc)
        """,
    ]


def _notification_outbox_schema_sql() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT NOT NULL UNIQUE,
            idempotency_key TEXT NOT NULL UNIQUE,
            user_id INTEGER NULL,
            subscription_id INTEGER NULL,
            order_id INTEGER NULL,
            bot_key TEXT NULL,
            notification_kind TEXT NOT NULL,
            payload_json TEXT NULL,
            state TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            available_at_utc TEXT NOT NULL,
            claimed_at_utc TEXT NULL,
            provider_accepted_at_utc TEXT NULL,
            failed_at_utc TEXT NULL,
            last_error_code TEXT NULL,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(subscription_id) REFERENCES subscriptions(id),
            FOREIGN KEY(order_id) REFERENCES orders(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_notification_outbox_dispatch
        ON notification_outbox(state, available_at_utc)
        """,
    ]


def _broadcast_campaign_schema_sql() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS broadcast_campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT NOT NULL UNIQUE,
            admin_telegram_id INTEGER NOT NULL,
            source_bot_key TEXT NOT NULL,
            source_chat_id INTEGER NOT NULL,
            source_message_id INTEGER NOT NULL,
            filter_kind TEXT NOT NULL,
            recipient_pattern TEXT NULL,
            recipient_upper_bound_user_id INTEGER NOT NULL,
            message_text TEXT NOT NULL,
            message_sha256 TEXT NOT NULL,
            confirmation_token_hash TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL,
            target_count INTEGER NOT NULL DEFAULT 0,
            queued_count INTEGER NOT NULL DEFAULT 0,
            expires_at_utc TEXT NOT NULL,
            confirmed_at_utc TEXT NULL,
            queued_at_utc TEXT NULL,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            UNIQUE(source_bot_key, source_chat_id, source_message_id),
            CHECK(filter_kind IN ('all', 'regex')),
            CHECK(state IN (
                'preparing', 'awaiting_confirmation', 'enqueuing', 'queued',
                'empty', 'expired', 'failed'
            )),
            CHECK(target_count >= 0),
            CHECK(queued_count >= 0)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_broadcast_campaign_admin_state
        ON broadcast_campaigns(admin_telegram_id, state, created_at_utc)
        """,
        """
        CREATE TABLE IF NOT EXISTS broadcast_recipients (
            campaign_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            telegram_id INTEGER NOT NULL,
            created_at_utc TEXT NOT NULL,
            PRIMARY KEY(campaign_id, user_id),
            FOREIGN KEY(campaign_id) REFERENCES broadcast_campaigns(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_broadcast_recipients_campaign_user
        ON broadcast_recipients(campaign_id, user_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_broadcast_recipients_telegram
        ON broadcast_recipients(telegram_id)
        """,
    ]


def _commerce_policy_schema_sql() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS commerce_policy (
            singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
            version INTEGER NOT NULL,
            new_purchases_enabled INTEGER NOT NULL,
            trials_enabled INTEGER NOT NULL,
            renewals_enabled INTEGER NOT NULL,
            resumes_enabled INTEGER NOT NULL,
            device_upgrades_enabled INTEGER NOT NULL,
            extend_and_upgrade_enabled INTEGER NOT NULL DEFAULT 0,
            referrals_enabled INTEGER NOT NULL,
            campaign_tracking_enabled INTEGER NOT NULL,
            capacity_enforcement_enabled INTEGER NOT NULL,
            reason_code TEXT NOT NULL,
            operator_note TEXT NULL,
            updated_by_admin_telegram_id INTEGER NULL,
            updated_at_utc TEXT NOT NULL,
            expires_at_utc TEXT NULL
        )
        """,
        f"""
        INSERT OR IGNORE INTO commerce_policy(
            singleton_id, version, new_purchases_enabled, trials_enabled,
            renewals_enabled, resumes_enabled, device_upgrades_enabled,
            extend_and_upgrade_enabled, referrals_enabled, campaign_tracking_enabled,
            capacity_enforcement_enabled, reason_code, updated_at_utc
        ) VALUES(1, 1, 0, 0, 1, 0, 0, 0, 0, 1, 0, 'pre_advertising_freeze', '{_now_iso()}')
        """,
    ]


def _commerce_policy_confirmation_schema_sql() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS commerce_policy_change_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT NOT NULL UNIQUE,
            confirmation_token_hash TEXT NOT NULL UNIQUE,
            admin_telegram_id INTEGER NOT NULL,
            switch_name TEXT NOT NULL,
            requested_enabled INTEGER NOT NULL,
            expected_policy_version INTEGER NOT NULL,
            reason_code TEXT NOT NULL,
            operator_note TEXT NULL,
            state TEXT NOT NULL,
            expires_at_utc TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            confirmed_at_utc TEXT NULL,
            failure_code TEXT NULL,
            CHECK(requested_enabled IN (0, 1)),
            CHECK(state IN ('pending', 'confirmed', 'expired', 'cancelled', 'stale'))
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_commerce_policy_change_requests_pending
        ON commerce_policy_change_requests(state, expires_at_utc)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_commerce_policy_change_requests_admin
        ON commerce_policy_change_requests(admin_telegram_id, switch_name, state)
        """,
        """
        UPDATE commerce_policy
        SET
            version = version + 1,
            new_purchases_enabled = 0,
            trials_enabled = 0,
            renewals_enabled = 1,
            resumes_enabled = 0,
            device_upgrades_enabled = 0,
            extend_and_upgrade_enabled = 0,
            referrals_enabled = 0,
            reason_code = 'pre_advertising_freeze',
            operator_note = 'Applied automatically by migration 32 before advertising rollout',
            updated_at_utc = CURRENT_TIMESTAMP
        WHERE singleton_id = 1
          AND version = 1
          AND reason_code = 'initial_compatibility_policy'
          AND updated_by_admin_telegram_id IS NULL
        """,
    ]


def _refund_plan_schema_sql() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS refund_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT NOT NULL UNIQUE,
            refund_operation_id INTEGER NOT NULL UNIQUE,
            order_id INTEGER NOT NULL UNIQUE,
            subscription_id INTEGER NULL,
            expected_current_entitlement_version INTEGER NULL,
            previous_status TEXT NULL,
            previous_valid_until_utc TEXT NULL,
            previous_max_devices INTEGER NULL,
            target_status TEXT NOT NULL,
            target_valid_until_utc TEXT NULL,
            target_max_devices INTEGER NULL,
            source_order_kind TEXT NOT NULL,
            computation_version INTEGER NOT NULL,
            evidence_hash TEXT NOT NULL,
            state TEXT NOT NULL,
            confirmation_token_hash TEXT NULL UNIQUE,
            confirmation_expires_at_utc TEXT NULL,
            created_by_admin_telegram_id INTEGER NULL,
            confirmed_at_utc TEXT NULL,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            failure_code TEXT NULL,
            FOREIGN KEY(refund_operation_id) REFERENCES refund_operations(id),
            FOREIGN KEY(order_id) REFERENCES orders(id),
            FOREIGN KEY(subscription_id) REFERENCES subscriptions(id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_refund_plans_state ON refund_plans(state, updated_at_utc)",
        "CREATE INDEX IF NOT EXISTS ix_refund_plans_subscription ON refund_plans(subscription_id)",
    ]


def _acquisition_schema_sql() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS acquisition_campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_token TEXT NOT NULL UNIQUE,
            channel TEXT NOT NULL,
            placement TEXT NULL,
            creative TEXT NULL,
            landing_variant TEXT NULL,
            status TEXT NOT NULL,
            starts_at_utc TEXT NULL,
            ends_at_utc TEXT NULL,
            planned_spend_minor_units INTEGER NOT NULL DEFAULT 0,
            actual_spend_minor_units INTEGER NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'RUB',
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_acquisition_campaigns_status "
        "ON acquisition_campaigns(status)",
        """
        CREATE TABLE IF NOT EXISTS user_acquisition (
            user_id INTEGER PRIMARY KEY,
            first_campaign_id INTEGER NULL,
            first_touch_at_utc TEXT NULL,
            first_bot_key TEXT NULL,
            first_start_payload_hash TEXT NULL,
            last_campaign_id INTEGER NULL,
            last_touch_at_utc TEXT NULL,
            last_bot_key TEXT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(first_campaign_id) REFERENCES acquisition_campaigns(id),
            FOREIGN KEY(last_campaign_id) REFERENCES acquisition_campaigns(id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_user_acquisition_first_campaign "
        "ON user_acquisition(first_campaign_id)",
        "CREATE INDEX IF NOT EXISTS ix_user_acquisition_last_campaign "
        "ON user_acquisition(last_campaign_id)",
        """
        CREATE TABLE IF NOT EXISTS acquisition_touches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            campaign_id INTEGER NOT NULL,
            touched_at_utc TEXT NOT NULL,
            bot_key TEXT NULL,
            touch_kind TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(campaign_id) REFERENCES acquisition_campaigns(id),
            UNIQUE(user_id, campaign_id, touch_kind, payload_hash)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_acquisition_touches_campaign_time "
        "ON acquisition_touches(campaign_id, touched_at_utc)",
        "CREATE INDEX IF NOT EXISTS ix_acquisition_touches_user_time "
        "ON acquisition_touches(user_id, touched_at_utc)",
    ]


def _capacity_state_schema_sql() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS capacity_state_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            previous_state TEXT NULL,
            new_state TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            captured_at_utc TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_capacity_state_transitions_time "
        "ON capacity_state_transitions(captured_at_utc)",
    ]


def _multi_bot_schema_sql() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS telegram_bot_channels (
            bot_key TEXT PRIMARY KEY,
            telegram_bot_id INTEGER NOT NULL UNIQUE,
            username TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            required INTEGER NOT NULL,
            last_verified_at_utc TEXT NOT NULL,
            last_update_at_utc TEXT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS user_bot_channels (
            user_id INTEGER NOT NULL,
            bot_key TEXT NOT NULL,
            first_seen_at_utc TEXT NOT NULL,
            last_seen_at_utc TEXT NOT NULL,
            can_receive_messages INTEGER NOT NULL DEFAULT 1,
            blocked_at_utc TEXT NULL,
            PRIMARY KEY (user_id, bot_key),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(bot_key) REFERENCES telegram_bot_channels(bot_key)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_purchase_quotes_origin_bot
        ON purchase_quotes(origin_bot_key)
        """,
        "CREATE INDEX IF NOT EXISTS ix_orders_origin_bot ON orders(origin_bot_key)",
        """
        CREATE INDEX IF NOT EXISTS ix_onboarding_origin_bot
        ON onboarding_sessions(origin_bot_key)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_support_requests_origin_bot
        ON support_requests(origin_bot_key)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_notification_delivery_bot
        ON notification_deliveries(delivery_bot_key)
        """,
        "CREATE INDEX IF NOT EXISTS ix_audit_events_bot ON audit_events(bot_key)",
        "CREATE INDEX IF NOT EXISTS ix_product_events_bot ON product_events(bot_key)",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_support_messages_bot_chat_message
        ON support_messages(bot_key, telegram_chat_id, telegram_message_id)
        WHERE bot_key IS NOT NULL
        """,
    ]
