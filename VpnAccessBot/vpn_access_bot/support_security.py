from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlsplit

_MAX_TEXT_LENGTH = 32_768
_MAX_DECODE_PASSES = 2
_MAX_VARIANTS = 32

_SECRET_PATTERNS = (
    re.compile(r"https?://\S+/sub/\S+", re.IGNORECASE),
    re.compile(r"(?:token|claim|admin[_-]?token)\s*[=:]\s*\S+", re.IGNORECASE),
    re.compile(r"X-Admin-Token\s*:\s*\S+", re.IGNORECASE),
    re.compile(r"%2fsub%2f", re.IGNORECASE),
    re.compile(r"(?:token|claim|admin(?:_|%5f|-)?token)%3d", re.IGNORECASE),
)


def _bounded_decode(value: str) -> list[str]:
    variants = [value]
    current = value
    for _ in range(_MAX_DECODE_PASSES):
        decoded = unquote(current)
        if decoded == current or len(decoded) > _MAX_TEXT_LENGTH:
            break
        variants.append(decoded)
        current = decoded
    return variants


def _candidate_variants(text: str) -> list[str]:
    pending = [text[:_MAX_TEXT_LENGTH]]
    variants: list[str] = []
    seen: set[str] = set()

    while pending and len(variants) < _MAX_VARIANTS:
        candidate = pending.pop(0)
        if candidate in seen:
            continue
        seen.add(candidate)
        variants.append(candidate)

        for decoded in _bounded_decode(candidate):
            if decoded not in seen:
                pending.append(decoded)

        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue

        if parsed.query:
            for key, value in parse_qsl(parsed.query, keep_blank_values=True):
                pending.extend((key, value))
        if parsed.fragment:
            pending.append(parsed.fragment)

    return variants


def contains_secret_material(text: str | None) -> bool:
    if not text:
        return False
    return any(
        pattern.search(candidate)
        for candidate in _candidate_variants(text)
        for pattern in _SECRET_PATTERNS
    )
