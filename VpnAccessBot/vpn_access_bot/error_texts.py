from __future__ import annotations

from vpn_access_bot.commerce import ERROR_MESSAGES, UserErrorCode


def user_error_text(code: UserErrorCode) -> str:
    return ERROR_MESSAGES[code]
