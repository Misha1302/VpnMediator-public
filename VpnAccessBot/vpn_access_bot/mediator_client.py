from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from vpn_access_bot.correlation import get_correlation_id


@dataclass(frozen=True)
class MediatorReadiness:
    status: str
    catalog_state: str
    server_count: int
    migrations_applied: int
    migrations_current: bool
    device_issuance_version: int = 1
    device_feed_policy_version: int = 0
    device_feed_binding_mode: str = "off"
    default_new_device_feed_policy: str = "legacy"
    require_device_issuance_key: bool = False
    device_security_posture: str = "none"
    required_device_security_posture: str = "none"
    identity_hash_configured: bool = False
    unified_subscription_feed_enabled: bool = False
    shared_subscription_links_only: bool = False
    reason: str | None = None
    active_subscriptions: int | None = None
    active_devices: int | None = None
    configured_subscription_capacity: int | None = None
    configured_device_capacity: int | None = None
    capacity_utilization_percent: float | None = None
    capacity_state: str = "unknown"
    snapshot_captured_at_utc: str | None = None


@dataclass(frozen=True)
class EntitlementPayload:
    version: int
    status: str
    valid_until_utc: str
    max_device_tokens: int


@dataclass(frozen=True)
class CreateMediatedSubscriptionResult:
    public_guid: str
    already_existed: bool


@dataclass(frozen=True)
class EntitlementApplyResult:
    status: str
    current_version: int | None


@dataclass(frozen=True)
class MediatorEntitlementOperationResult:
    status: str
    operation_id: str
    public_guid: str
    operation_type: str
    expected_version: int
    result_version: int
    result_status: str
    result_valid_until_utc: str | None
    result_max_device_tokens: int
    applied_at_utc: str


@dataclass(frozen=True)
class SubscriptionFeedCredential:
    status: str
    connection_url: str
    created: bool


@dataclass(frozen=True)
class DeviceTokenListItem:
    public_id: str
    display_name: str
    state: str
    pending_expires_at_utc: str | None
    activated_at_utc: str | None
    last_used_at_utc: str | None
    revoked_at_utc: str | None
    revocation_reason: str | None
    first_fetched_at_utc: str | None = None
    device_type: str | None = None
    platform: str | None = None
    detected_model: str | None = None
    detection_source: str | None = None
    feed_policy_version: int = 1
    feed_policy_mode: str = "legacy"
    binding_state: str = "grandfathered"
    bound_platform: str | None = None
    bound_client_family: str | None = None
    bound_at_utc: str | None = None
    identity_bound: bool = False
    identity_source: str | None = None
    last_identity_seen_at_utc: str | None = None
    last_identity_mismatch_at_utc: str | None = None
    last_transfer_at_utc: str | None = None
    transfer_count: int = 0
    risk_score: int = 0
    access_channel: str = "device_link"
    device_state: str | None = None


@dataclass(frozen=True)
class MediatorSubscriptionDetails:
    public_guid: str
    subscription_url: str | None
    max_devices: int
    is_active: bool
    active_device_count: int
    customer_name: str | None
    note: str | None
    devices: list[dict]


@dataclass(frozen=True)
class MediatorEntitlementDetails:
    public_guid: str
    version: int
    status: str
    valid_until_utc: str | None
    max_device_tokens: int
    updated_at_utc: str


class MediatorClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        payload: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.payload = payload


