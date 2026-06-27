from __future__ import annotations

import httpx
import pytest

from vpn_access_bot.mediator_client import MediatorClient, MediatorClientError


@pytest.mark.asyncio
async def test_transport_failure_is_wrapped_as_mediator_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection failed.", request=request)

    client = MediatorClient(
        base_url="http://mediator.test",
        admin_token="test-token",
        transport=httpx.MockTransport(handler),
    )

    try:
        with pytest.raises(MediatorClientError) as caught:
            await client.list_device_tokens(
                "00000000-0000-0000-0000-000000000001",
            )
    finally:
        await client.close()

    assert caught.value.status_code is None
    assert caught.value.error_code == "mediator_unavailable"


@pytest.mark.asyncio
async def test_entitlement_operation_provenance_is_queried_by_result_version() -> None:
    seen_request: httpx.Request | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = request
        return httpx.Response(
            200,
            request=request,
            json={
                "status": "applied",
                "operationId": "operation-1",
                "publicGuid": "00000000-0000-0000-0000-000000000001",
                "operationType": "admin_revoke",
                "expectedVersion": 1,
                "resultVersion": 2,
                "resultStatus": "disabled",
                "resultValidUntilUtc": "2026-07-01T00:00:00Z",
                "resultMaxDeviceTokens": 3,
                "appliedAtUtc": "2026-06-10T00:00:00Z",
            },
        )

    client = MediatorClient(
        base_url="http://mediator.test",
        admin_token="test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.get_entitlement_operation_by_result_version(
            "00000000-0000-0000-0000-000000000001",
            2,
        )
    finally:
        await client.close()

    assert result is not None
    assert result.operation_type == "admin_revoke"
    assert result.result_version == 2
    assert seen_request is not None
    assert seen_request.url.path.endswith("/entitlement-operations/by-result-version/2")
    assert seen_request.headers["X-Admin-Token"] == "test-token"


