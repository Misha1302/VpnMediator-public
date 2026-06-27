"""initial closed beta schema

Revision ID: 20260605_0001
Revises:
Create Date: 2026-06-05

"""

from __future__ import annotations

from alembic import op
from vpn_access_bot.migrations import (
    _commercial_schema_sql,
    _idempotency_schema_sql,
    _initial_schema_sql,
)

revision = "20260605_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in _initial_schema_sql():
        op.execute(statement)

    for statement in _commercial_schema_sql():
        op.execute(statement)

    for statement in _idempotency_schema_sql():
        op.execute(statement)

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at_utc TEXT NOT NULL
        )
        """
    )
    op.execute(
        """
        INSERT OR IGNORE INTO schema_migrations(version, name, applied_at_utc)
        VALUES(1, 'initial_schema', datetime('now'))
        """
    )
    op.execute(
        """
        INSERT OR IGNORE INTO schema_migrations(version, name, applied_at_utc)
        VALUES(2, 'commercial_entitlement_quotes_orders', datetime('now'))
        """
    )
    op.execute(
        """
        INSERT OR IGNORE INTO schema_migrations(version, name, applied_at_utc)
        VALUES(3, 'order_payment_idempotency_indexes', datetime('now'))
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM schema_migrations WHERE version IN (1, 2, 3)")
    op.execute("DROP TABLE IF EXISTS audit_events")
    op.execute("DROP TABLE IF EXISTS notification_deliveries")
    op.execute("DROP TABLE IF EXISTS order_applications")
    op.execute("DROP TABLE IF EXISTS access_entitlements")
    op.execute("DROP TABLE IF EXISTS purchase_quotes")
