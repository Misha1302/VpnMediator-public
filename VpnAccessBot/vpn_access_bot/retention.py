from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vpn_access_bot.config import get_settings
from vpn_access_bot.db import Database
from vpn_access_bot.models import (
    BroadcastCampaign,
    BroadcastRecipient,
    NotificationDelivery,
    NotificationOutbox,
    OnboardingSession,
    ProductEvent,
    PurchaseQuote,
    TelegramUpdateInbox,
    utc_now,
)


@dataclass(frozen=True)
class CleanupResult:
    expired_quotes: int
    product_events: int
    onboarding_sessions: int
    notification_deliveries: int
    notification_outbox: int
    broadcast_recipients: int
    broadcast_campaigns: int
    telegram_update_inbox: int
    dry_run: bool


async def cleanup_once(
    session: AsyncSession,
    *,
    retention_days: int,
    dry_run: bool,
    broadcast_draft_retention_hours: int = 24,
) -> CleanupResult:
    now = utc_now()
    cutoff = now - timedelta(days=retention_days)
    predicates = {
        "expired_quotes": (
            PurchaseQuote,
            PurchaseQuote.expires_at_utc < cutoff,
            PurchaseQuote.consumed_at_utc.is_(None),
        ),
        "product_events": (ProductEvent, ProductEvent.occurred_at_utc < cutoff),
        "onboarding_sessions": (
            OnboardingSession,
            OnboardingSession.updated_at_utc < cutoff,
            OnboardingSession.status.in_(["completed", "abandoned"]),
        ),
        "notification_deliveries": (
            NotificationDelivery,
            NotificationDelivery.delivered_at_utc < cutoff,
            NotificationDelivery.status.in_(["delivered", "failed"]),
        ),
    }
    counts: dict[str, int] = {}
    for name, (model, *where) in predicates.items():
        count = await session.scalar(select(func.count()).select_from(model).where(*where))
        counts[name] = int(count or 0)
        if not dry_run and counts[name] > 0:
            await session.execute(delete(model).where(*where))

    outbox_where = (
        NotificationOutbox.updated_at_utc < cutoff,
        NotificationOutbox.state.in_(["provider_accepted", "terminal_failed"]),
    )
    outbox_count = await session.scalar(
        select(func.count()).select_from(NotificationOutbox).where(*outbox_where)
    )
    counts["notification_outbox"] = int(outbox_count or 0)
    if not dry_run and counts["notification_outbox"] > 0:
        await session.execute(delete(NotificationOutbox).where(*outbox_where))

    telegram_update_where = (
        TelegramUpdateInbox.updated_at_utc < cutoff,
        TelegramUpdateInbox.status.in_(["processed", "quarantined"]),
    )
    telegram_update_count = await session.scalar(
        select(func.count()).select_from(TelegramUpdateInbox).where(*telegram_update_where)
    )
    counts["telegram_update_inbox"] = int(telegram_update_count or 0)
    if not dry_run and counts["telegram_update_inbox"] > 0:
        await session.execute(delete(TelegramUpdateInbox).where(*telegram_update_where))

    if not dry_run:
        await session.execute(
            update(BroadcastCampaign)
            .where(
                BroadcastCampaign.state.in_(["preparing", "awaiting_confirmation"]),
                BroadcastCampaign.expires_at_utc < now,
            )
            .values(state="expired", updated_at_utc=now)
        )

    draft_cutoff = now - timedelta(hours=max(broadcast_draft_retention_hours, 1))
    campaign_without_outbox = (
        ~select(NotificationOutbox.id)
        .where(NotificationOutbox.broadcast_campaign_id == BroadcastCampaign.id)
        .exists()
    )
    removable_campaign = (
        (
            BroadcastCampaign.state.in_(["empty", "expired", "failed"])
            & (BroadcastCampaign.updated_at_utc < draft_cutoff)
        )
        | (
            BroadcastCampaign.state.in_(["preparing", "awaiting_confirmation"])
            & (BroadcastCampaign.expires_at_utc < now)
            & (BroadcastCampaign.updated_at_utc < draft_cutoff)
        )
        | ((BroadcastCampaign.state == "queued") & (BroadcastCampaign.updated_at_utc < cutoff))
    ) & campaign_without_outbox
    removable_campaign_ids = select(BroadcastCampaign.id).where(removable_campaign)

    recipient_count = await session.scalar(
        select(func.count())
        .select_from(BroadcastRecipient)
        .where(BroadcastRecipient.campaign_id.in_(removable_campaign_ids))
    )
    counts["broadcast_recipients"] = int(recipient_count or 0)
    campaign_count = await session.scalar(
        select(func.count()).select_from(BroadcastCampaign).where(removable_campaign)
    )
    counts["broadcast_campaigns"] = int(campaign_count or 0)
    if not dry_run:
        if counts["broadcast_recipients"] > 0:
            await session.execute(
                delete(BroadcastRecipient).where(
                    BroadcastRecipient.campaign_id.in_(removable_campaign_ids)
                )
            )
        if counts["broadcast_campaigns"] > 0:
            await session.execute(delete(BroadcastCampaign).where(removable_campaign))

    return CleanupResult(**counts, dry_run=dry_run)


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Apply documented non-financial retention policy.")
    parser.add_argument("--dry-run", action="store_true", help="Report without deleting rows.")
    parser.add_argument("--retention-days", type=int, default=None)
    args = parser.parse_args()
    settings = get_settings()
    retention_days = args.retention_days or settings.cleanup_retention_days
    if retention_days < 30:
        raise SystemExit("Retention must be at least 30 days.")

    database = Database(settings.database_url)
    await database.initialize()
    try:
        async with database.session() as session:
            result = await cleanup_once(
                session,
                retention_days=retention_days,
                dry_run=args.dry_run,
                broadcast_draft_retention_hours=settings.broadcast_draft_retention_hours,
            )
        print(result)
    finally:
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