@pytest.mark.asyncio
async def test_missing_entitlement_operation_provenance_returns_none() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request, text="not found")

    client = MediatorClient(
        base_url="http://mediator.test",
        admin_token="test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.get_entitlement_operation_by_result_version(
            "00000000-0000-0000-0000-000000000001",
            2,
        )
    finally:
        await client.close()

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {
            "status": "applied",
            "operationId": "operation-1",
            "publicGuid": "00000000-0000-0000-0000-000000000001",
            "operationType": "admin_revoke",
            "expectedVersion": "not-a-number",
            "resultVersion": 2,
            "resultStatus": "disabled",
            "resultMaxDeviceTokens": 3,
            "appliedAtUtc": "2026-06-10T00:00:00Z",
        },
    ],
)
async def test_invalid_entitlement_operation_payload_is_rejected(payload: object) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json=payload)

    client = MediatorClient(
        base_url="http://mediator.test",
        admin_token="test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(MediatorClientError) as caught:
            await client.get_entitlement_operation_by_result_version(
                "00000000-0000-0000-0000-000000000001",
                2,
            )
    finally:
        await client.close()

    assert caught.value.error_code == "invalid_response"


@pytest.mark.asyncio
async def test_readiness_uses_internal_authenticated_endpoint_and_parses_capabilities() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/health/ready"
        assert request.headers["X-Admin-Token"] == "test-token"
        return httpx.Response(
            200,
            request=request,
            json={
                "status": "ready",
                "catalogState": "fresh",
                "serverCount": 3,
                "migrationsApplied": 18,
                "expectedMigrations": 18,
                "migrationsCurrent": True,
                "deviceFeedPolicyVersion": 1,
                "deviceFeedBindingMode": "observe",
                "defaultNewDeviceFeedPolicy": "legacy",
                "effectiveDeviceSecurityPosture": "observing",
                "requiredDeviceSecurityPosture": "observe",
                "deviceIdentityHashConfigured": True,
                "unifiedSubscriptionFeedEnabled": True,
                "sharedSubscriptionLinksOnly": True,
            },
        )

    client = MediatorClient(
        base_url="http://mediator.test",
        admin_token="test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.get_readiness()
    finally:
        await client.close()

    assert result.device_feed_policy_version == 1
    assert result.device_feed_binding_mode == "observe"
    assert result.default_new_device_feed_policy == "legacy"
    assert result.device_security_posture == "observing"
    assert result.required_device_security_posture == "observe"
    assert result.identity_hash_configured is True
    assert result.shared_subscription_links_only is True


@pytest.mark.asyncio
async def test_device_list_defaults_old_mediator_response_to_legacy_policy() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json=[
                {
                    "publicId": "device-1",
                    "displayName": "Old device",
                    "state": "active",
                }
            ],
        )

    client = MediatorClient(
        base_url="http://mediator.test",
        admin_token="test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.list_device_tokens("00000000-0000-0000-0000-000000000001")
    finally:
        await client.close()

    assert result[0].feed_policy_mode == "legacy"
    assert result[0].binding_state == "grandfathered"
    assert result[0].bound_platform is None


@pytest.mark.asyncio
async def test_device_list_parses_hwid_identity_binding_without_exposing_hash() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json=[
                {
                    "publicId": "device-2",
                    "displayName": "Happ · Android",
                    "state": "active",
                    "feedPolicyVersion": 2,
                    "feedPolicyMode": "enforce",
                    "bindingState": "bound",
                    "boundPlatform": "android",
                    "identityBound": True,
                    "identitySource": "happ_hwid",
                    "lastIdentitySeenAtUtc": "2026-06-12T12:00:00Z",
                }
            ],
        )

    client = MediatorClient(
        base_url="http://mediator.test",
        admin_token="test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.list_device_tokens("00000000-0000-0000-0000-000000000001")
    finally:
        await client.close()

    assert result[0].feed_policy_version == 2
    assert result[0].identity_bound is True
    assert result[0].identity_source == "happ_hwid"
    assert result[0].last_identity_seen_at_utc == "2026-06-12T12:00:00Z"


@pytest.mark.asyncio
async def test_ensure_subscription_feed_uses_stable_admin_endpoint_and_public_base() -> None:
    seen_request: httpx.Request | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = request
        return httpx.Response(
            201,
            request=request,
            json={
                "status": "created",
                "connectionUrl": (
                    "http://127.0.0.1:5000/sub/"
                    "00000000-0000-0000-0000-000000000001/feed?token=fake-secret"
                ),
                "created": True,
            },
        )

    client = MediatorClient(
        base_url="http://127.0.0.1:5000",
        admin_token="test-token",
        public_subscription_base_url="https://vpn.example",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.ensure_subscription_feed("00000000-0000-0000-0000-000000000001")
    finally:
        await client.close()

    assert result.status == "created"
    assert result.created is True
    assert result.connection_url == (
        "https://vpn.example/sub/00000000-0000-0000-0000-000000000001/feed?token=fake-secret"
    )
    assert seen_request is not None
    assert seen_request.method == "POST"
    assert seen_request.url.path.endswith("/feed-credential/ensure")
    assert seen_request.headers["X-Admin-Token"] == "test-token"


@pytest.mark.asyncio
async def test_device_list_parses_unified_feed_state() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json=[
                {
                    "publicId": "device-3",
                    "displayName": "Test phone",
                    "state": "revoked",
                    "accessChannel": "unified_feed",
                    "deviceState": "disabled",
                    "identityBound": True,
                }
            ],
        )

    client = MediatorClient(
        base_url="http://mediator.test",
        admin_token="test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.list_device_tokens("00000000-0000-0000-0000-000000000001")
    finally:
        await client.close()

    assert result[0].access_channel == "unified_feed"
    assert result[0].device_state == "disabled"
    assert result[0].state == "revoked"


@pytest.mark.asyncio
async def test_enable_unified_device_uses_owner_scoped_endpoint() -> None:
    seen_request: httpx.Request | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = request
        return httpx.Response(200, request=request, json={"status": "enabled"})

    client = MediatorClient(
        base_url="http://mediator.test",
        admin_token="test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.enable_unified_device(
            "00000000-0000-0000-0000-000000000001",
            "device-3",
        )
    finally:
        await client.close()

    assert seen_request is not None
    assert seen_request.method == "POST"
    assert seen_request.url.path.endswith("/devices/device-3/enable")


@pytest.mark.asyncio
async def test_get_entitlement_rejects_invalid_payload_shape_and_timestamp() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "publicGuid": "00000000-0000-0000-0000-000000000001",
                "version": 1,
                "status": "active",
                "validUntilUtc": "not-a-timestamp",
                "maxDeviceTokens": 3,
                "updatedAtUtc": "2026-06-14T12:00:00+00:00",
            },
        )

    client = MediatorClient(
        base_url="http://mediator.test",
        admin_token="test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(MediatorClientError) as caught:
            await client.get_entitlement("00000000-0000-0000-0000-000000000001")
    finally:
        await client.close()

    assert caught.value.error_code == "invalid_response"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expectedMigrations", "broken"),
        ("migrationsCurrent", "true"),
        ("serverCount", True),
        ("capacityUtilizationPercent", "12.5"),
    ],
)
async def test_readiness_rejects_wrong_field_types(field: str, value: object) -> None:
    payload: dict[str, object] = {
        "status": "ready",
        "catalogState": "fresh",
        "serverCount": 3,
        "migrationsApplied": 32,
        "expectedMigrations": 32,
        "migrationsCurrent": True,
        "deviceIssuanceVersion": 2,
        "unifiedSubscriptionFeedEnabled": True,
        "sharedSubscriptionLinksOnly": True,
    }
    payload[field] = value

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json=payload)

    client = MediatorClient(
        base_url="http://mediator.test",
        admin_token="test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(MediatorClientError) as caught:
            await client.get_readiness()
    finally:
        await client.close()

    assert caught.value.error_code == "invalid_response"
