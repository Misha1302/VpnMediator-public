# Device identity

Unified feed identity is the Happ `x-hwid` value protected by HMAC-SHA-256 with a dedicated key.
Raw HWID is never persisted or logged. User-Agent, IP, model and display labels are not identity.

The first valid HWID atomically reserves a slot. The same HWID reuses its row across networks;
concurrent different HWIDs cannot both exceed the entitlement. A disabled HWID cannot silently
register again.

Required production settings:

```text
VpnMediator__DeviceIdentityHashKeyId=v1
VpnMediator__DeviceIdentityHashKey=<dedicated random secret>
```

For rotation, configure the former ID/key as the previous pair. A successful previous-key match is
rehash-migrated to the current key. Remove the previous key only after the observation window.

Legacy per-device URLs retain their historical versioned binding data during the compatibility
window. Global `DeviceFeedBindingMode` settings apply to that legacy path; unified feed HWID
enforcement is always active.

This boundary limits feed registrations. It cannot revoke a raw upstream credential already
downloaded from the shared catalog.