class MediatorClient:
    def __init__(
        self,
        base_url: str,
        admin_token: str,
        public_subscription_base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._admin_token = admin_token
        self._public_subscription_base_url = (
            public_subscription_base_url.rstrip("/") if public_subscription_base_url else None
        )
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get_readiness(self) -> MediatorReadiness:
        response = await self._request(
            "GET",
            f"{self._base_url}/internal/health/ready",
            headers=self._headers(),
        )
        try:
            data = response.json()
        except ValueError:
            data = self._json_or_raise(response)

        if not isinstance(data, dict):
            raise MediatorClientError(
                "Mediator returned an invalid readiness response.",
                status_code=response.status_code,
                error_code="invalid_response",
            )

        if not response.is_success and not (response.status_code == 503 and "status" in data):
            self._json_or_raise(response)

        def invalid(field: str) -> MediatorClientError:
            return MediatorClientError(
                f"Mediator readiness field {field!r} has an invalid type.",
                status_code=response.status_code,
                error_code="invalid_response",
            )

        def read_int(field: str, default: int = 0) -> int:
            value = data.get(field, default)
            if isinstance(value, bool) or not isinstance(value, int):
                raise invalid(field)
            return value

        def read_optional_int(field: str) -> int | None:
            value = data.get(field)
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, int):
                raise invalid(field)
            return value

        def read_float(field: str) -> float | None:
            value = data.get(field)
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise invalid(field)
            return float(value)

        def read_bool(field: str, default: bool = False) -> bool:
            value = data.get(field, default)
            if not isinstance(value, bool):
                raise invalid(field)
            return value

        def read_str(field: str, default: str) -> str:
            value = data.get(field, default)
            if not isinstance(value, str):
                raise invalid(field)
            return value

        def read_optional_str(field: str) -> str | None:
            value = data.get(field)
            if value is None:
                return None
            if not isinstance(value, str):
                raise invalid(field)
            return value

        migrations_applied = read_int("migrationsApplied")
        expected = read_optional_int("expectedMigrations")
        if "migrationsCurrent" in data:
            migrations_current = read_bool("migrationsCurrent")
        else:
            migrations_current = expected is None or migrations_applied >= expected
        security_posture_field = (
            "deviceSecurityPosture"
            if "deviceSecurityPosture" in data
            else "effectiveDeviceSecurityPosture"
        )
        identity_hash_field = (
            "identityHashConfigured"
            if "identityHashConfigured" in data
            else "deviceIdentityHashConfigured"
        )

        return MediatorReadiness(
            status=read_str("status", "unavailable"),
            catalog_state=read_str("catalogState", "unavailable"),
            server_count=read_int("serverCount"),
            migrations_applied=migrations_applied,
            migrations_current=migrations_current,
            device_issuance_version=read_int("deviceIssuanceVersion", 1),
            device_feed_policy_version=read_int("deviceFeedPolicyVersion"),
            device_feed_binding_mode=read_str("deviceFeedBindingMode", "off"),
            default_new_device_feed_policy=read_str("defaultNewDeviceFeedPolicy", "legacy"),
            require_device_issuance_key=read_bool("requireDeviceIssuanceKey"),
            device_security_posture=read_str(security_posture_field, "none"),
            required_device_security_posture=read_str("requiredDeviceSecurityPosture", "none"),
            identity_hash_configured=read_bool(identity_hash_field),
            unified_subscription_feed_enabled=read_bool("unifiedSubscriptionFeedEnabled"),
            shared_subscription_links_only=read_bool("sharedSubscriptionLinksOnly"),
            reason=read_optional_str("reason"),
            active_subscriptions=read_optional_int("activeSubscriptions"),
            active_devices=read_optional_int("activeDevices"),
            configured_subscription_capacity=read_optional_int("configuredSubscriptionCapacity"),
            configured_device_capacity=read_optional_int("configuredDeviceCapacity"),
            capacity_utilization_percent=read_float("capacityUtilizationPercent"),
            capacity_state=read_str("capacityState", "unknown"),
            snapshot_captured_at_utc=read_optional_str("snapshotCapturedAtUtc"),
        )

    async def create_subscription(
        self,
        external_request_id: str,
        customer_reference: str,
        note: str,
        entitlement: EntitlementPayload,
    ) -> CreateMediatedSubscriptionResult:
        response = await self._request(
            "POST",
            f"{self._base_url}/admin/subscriptions",
            headers=self._headers(),
            json={
                "externalRequestId": external_request_id,
                "customerReference": customer_reference,
                "note": note,
                "entitlement": {
                    "version": entitlement.version,
                    "status": entitlement.status,
                    "validUntilUtc": entitlement.valid_until_utc,
                    "maxDeviceTokens": entitlement.max_device_tokens,
                },
            },
        )
        data = self._json_or_raise(response)

        return CreateMediatedSubscriptionResult(
            public_guid=str(data["publicGuid"]),
            already_existed=bool(data.get("alreadyExisted", False)),
        )

    async def update_entitlement(
        self,
        public_guid: str,
        entitlement: EntitlementPayload,
    ) -> EntitlementApplyResult:
        response = await self._request(
            "PUT",
            f"{self._base_url}/admin/subscriptions/{public_guid}/entitlement",
            headers=self._headers(),
            json={
                "version": entitlement.version,
                "status": entitlement.status,
                "validUntilUtc": entitlement.valid_until_utc,
                "maxDeviceTokens": entitlement.max_device_tokens,
            },
        )
        data = self._json_or_raise(response)
        return EntitlementApplyResult(
            status=str(data["status"]),
            current_version=(
                int(data["currentVersion"]) if data.get("currentVersion") is not None else None
            ),
        )

    async def apply_entitlement_operation(
        self,
        public_guid: str,
        *,
        operation_id: str,
        operation_type: str,
        expected_version: int,
        status: str,
        valid_until_utc: str,
        max_device_tokens: int,
    ) -> MediatorEntitlementOperationResult:
        response = await self._request(
            "POST",
            f"{self._base_url}/admin/subscriptions/{public_guid}/entitlement-operations",
            headers=self._headers(),
            json={
                "operationId": operation_id,
                "operationType": operation_type,
                "expectedVersion": expected_version,
                "status": status,
                "validUntilUtc": valid_until_utc,
                "maxDeviceTokens": max_device_tokens,
            },
        )
        data = self._json_or_raise(response)
        return self._operation_result_from_json(data)

    async def get_entitlement_operation(
        self, operation_id: str
    ) -> MediatorEntitlementOperationResult | None:
        response = await self._request(
            "GET",
            f"{self._base_url}/admin/entitlement-operations/{operation_id}",
            headers=self._headers(),
        )
        if response.status_code == 404:
            return None
        data = self._json_or_raise(response)
        return self._operation_result_from_json(data)

    async def get_entitlement_operation_by_result_version(
        self,
        public_guid: str,
        result_version: int,
    ) -> MediatorEntitlementOperationResult | None:
        response = await self._request(
            "GET",
            (
                f"{self._base_url}/admin/subscriptions/{public_guid}/"
                f"entitlement-operations/by-result-version/{result_version}"
            ),
            headers=self._headers(),
        )
        if response.status_code == 404:
            return None
        data = self._json_or_raise(response)
        return self._operation_result_from_json(data)

    async def get_entitlement(self, public_guid: str) -> MediatorEntitlementDetails:
        response = await self._request(
            "GET",
            f"{self._base_url}/admin/subscriptions/{public_guid}/entitlement",
            headers=self._headers(),
        )
        data = self._json_or_raise(response)
        return self._entitlement_details_from_json(data)

    async def get_subscription(self, public_guid: str) -> MediatorSubscriptionDetails:
        response = await self._request(
            "GET",
            f"{self._base_url}/admin/subscriptions/{public_guid}",
            headers=self._headers(),
        )
        data = self._json_or_raise(response)

        return self._subscription_details_from_json(data)

    async def list_subscriptions(self) -> list[MediatorSubscriptionDetails]:
        response = await self._request(
            "GET",
            f"{self._base_url}/admin/subscriptions",
            headers=self._headers(),
        )
        data = self._json_or_raise(response)

        return [self._subscription_details_from_json(item) for item in data]

    async def find_subscription_by_note(self, note: str) -> MediatorSubscriptionDetails | None:
        subscriptions = await self.list_subscriptions()

        for subscription in subscriptions:
            if subscription.note == note:
                return await self.get_subscription(subscription.public_guid)

        return None

    async def reset_devices(self, public_guid: str) -> int:
        response = await self._request(
            "DELETE",
            f"{self._base_url}/admin/subscriptions/{public_guid}/devices",
            headers=self._headers(),
        )
        data = self._json_or_raise(response)
        return int(data.get("unboundDevices", 0))

    async def ensure_subscription_feed(
        self,
        public_guid: str,
    ) -> SubscriptionFeedCredential:
        response = await self._request(
            "POST",
            f"{self._base_url}/admin/subscriptions/{public_guid}/feed-credential/ensure",
            headers=self._headers(),
        )
        data = self._json_or_raise(response)
        return SubscriptionFeedCredential(
            status=str(data.get("status", "existing")),
            connection_url=self._rewrite_public_base(str(data["connectionUrl"])),
            created=bool(data.get("created", False)),
        )

    async def list_device_tokens(self, public_guid: str) -> list[DeviceTokenListItem]:
        response = await self._request(
            "GET",
            f"{self._base_url}/admin/subscriptions/{public_guid}/device-tokens",
            headers=self._headers(),
        )
        data = self._json_or_raise(response)
        return [
            DeviceTokenListItem(
                public_id=str(item["publicId"]),
                display_name=str(item["displayName"]),
                state=str(item.get("state", "active")),
                pending_expires_at_utc=item.get("pendingExpiresAtUtc"),
                activated_at_utc=item.get("activatedAtUtc"),
                last_used_at_utc=item.get("lastUsedAtUtc"),
                first_fetched_at_utc=item.get("firstFetchedAtUtc"),
                revoked_at_utc=item.get("revokedAtUtc"),
                revocation_reason=item.get("revocationReason"),
                device_type=item.get("deviceType"),
                platform=item.get("platform"),
                detected_model=item.get("detectedModel"),
                detection_source=item.get("detectionSource"),
                feed_policy_version=int(item.get("feedPolicyVersion", 1)),
                feed_policy_mode=str(item.get("feedPolicyMode", "legacy")),
                binding_state=str(item.get("bindingState", "grandfathered")),
                bound_platform=item.get("boundPlatform"),
                bound_client_family=item.get("boundClientFamily"),
                bound_at_utc=item.get("boundAtUtc"),
                identity_bound=bool(item.get("identityBound", False)),
                identity_source=item.get("identitySource"),
                last_identity_seen_at_utc=item.get("lastIdentitySeenAtUtc"),
                last_identity_mismatch_at_utc=item.get("lastIdentityMismatchAtUtc"),
                last_transfer_at_utc=item.get("lastTransferAtUtc"),
                transfer_count=int(item.get("transferCount", 0)),
                risk_score=int(item.get("riskScore", 0)),
                access_channel=str(item.get("accessChannel", "device_link")),
                device_state=item.get("deviceState"),
            )
            for item in data
        ]

    async def revoke_device_token(self, public_guid: str, device_public_id: str) -> None:
        response = await self._request(
            "DELETE",
            f"{self._base_url}/admin/subscriptions/{public_guid}/device-tokens/{device_public_id}",
            headers=self._headers(),
        )
        self._json_or_raise(response)

    async def enable_unified_device(
        self,
        public_guid: str,
        device_public_id: str,
    ) -> None:
        response = await self._request(
            "POST",
            (
                f"{self._base_url}/admin/subscriptions/{public_guid}/"
                f"devices/{device_public_id}/enable"
            ),
            headers=self._headers(),
        )
        self._json_or_raise(response)

    async def revoke_all_device_tokens(self, public_guid: str) -> int:
        response = await self._request(
            "DELETE",
            f"{self._base_url}/admin/subscriptions/{public_guid}/device-tokens",
            headers=self._headers(),
        )
        data = self._json_or_raise(response)
        return int(data.get("revokedTokens", 0))

    async def set_limit(self, public_guid: str, max_devices: int) -> None:
        response = await self._request(
            "PATCH",
            f"{self._base_url}/admin/subscriptions/{public_guid}/limit",
            headers=self._headers(),
            json={"maxDevices": max_devices},
        )
        self._json_or_raise(response)

    async def enable_subscription(self, public_guid: str) -> None:
        response = await self._request(
            "POST",
            f"{self._base_url}/admin/subscriptions/{public_guid}/enable",
            headers=self._headers(),
        )
        self._json_or_raise(response)

    async def disable_subscription(self, public_guid: str) -> None:
        response = await self._request(
            "POST",
            f"{self._base_url}/admin/subscriptions/{public_guid}/disable",
            headers=self._headers(),
        )
        self._json_or_raise(response)

    async def _request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            return await self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exception:
            raise MediatorClientError(
                "Mediator is unavailable.",
                error_code="mediator_unavailable",
            ) from exception

    def _headers(self) -> dict[str, str]:
        headers = {"X-Admin-Token": self._admin_token}
        correlation_id = get_correlation_id()
        if correlation_id is not None:
            headers["X-Correlation-ID"] = correlation_id
        return headers

    def _json_or_raise(self, response: httpx.Response) -> Any:
        try:
            payload = response.json()
        except ValueError as exception:
            if response.is_success:
                raise MediatorClientError(
                    "Mediator returned an invalid JSON response.",
                    status_code=response.status_code,
                    error_code="invalid_response",
                ) from exception

            payload = None

        if response.is_success:
            return payload

        error_code = None

        if isinstance(payload, dict) and payload.get("errorCode") is not None:
            error_code = str(payload["errorCode"])
            if error_code == "credential_unavailable_regenerate_required":
                error_code = "credential_reissue_required"

        suffix = f" ({error_code})" if error_code else ""

        raise MediatorClientError(
            f"Mediator request failed: HTTP {response.status_code}{suffix}.",
            status_code=response.status_code,
            error_code=error_code,
            payload=payload,
        )

    @staticmethod
    def _entitlement_details_from_json(data: Any) -> MediatorEntitlementDetails:
        if not isinstance(data, dict):
            raise MediatorClientError(
                "Mediator returned an invalid entitlement response.",
                error_code="invalid_response",
            )

        required = (
            "publicGuid",
            "version",
            "status",
            "validUntilUtc",
            "maxDeviceTokens",
            "updatedAtUtc",
        )
        missing = [name for name in required if data.get(name) is None]
        if missing:
            raise MediatorClientError(
                "Mediator returned an invalid entitlement response.",
                error_code="invalid_response",
                payload={"missingFields": missing},
            )

        try:
            version = int(data["version"])
            max_device_tokens = int(data["maxDeviceTokens"])
            valid_until_utc = str(data["validUntilUtc"])
            updated_at_utc = str(data["updatedAtUtc"])
            parsed_valid_until = datetime.fromisoformat(valid_until_utc.replace("Z", "+00:00"))
            parsed_updated_at = datetime.fromisoformat(updated_at_utc.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exception:
            raise MediatorClientError(
                "Mediator returned an invalid entitlement response.",
                error_code="invalid_response",
            ) from exception

        status = str(data["status"])
        if (
            version < 1
            or max_device_tokens < 1
            or status not in {"active", "disabled", "expired"}
            or parsed_valid_until.tzinfo is None
            or parsed_updated_at.tzinfo is None
        ):
            raise MediatorClientError(
                "Mediator returned an invalid entitlement response.",
                error_code="invalid_response",
            )

        public_guid = str(data["publicGuid"])
        if not public_guid:
            raise MediatorClientError(
                "Mediator returned an invalid entitlement response.",
                error_code="invalid_response",
            )

        return MediatorEntitlementDetails(
            public_guid=public_guid,
            version=version,
            status=status,
            valid_until_utc=valid_until_utc,
            max_device_tokens=max_device_tokens,
            updated_at_utc=updated_at_utc,
        )

    @staticmethod
    def _operation_result_from_json(
        data: Any,
    ) -> MediatorEntitlementOperationResult:
        if not isinstance(data, dict):
            raise MediatorClientError(
                "Mediator returned an invalid entitlement operation response.",
                error_code="invalid_response",
            )

        required = (
            "status",
            "operationId",
            "publicGuid",
            "operationType",
            "expectedVersion",
            "resultVersion",
            "resultStatus",
            "resultMaxDeviceTokens",
            "appliedAtUtc",
        )
        missing = [name for name in required if data.get(name) is None]
        if missing:
            raise MediatorClientError(
                "Mediator returned an invalid entitlement operation response.",
                error_code="invalid_response",
                payload={"missingFields": missing},
            )

        try:
            expected_version = int(data["expectedVersion"])
            result_version = int(data["resultVersion"])
            result_max_device_tokens = int(data["resultMaxDeviceTokens"])
        except (TypeError, ValueError) as exception:
            raise MediatorClientError(
                "Mediator returned an invalid entitlement operation response.",
                error_code="invalid_response",
            ) from exception

        if expected_version < 1 or result_version < 1 or result_max_device_tokens < 1:
            raise MediatorClientError(
                "Mediator returned an invalid entitlement operation response.",
                error_code="invalid_response",
            )

        return MediatorEntitlementOperationResult(
            status=str(data["status"]),
            operation_id=str(data["operationId"]),
            public_guid=str(data["publicGuid"]),
            operation_type=str(data["operationType"]),
            expected_version=expected_version,
            result_version=result_version,
            result_status=str(data["resultStatus"]),
            result_valid_until_utc=(
                str(data["resultValidUntilUtc"])
                if data.get("resultValidUntilUtc") is not None
                else None
            ),
            result_max_device_tokens=result_max_device_tokens,
            applied_at_utc=str(data["appliedAtUtc"]),
        )

    def _subscription_details_from_json(self, data: dict[str, Any]) -> MediatorSubscriptionDetails:
        subscription_url = None

        if data.get("subscriptionUrl"):
            subscription_url = self._rewrite_public_base(str(data["subscriptionUrl"]))

        return MediatorSubscriptionDetails(
            public_guid=str(data["publicGuid"]),
            subscription_url=subscription_url,
            max_devices=int(data["maxDevices"]),
            is_active=bool(data["isActive"]),
            active_device_count=int(data.get("activeDeviceCount", 0)),
            customer_name=data.get("customerName"),
            note=data.get("note"),
            devices=list(data.get("devices", [])),
        )

    def _rewrite_public_base(self, url: str) -> str:
        if self._public_subscription_base_url is None:
            return url

        source = urlsplit(url)
        target = urlsplit(self._public_subscription_base_url)

        return urlunsplit(
            (
                target.scheme,
                target.netloc,
                source.path,
                source.query,
                source.fragment,
            )
        )
