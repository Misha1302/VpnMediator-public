from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from vpn_access_bot.constants import (
    PAYMENT_MODE_TELEGRAM_PAYMENTS_LEGACY,
    PAYMENT_MODE_TELEGRAM_STARS,
    SUPPORTED_PAYMENT_MODES,
)

PaymentMode = Literal["manual", "telegram_stars"]


class TelegramBotDefinition(BaseModel):
    key: str
    token: SecretStr
    expected_username: str | None = None
    enabled: bool = True
    required: bool = True
    proxy_url: str | None = None

    @field_validator("key", mode="after")
    @classmethod
    def validate_key(cls, value: str) -> str:
        normalized = value.strip().lower()
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,31}", normalized) is None:
            raise ValueError("Telegram bot key must be a stable lowercase identifier.")
        return normalized

    @field_validator("expected_username", mode="after")
    @classmethod
    def normalize_username(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lstrip("@")
        return normalized or None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_proxy_url: str | None = Field(default=None, alias="TELEGRAM_PROXY_URL")
    telegram_expected_username: str | None = Field(default=None, alias="TELEGRAM_EXPECTED_USERNAME")
    telegram_bots_json: str | None = Field(default=None, alias="TELEGRAM_BOTS_JSON")
    default_bot_key: str = Field(default="razaltush", alias="DEFAULT_BOT_KEY")
    admin_telegram_ids: list[int] = Field(default_factory=list, alias="ADMIN_TELEGRAM_IDS")
    allow_test_user_reset: bool = Field(default=False, alias="ALLOW_TEST_USER_RESET")
    test_user_reset_telegram_ids: list[int] = Field(
        default_factory=list, alias="TEST_USER_RESET_TELEGRAM_IDS"
    )
    support_agent_telegram_ids: list[int] = Field(
        default_factory=list, alias="SUPPORT_AGENT_TELEGRAM_IDS"
    )
    app_env: str = Field(default="development", alias="APP_ENV")
    product_name: str = Field(default="Razaltush VPN", alias="PRODUCT_NAME")
    subscription_time_zone: str = Field(default="Europe/Moscow", alias="SUBSCRIPTION_TIME_ZONE")
    expiration_policy_version: str = Field(
        default="calendar-day-bonus-v2", alias="EXPIRATION_POLICY_VERSION"
    )
    expiration_policy_effective_at_utc: datetime = Field(
        default=datetime(2026, 6, 7, tzinfo=UTC),
        alias="EXPIRATION_POLICY_EFFECTIVE_AT_UTC",
    )

    payment_mode: PaymentMode = Field(default="manual", alias="PAYMENT_MODE")
    yookassa_integration_enabled: bool = Field(default=False, alias="YOOKASSA_INTEGRATION_ENABLED")
    external_payment_enabled: bool = Field(default=False, alias="EXTERNAL_PAYMENT_ENABLED")
    checkout_public_base_url: str | None = Field(default=None, alias="CHECKOUT_PUBLIC_BASE_URL")
    checkout_bind_host: str = Field(default="127.0.0.1", alias="CHECKOUT_BIND_HOST")
    checkout_bind_port: int = Field(default=8082, alias="CHECKOUT_BIND_PORT")
    checkout_token_secret: SecretStr = Field(default=SecretStr(""), alias="CHECKOUT_TOKEN_SECRET")
    yookassa_shop_id: str = Field(default="", alias="YOOKASSA_SHOP_ID")
    yookassa_secret_key: SecretStr = Field(default=SecretStr(""), alias="YOOKASSA_SECRET_KEY")
    yookassa_api_base_url: str = Field(
        default="https://api.yookassa.ru/v3", alias="YOOKASSA_API_BASE_URL"
    )
    yookassa_return_url: str | None = Field(default=None, alias="YOOKASSA_RETURN_URL")
    yookassa_webhook_path_secret: SecretStr = Field(
        default=SecretStr(""), alias="YOOKASSA_WEBHOOK_PATH_SECRET"
    )
    yookassa_request_timeout_seconds: float = Field(
        default=15.0, alias="YOOKASSA_REQUEST_TIMEOUT_SECONDS"
    )
    database_url: str = Field(default="sqlite+aiosqlite:///./data/vpn_bot.db", alias="DATABASE_URL")

    mediator_base_url: str = Field(default="http://127.0.0.1:5062", alias="MEDIATOR_BASE_URL")
    mediator_admin_token: str = Field(alias="MEDIATOR_ADMIN_TOKEN")
    public_subscription_base_url: str | None = Field(
        default=None, alias="PUBLIC_SUBSCRIPTION_BASE_URL"
    )
    fallback_subscription_base_url: str | None = Field(
        default=None, alias="FALLBACK_SUBSCRIPTION_BASE_URL"
    )
    happ_deep_link_template: str | None = Field(default=None, alias="HAPP_DEEP_LINK_TEMPLATE")

    purchasable_period_options: str = Field(default="1,3,6,12", alias="PURCHASABLE_PERIOD_OPTIONS")
    purchasable_device_options: str = Field(
        default="1,2,3,4,5,6,7,8,9,10,11,12", alias="PURCHASABLE_DEVICE_OPTIONS"
    )
    quote_ttl_minutes: int = Field(default=20, alias="QUOTE_TTL_MINUTES")
    order_ttl_minutes: int = Field(default=60, alias="ORDER_TTL_MINUTES")
    checkout_authorization_grace_seconds: int = Field(
        default=600, alias="CHECKOUT_AUTHORIZATION_GRACE_SECONDS"
    )
    payment_reconciliation_interval_seconds: int = Field(
        default=5, alias="PAYMENT_RECONCILIATION_INTERVAL_SECONDS"
    )
    telegram_update_concurrency_limit: int = Field(
        default=32, alias="TELEGRAM_UPDATE_CONCURRENCY_LIMIT"
    )
    telegram_update_inbox_poll_interval_seconds: float = Field(
        default=0.25, alias="TELEGRAM_UPDATE_INBOX_POLL_INTERVAL_SECONDS"
    )
    telegram_update_retry_base_seconds: int = Field(
        default=1, alias="TELEGRAM_UPDATE_RETRY_BASE_SECONDS"
    )
    telegram_update_retry_max_attempts: int = Field(
        default=8, alias="TELEGRAM_UPDATE_RETRY_MAX_ATTEMPTS"
    )
    telegram_update_lease_seconds: int = Field(default=60, alias="TELEGRAM_UPDATE_LEASE_SECONDS")
    pricing_version: str = Field(default="closed-beta-2026-06-v2", alias="PRICING_VERSION")
    pricing_base_device_month_stars: int = Field(
        default=60, alias="PRICING_BASE_DEVICE_MONTH_STARS"
    )
    pricing_base_device_month_rub_kopecks: int = Field(
        default=19900, alias="PRICING_BASE_DEVICE_MONTH_RUB_KOPECKS"
    )
    pricing_duration_discounts: str = Field(
        default="1:0,3:10,6:20,12:30", alias="PRICING_DURATION_DISCOUNTS"
    )
    trial_enabled: bool = Field(default=True, alias="TRIAL_ENABLED")
    readiness_cache_seconds: int = Field(default=10, alias="READINESS_CACHE_SECONDS")
    pre_checkout_total_timeout_seconds: float = Field(
        default=8.0, alias="PRE_CHECKOUT_TOTAL_TIMEOUT_SECONDS"
    )
    pre_checkout_answer_reserve_seconds: float = Field(
        default=2.0, alias="PRE_CHECKOUT_ANSWER_RESERVE_SECONDS"
    )
    pre_checkout_readiness_timeout_seconds: float = Field(
        default=1.5, alias="PRE_CHECKOUT_READINESS_TIMEOUT_SECONDS"
    )
    onboarding_reminder_delay_minutes: int = Field(
        default=15, alias="ONBOARDING_REMINDER_DELAY_MINUTES"
    )
    onboarding_stale_hours: int = Field(default=72, alias="ONBOARDING_STALE_HOURS")

    device_reset_cooldown_hours: int = Field(default=12, alias="DEVICE_RESET_COOLDOWN_HOURS")
    expiration_check_interval_seconds: int = Field(
        default=1800, alias="EXPIRATION_CHECK_INTERVAL_SECONDS"
    )
    entitlement_reconciliation_interval_seconds: int = Field(
        default=300, alias="ENTITLEMENT_RECONCILIATION_INTERVAL_SECONDS"
    )
    notification_check_interval_seconds: int = Field(
        default=1800, alias="NOTIFICATION_CHECK_INTERVAL_SECONDS"
    )
    referral_check_interval_seconds: int = Field(
        default=300, alias="REFERRAL_CHECK_INTERVAL_SECONDS"
    )
    referral_reward_hold_hours: int = Field(default=24, alias="REFERRAL_REWARD_HOLD_HOURS")
    referral_enabled: bool = Field(default=False, alias="REFERRAL_ENABLED")
    configured_subscription_capacity: int = Field(
        default=0, alias="CONFIGURED_SUBSCRIPTION_CAPACITY"
    )
    configured_device_capacity: int = Field(default=0, alias="CONFIGURED_DEVICE_CAPACITY")
    capacity_constrained_ratio: float = Field(default=0.70, alias="CAPACITY_CONSTRAINED_RATIO")
    capacity_saturated_ratio: float = Field(default=0.85, alias="CAPACITY_SATURATED_RATIO")
    capacity_recovery_ratio: float = Field(default=0.65, alias="CAPACITY_RECOVERY_RATIO")
    capacity_min_dwell_seconds: int = Field(default=300, alias="CAPACITY_MIN_DWELL_SECONDS")
    capacity_backlog_slo_seconds: int = Field(default=300, alias="CAPACITY_BACKLOG_SLO_SECONDS")
    worker_stale_after_seconds: int = Field(default=900, alias="WORKER_STALE_AFTER_SECONDS")
    capacity_notification_backlog_limit: int = Field(
        default=100, alias="CAPACITY_NOTIFICATION_BACKLOG_LIMIT"
    )
    capacity_refund_manual_review_limit: int = Field(
        default=1, alias="CAPACITY_REFUND_MANUAL_REVIEW_LIMIT"
    )
    capacity_worker_stale_limit: int = Field(default=1, alias="CAPACITY_WORKER_STALE_LIMIT")
    refund_confirmation_ttl_minutes: int = Field(default=5, alias="REFUND_CONFIRMATION_TTL_MINUTES")
    public_telegram_bot_username: str = Field(
        default="@RazaltushVpnBot", alias="PUBLIC_TELEGRAM_BOT_USERNAME"
    )
    support_chat_id: int | None = Field(default=None, alias="SUPPORT_CHAT_ID")
    support_contact: str | None = Field(default=None, alias="SUPPORT_CONTACT")
    health_bind_host: str = Field(default="127.0.0.1", alias="HEALTH_BIND_HOST")
    health_bind_port: int = Field(default=8081, alias="HEALTH_BIND_PORT")
    instance_lock_path: str = Field(
        default="./data/vpn-access-bot.lock", alias="INSTANCE_LOCK_PATH"
    )
    critical_worker_failure_limit: int = Field(default=5, alias="CRITICAL_WORKER_FAILURE_LIMIT")
    broadcast_confirmation_ttl_minutes: int = Field(
        default=15, alias="BROADCAST_CONFIRMATION_TTL_MINUTES"
    )
    broadcast_max_delivery_attempts: int = Field(default=8, alias="BROADCAST_MAX_DELIVERY_ATTEMPTS")
    broadcast_retry_base_seconds: int = Field(default=30, alias="BROADCAST_RETRY_BASE_SECONDS")
    broadcast_retry_max_seconds: int = Field(default=3600, alias="BROADCAST_RETRY_MAX_SECONDS")
    notification_outbox_max_delivery_attempts: int = Field(
        default=8,
        alias="NOTIFICATION_OUTBOX_MAX_DELIVERY_ATTEMPTS",
    )
    notification_outbox_retry_base_seconds: int = Field(
        default=30,
        alias="NOTIFICATION_OUTBOX_RETRY_BASE_SECONDS",
    )
    notification_outbox_retry_max_seconds: int = Field(
        default=3600,
        alias="NOTIFICATION_OUTBOX_RETRY_MAX_SECONDS",
    )
    broadcast_draft_retention_hours: int = Field(
        default=24, alias="BROADCAST_DRAFT_RETENTION_HOURS"
    )
    cleanup_retention_days: int = Field(default=90, alias="CLEANUP_RETENTION_DAYS")
    log_directory: str = Field(default="./logs", alias="LOG_DIRECTORY")
    log_retention_days: int = Field(default=14, alias="LOG_RETENTION_DAYS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("subscription_time_zone", mode="after")
    @classmethod
    def validate_subscription_time_zone(cls, value: str) -> str:
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except ZoneInfoNotFoundError as exception:
            raise ValueError("SUBSCRIPTION_TIME_ZONE must be a valid IANA timezone.") from exception
        return normalized

    @field_validator("expiration_policy_effective_at_utc", mode="after")
    @classmethod
    def normalize_policy_effective_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("EXPIRATION_POLICY_EFFECTIVE_AT_UTC must include a timezone.")
        return value.astimezone(UTC)

    @field_validator("public_telegram_bot_username", mode="after")
    @classmethod
    def normalize_public_bot_username(cls, value: str) -> str:
        normalized = value.strip().lstrip("@")
        if re.fullmatch(r"[A-Za-z0-9_]{5,32}", normalized) is None:
            raise ValueError("PUBLIC_TELEGRAM_BOT_USERNAME must be a valid Telegram username.")
        if normalized.casefold() != "razaltushvpnbot".casefold():
            raise ValueError("The canonical public bot username is @RazaltushVpnBot.")
        return f"@{normalized}"

    @field_validator(
        "configured_subscription_capacity",
        "configured_device_capacity",
        "capacity_backlog_slo_seconds",
        "capacity_min_dwell_seconds",
        "worker_stale_after_seconds",
        "refund_confirmation_ttl_minutes",
        mode="after",
    )
    @classmethod
    def validate_non_negative_capacity_values(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Capacity and timeout values must be non-negative.")
        return value

    @field_validator(
        "capacity_constrained_ratio",
        "capacity_saturated_ratio",
        "capacity_recovery_ratio",
        mode="after",
    )
    @classmethod
    def validate_capacity_ratio(cls, value: float) -> float:
        if value <= 0 or value > 1:
            raise ValueError("Capacity ratios must be in the interval (0, 1].")
        return value

    @model_validator(mode="after")
    def validate_capacity_threshold_order(self) -> Settings:
        if self.capacity_recovery_ratio >= self.capacity_constrained_ratio:
            raise ValueError("CAPACITY_RECOVERY_RATIO must be below CAPACITY_CONSTRAINED_RATIO.")
        if self.capacity_constrained_ratio >= self.capacity_saturated_ratio:
            raise ValueError("CAPACITY_CONSTRAINED_RATIO must be below CAPACITY_SATURATED_RATIO.")
        return self

    @field_validator("app_env", mode="after")
    @classmethod
    def normalize_app_env(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"development", "test", "staging", "production"}:
            raise ValueError("APP_ENV must be development, test, staging, or production.")
        return normalized

    @field_validator(
        "admin_telegram_ids",
        "support_agent_telegram_ids",
        "test_user_reset_telegram_ids",
        mode="before",
    )
    @classmethod
    def parse_actor_ids(cls, value: object) -> list[int]:
        if value is None or value == "":
            return []

        if isinstance(value, list):
            return [int(item) for item in value]

        if isinstance(value, str):
            return [int(part.strip()) for part in value.split(",") if part.strip()]

        raise ValueError("Telegram actor ID lists must contain comma-separated integers.")

    @field_validator(
        "mediator_base_url",
        "public_subscription_base_url",
        "fallback_subscription_base_url",
        "happ_deep_link_template",
        "checkout_public_base_url",
        "yookassa_return_url",
        "yookassa_api_base_url",
        mode="after",
    )
    @classmethod
    def strip_trailing_slash(cls, value: str | None) -> str | None:
        if value is None:
            return None

        return value.rstrip("/")

    @field_validator("payment_mode", mode="before")
    @classmethod
    def validate_payment_mode(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("PAYMENT_MODE must be either 'manual' or 'telegram_stars'.")

        normalized = value.strip().lower()

        if normalized == PAYMENT_MODE_TELEGRAM_PAYMENTS_LEGACY:
            return PAYMENT_MODE_TELEGRAM_STARS

        if normalized not in SUPPORTED_PAYMENT_MODES:
            raise ValueError("PAYMENT_MODE must be either 'manual' or 'telegram_stars'.")

        return normalized

    @field_validator("product_name", mode="after")
    @classmethod
    def validate_product_name(cls, value: str) -> str:
        normalized = value.strip()

        if not 2 <= len(normalized) <= 32:
            raise ValueError("PRODUCT_NAME must contain from 2 to 32 characters.")

        return normalized

    @field_validator(
        "quote_ttl_minutes",
        "order_ttl_minutes",
        "checkout_authorization_grace_seconds",
        "payment_reconciliation_interval_seconds",
        "telegram_update_concurrency_limit",
        "telegram_update_retry_base_seconds",
        "telegram_update_retry_max_attempts",
        "telegram_update_lease_seconds",
        "expiration_check_interval_seconds",
        "entitlement_reconciliation_interval_seconds",
        "notification_check_interval_seconds",
        "referral_check_interval_seconds",
        "referral_reward_hold_hours",
        "readiness_cache_seconds",
        "onboarding_reminder_delay_minutes",
        "onboarding_stale_hours",
        "health_bind_port",
        "checkout_bind_port",
        "critical_worker_failure_limit",
        "broadcast_confirmation_ttl_minutes",
        "broadcast_max_delivery_attempts",
        "broadcast_retry_base_seconds",
        "broadcast_retry_max_seconds",
        "notification_outbox_max_delivery_attempts",
        "notification_outbox_retry_base_seconds",
        "notification_outbox_retry_max_seconds",
        "broadcast_draft_retention_hours",
        "cleanup_retention_days",
        "log_retention_days",
        "capacity_notification_backlog_limit",
        "capacity_refund_manual_review_limit",
        "capacity_worker_stale_limit",
        mode="after",
    )
    @classmethod
    def validate_positive_int(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Purchase limits and TTL values must be positive.")

        return value

    @field_validator(
        "telegram_update_inbox_poll_interval_seconds",
        "pre_checkout_total_timeout_seconds",
        "pre_checkout_answer_reserve_seconds",
        "pre_checkout_readiness_timeout_seconds",
        "yookassa_request_timeout_seconds",
        mode="after",
    )
    @classmethod
    def validate_positive_float(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("Timeout and polling interval values must be positive.")
        return value

    @field_validator("default_bot_key", mode="after")
    @classmethod
    def validate_default_bot_key(cls, value: str) -> str:
        normalized = value.strip().lower()
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,31}", normalized) is None:
            raise ValueError("DEFAULT_BOT_KEY must be a stable lowercase identifier.")
        return normalized

    @field_validator("log_level", mode="after")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL is invalid.")
        return normalized

    def telegram_bot_definitions(self) -> list[TelegramBotDefinition]:
        definitions: list[TelegramBotDefinition] = []
        if self.telegram_bots_json:
            payload = json.loads(self.telegram_bots_json)
            if not isinstance(payload, list):
                raise ValueError("TELEGRAM_BOTS_JSON must contain a JSON array.")
            definitions.extend(TelegramBotDefinition.model_validate(item) for item in payload)
        else:
            indexes: set[int] = set()
            prefix = "TELEGRAM_BOTS__"
            for name in os.environ:
                if not name.startswith(prefix):
                    continue
                suffix = name[len(prefix) :]
                first, separator, _ = suffix.partition("__")
                if separator and first.isdigit():
                    indexes.add(int(first))
            for index in sorted(indexes):
                env_prefix = f"{prefix}{index}__"
                key = os.getenv(f"{env_prefix}KEY", "").strip()
                token = os.getenv(f"{env_prefix}TOKEN", "").strip()
                if not key or not token:
                    raise ValueError(f"Telegram bot definition {index} requires KEY and TOKEN.")
                definitions.append(
                    TelegramBotDefinition(
                        key=key,
                        token=token,
                        expected_username=os.getenv(f"{env_prefix}EXPECTED_USERNAME"),
                        enabled=os.getenv(f"{env_prefix}ENABLED", "true").lower()
                        not in {"0", "false", "no"},
                        required=os.getenv(f"{env_prefix}REQUIRED", "true").lower()
                        not in {"0", "false", "no"},
                        proxy_url=os.getenv(f"{env_prefix}PROXY_URL"),
                    )
                )

        if not definitions and self.telegram_bot_token.strip():
            definitions.append(
                TelegramBotDefinition(
                    key=self.default_bot_key,
                    token=self.telegram_bot_token,
                    expected_username=self.telegram_expected_username,
                    proxy_url=self.telegram_proxy_url,
                )
            )

        enabled = [definition for definition in definitions if definition.enabled]
        if not enabled:
            raise ValueError("At least one enabled Telegram bot must be configured.")
        keys = [definition.key for definition in enabled]
        if len(keys) != len(set(keys)):
            raise ValueError("Telegram bot keys must be unique.")
        return enabled

    @model_validator(mode="after")
    def validate_production_configuration(self) -> Settings:
        self.telegram_bot_definitions()
        if not 4.0 <= self.pre_checkout_total_timeout_seconds <= 9.0:
            raise ValueError("PRE_CHECKOUT_TOTAL_TIMEOUT_SECONDS must be between 4 and 9 seconds.")
        if self.pre_checkout_answer_reserve_seconds >= self.pre_checkout_total_timeout_seconds:
            raise ValueError(
                "PRE_CHECKOUT_ANSWER_RESERVE_SECONDS must be less than the total timeout."
            )
        validation_budget = (
            self.pre_checkout_total_timeout_seconds - self.pre_checkout_answer_reserve_seconds
        )
        if self.pre_checkout_readiness_timeout_seconds >= validation_budget:
            raise ValueError(
                "PRE_CHECKOUT_READINESS_TIMEOUT_SECONDS must leave time for order validation."
            )
        if self.allow_test_user_reset:
            if not self.test_user_reset_telegram_ids:
                raise ValueError(
                    "TEST_USER_RESET_TELEGRAM_IDS must not be empty when "
                    "ALLOW_TEST_USER_RESET is enabled."
                )
            unexpected_ids = set(self.test_user_reset_telegram_ids) - set(self.admin_telegram_ids)
            if unexpected_ids:
                raise ValueError(
                    "TEST_USER_RESET_TELEGRAM_IDS must be a subset of ADMIN_TELEGRAM_IDS."
                )
        if self.external_payment_enabled and not self.yookassa_integration_enabled:
            raise ValueError(
                "YOOKASSA_INTEGRATION_ENABLED must be true when external payment is enabled."
            )
        if self.yookassa_integration_enabled:
            if self.checkout_public_base_url is None:
                raise ValueError(
                    "CHECKOUT_PUBLIC_BASE_URL is required when external payment is enabled."
                )
            if self.yookassa_return_url is None:
                raise ValueError(
                    "YOOKASSA_RETURN_URL is required when external payment is enabled."
                )
            if len(self.checkout_token_secret.get_secret_value().encode("utf-8")) < 32:
                raise ValueError(
                    "CHECKOUT_TOKEN_SECRET must contain at least 32 bytes when "
                    "external payment is enabled."
                )
            if not self.yookassa_shop_id.strip() or not self.yookassa_secret_key.get_secret_value():
                raise ValueError(
                    "YooKassa credentials are required when external payment is enabled."
                )
            if len(self.yookassa_webhook_path_secret.get_secret_value()) < 24:
                raise ValueError(
                    "YOOKASSA_WEBHOOK_PATH_SECRET must contain at least 24 characters."
                )
            if (
                re.fullmatch(
                    r"[A-Za-z0-9_-]{24,128}",
                    self.yookassa_webhook_path_secret.get_secret_value(),
                )
                is None
            ):
                raise ValueError("YOOKASSA_WEBHOOK_PATH_SECRET must be URL-safe.")
            if self.checkout_bind_port == self.health_bind_port:
                raise ValueError("CHECKOUT_BIND_PORT must differ from HEALTH_BIND_PORT.")
        if self.app_env != "production":
            return self

        failures: list[str] = []
        placeholders = ("change-me", "replace-with", "placeholder", "test-token", "your_")

        def require_secret(value: str, name: str, minimum: int = 24) -> None:
            normalized = value.strip()
            if len(normalized) < minimum or any(
                marker in normalized.lower() for marker in placeholders
            ):
                failures.append(f"{name} must be a strong non-placeholder secret.")

        def require_https(value: str | None, name: str) -> None:
            if value is None or not value.startswith("https://"):
                failures.append(f"{name} must be an absolute HTTPS URL in production.")

        for bot_definition in self.telegram_bot_definitions():
            require_secret(
                bot_definition.token.get_secret_value(),
                f"TELEGRAM_BOTS[{bot_definition.key}].TOKEN",
                minimum=30,
            )
        require_secret(self.mediator_admin_token, "MEDIATOR_ADMIN_TOKEN")
        if not self.admin_telegram_ids:
            failures.append("ADMIN_TELEGRAM_IDS must not be empty in production.")
        if not self.support_agent_telegram_ids:
            failures.append("SUPPORT_AGENT_TELEGRAM_IDS must not be empty in production.")
        if self.support_chat_id is None:
            failures.append("SUPPORT_CHAT_ID must be configured in production.")
        if self.payment_mode != PAYMENT_MODE_TELEGRAM_STARS:
            failures.append("PAYMENT_MODE must be telegram_stars in production.")
        if self.yookassa_integration_enabled:
            require_https(self.checkout_public_base_url, "CHECKOUT_PUBLIC_BASE_URL")
            require_https(self.yookassa_return_url, "YOOKASSA_RETURN_URL")
            require_https(self.yookassa_api_base_url, "YOOKASSA_API_BASE_URL")
            require_secret(
                self.checkout_token_secret.get_secret_value(), "CHECKOUT_TOKEN_SECRET", 32
            )
            require_secret(self.yookassa_secret_key.get_secret_value(), "YOOKASSA_SECRET_KEY", 24)
            require_secret(
                self.yookassa_webhook_path_secret.get_secret_value(),
                "YOOKASSA_WEBHOOK_PATH_SECRET",
                24,
            )
            if not self.yookassa_shop_id.strip():
                failures.append(
                    "YOOKASSA_SHOP_ID must be configured when external payment is enabled."
                )
            elif re.fullmatch(r"[0-9]{4,32}", self.yookassa_shop_id.strip()) is None:
                failures.append("YOOKASSA_SHOP_ID must be a numeric shop identifier.")
            if self.yookassa_api_base_url.rstrip("/") != "https://api.yookassa.ru/v3":
                failures.append("YOOKASSA_API_BASE_URL must be https://api.yookassa.ru/v3.")
            checkout_url = urlparse(self.checkout_public_base_url or "")
            return_url = urlparse(self.yookassa_return_url or "")
            if (
                checkout_url.scheme != return_url.scheme
                or checkout_url.netloc != return_url.netloc
                or return_url.path != "/payment/return"
                or return_url.query
                or return_url.fragment
            ):
                failures.append(
                    "YOOKASSA_RETURN_URL must use the checkout host and /payment/return path."
                )
            if (
                checkout_url.username is not None
                or checkout_url.password is not None
                or checkout_url.path not in {"", "/"}
                or checkout_url.query
                or checkout_url.fragment
            ):
                failures.append("CHECKOUT_PUBLIC_BASE_URL must not include a path or query.")
            if self.checkout_bind_host not in {"127.0.0.1", "::1"}:
                failures.append("CHECKOUT_BIND_HOST must be loopback in production.")
        require_https(self.public_subscription_base_url, "PUBLIC_SUBSCRIPTION_BASE_URL")
        if not self.support_contact or "placeholder" in self.support_contact.lower():
            failures.append("SUPPORT_CONTACT must be configured in production.")
        if ":memory:" in self.database_url or self.database_url.endswith("/./data/vpn_bot.db"):
            failures.append("DATABASE_URL must point to a persistent production path.")
        if self.health_bind_host not in {"127.0.0.1", "::1"}:
            failures.append("HEALTH_BIND_HOST must be loopback in production.")
        if self.fallback_subscription_base_url is not None:
            require_https(self.fallback_subscription_base_url, "FALLBACK_SUBSCRIPTION_BASE_URL")
            if self.fallback_subscription_base_url == self.public_subscription_base_url:
                failures.append(
                    "Fallback subscription URL must be independent from the primary URL."
                )

        if failures:
            raise ValueError("Production configuration is invalid: " + " ".join(failures))
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
