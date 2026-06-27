from datetime import timedelta
from types import SimpleNamespace

from vpn_access_bot.handlers.subscription import _subscription_status_label
from vpn_access_bot.models import utc_now


def _subscription(*, state: str = "healthy", status: str = "active", days: int = 5):
    return SimpleNamespace(
        reconciliation_state=state,
        status=status,
        expires_at=utc_now() + timedelta(days=days),
    )


def test_blocked_subscription_is_not_presented_as_active() -> None:
    assert _subscription_status_label(_subscription(state="blocked"), True) == "требует проверки"


def test_recovering_subscription_has_explicit_status() -> None:
    assert (
        _subscription_status_label(_subscription(state="recovering"), True) == "восстанавливается"
    )


def test_remote_disabled_state_overrides_healthy_local_label() -> None:
    assert _subscription_status_label(_subscription(), False) == "требует проверки"


def test_healthy_confirmed_subscription_remains_active() -> None:
    assert _subscription_status_label(_subscription(), True) == "активен"
