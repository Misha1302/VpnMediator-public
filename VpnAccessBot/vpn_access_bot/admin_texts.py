from __future__ import annotations


def admin_command_error(action: str) -> str:
    return f"Не удалось выполнить действие «{action}». Проверьте данные и повторите позже."


def admin_secret_redacted() -> str:
    return "секрет скрыт"
