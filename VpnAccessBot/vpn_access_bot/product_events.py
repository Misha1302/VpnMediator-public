from __future__ import annotations

from enum import StrEnum


class ProductEventName(StrEnum):
    FIRST_START = "first_start"
    CAMPAIGN_TOUCH = "campaign_touch"
    MAIN_MENU_SHOWN = "main_menu_shown"
    TRIAL_CTA_OPENED = "trial_cta_opened"
    TRIAL_ACTIVATION_STARTED = "trial_activation_started"
    TRIAL_ACTIVATED = "trial_activated"
    TRIAL_ACTIVATION_FAILED = "trial_activation_failed"
    TRIAL_EXPIRED = "trial_expired"
    TRIAL_CONVERTED = "trial_converted"
    PURCHASE_STARTED = "purchase_started"
    DEVICES_SELECTED = "devices_selected"
    PERIOD_SELECTED = "period_selected"
    QUOTE_SHOWN = "quote_shown"
    PAYMENT_STARTED = "payment_started"
    PAYMENT_COMPLETED = "payment_completed"
    ACTIVATION_STARTED = "activation_started"
    ACTIVATION_COMPLETED = "activation_completed"
    ACTIVATION_FAILED = "activation_failed"
    REFUND_STARTED = "refund_started"
    REFUND_COMPLETED = "refund_completed"
    CONNECT_DEVICE_STARTED = "connect_device_started"
    PLATFORM_SELECTED = "platform_selected"
    SUBSCRIPTION_FEED_ISSUED = "subscription_feed_issued"
    SUBSCRIPTION_OBSERVED_BY_CLIENT = "subscription_observed_by_client"
    ONBOARDING_COMPLETED = "onboarding_completed"
    ONBOARDING_ABANDONED = "onboarding_abandoned"
    DEVICE_LIMIT_DENIED = "device_limit_denied"
    RENEWAL_STARTED = "renewal_started"
    RENEWAL_SUCCEEDED = "renewal_succeeded"
    UPGRADE_STARTED = "upgrade_started"
    UPGRADE_SUCCEEDED = "upgrade_succeeded"
    SUPPORT_OPENED = "support_opened"
    SUPPORT_RESOLVED = "support_resolved"


FUNNEL_EVENT_ORDER: tuple[ProductEventName, ...] = (
    ProductEventName.FIRST_START,
    ProductEventName.TRIAL_ACTIVATED,
    ProductEventName.PAYMENT_COMPLETED,
    ProductEventName.ACTIVATION_COMPLETED,
    ProductEventName.SUBSCRIPTION_OBSERVED_BY_CLIENT,
    ProductEventName.ONBOARDING_COMPLETED,
    ProductEventName.REFUND_COMPLETED,
)
