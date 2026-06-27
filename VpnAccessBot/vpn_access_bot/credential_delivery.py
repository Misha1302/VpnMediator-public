from __future__ import annotations

from dataclasses import dataclass
from string import Formatter
from urllib.parse import quote, urlsplit

COPY_TEXT_MAX_LENGTH = 256


@dataclass(frozen=True)
class CredentialDeliveryPlan:
    connection_url: str
    fallback_connection_url: str | None
    happ_deep_link: str | None
    can_copy: bool


def build_delivery_plan(
    connection_url: str,
    *,
    happ_deep_link_template: str | None,
    primary_subscription_base_url: str | None = None,
    fallback_subscription_base_url: str | None = None,
) -> CredentialDeliveryPlan:
    normalized_url = connection_url.strip()
    if not normalized_url:
        raise ValueError("Connection URL must not be empty.")

    parsed = urlsplit(normalized_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Connection URL must be an absolute HTTP(S) URL.")

    return CredentialDeliveryPlan(
        connection_url=normalized_url,
        fallback_connection_url=_build_fallback_url(
            normalized_url,
            primary_subscription_base_url,
            fallback_subscription_base_url,
        ),
        happ_deep_link=_build_optional_deep_link(
            happ_deep_link_template,
            normalized_url,
        ),
        can_copy=len(normalized_url) <= COPY_TEXT_MAX_LENGTH,
    )


def _build_fallback_url(
    connection_url: str,
    primary_base_url: str | None,
    fallback_base_url: str | None,
) -> str | None:
    if not fallback_base_url:
        return None

    fallback = urlsplit(fallback_base_url.rstrip("/"))
    if fallback.scheme != "https" or not fallback.netloc or fallback.query or fallback.fragment:
        raise ValueError("FALLBACK_SUBSCRIPTION_BASE_URL must be an absolute HTTPS URL.")

    connection = urlsplit(connection_url)
    primary = urlsplit(
        (primary_base_url or f"{connection.scheme}://{connection.netloc}").rstrip("/")
    )
    if connection.scheme != primary.scheme or connection.netloc != primary.netloc:
        raise ValueError("Connection URL does not belong to PUBLIC_SUBSCRIPTION_BASE_URL.")

    primary_path = primary.path.rstrip("/")
    if primary_path and not (
        connection.path == primary_path or connection.path.startswith(primary_path + "/")
    ):
        raise ValueError("Connection URL is outside PUBLIC_SUBSCRIPTION_BASE_URL path.")

    suffix = connection.path[len(primary_path) :] if primary_path else connection.path
    path = fallback.path.rstrip("/") + suffix
    return fallback._replace(path=path, query=connection.query, fragment="").geturl()


def _build_optional_deep_link(template: str | None, connection_url: str) -> str | None:
    if template is None or not template.strip():
        return None

    normalized_template = template.strip()
    fields = [field_name for _, field_name, _, _ in Formatter().parse(normalized_template)]
    if fields != ["url"]:
        raise ValueError("HAPP_DEEP_LINK_TEMPLATE must contain exactly one '{url}' field.")

    rendered = normalized_template.format(url=quote(connection_url, safe=""))
    parsed = urlsplit(rendered)
    if not parsed.scheme or parsed.scheme.lower() in {"http", "https", "javascript", "data"}:
        raise ValueError("HAPP_DEEP_LINK_TEMPLATE must render a non-HTTP application URI.")

    return rendered
