using System.Net;
using System.Security.Cryptography;
using System.Text;

public static class DeviceFeedBindingModes
{
    public const string Off = "off";
    public const string Observe = "observe";
    public const string Enforce = "enforce";

    public static bool IsSupported(string? value)
    {
        return value is Off or Observe or Enforce;
    }
}

public static class DeviceFeedPolicyModes
{
    public const string Legacy = "legacy";
    public const string Observe = "observe";
    public const string Enforce = "enforce";

    public static bool IsSupported(string? value)
    {
        return value is Legacy or Observe or Enforce;
    }
}

public static class DeviceFeedPolicyVersions
{
    public const int PlatformHeuristic = 1;
    public const int HwidIdentity = 2;
    public const int Current = HwidIdentity;

    public static bool IsSupported(int value)
    {
        return value is PlatformHeuristic or HwidIdentity;
    }
}

public static class DeviceSecurityPostures
{
    public const string None = "none";
    public const string Observe = "observe";
    public const string FeedEnforced = "feed_enforced";
    public static bool IsSupported(string? value)
    {
        return value is None or Observe or FeedEnforced;
    }
}

public static class DeviceSecurityPostureEvaluator
{
    public static string GetEffective(VpnMediatorOptions options)
    {
        bool identityConfigured = !string.IsNullOrWhiteSpace(options.DeviceIdentityHashKey)
            && !string.IsNullOrWhiteSpace(options.DeviceIdentityHashKeyId);
        bool versionTwo = options.DefaultNewDeviceFeedPolicyVersion
            == DeviceFeedPolicyVersions.HwidIdentity;
        bool nonLegacy = !string.Equals(
            options.DefaultNewDeviceFeedPolicy,
            DeviceFeedPolicyModes.Legacy,
            StringComparison.Ordinal);
        bool observing = !string.Equals(
            options.DeviceFeedBindingMode,
            DeviceFeedBindingModes.Off,
            StringComparison.Ordinal)
            && versionTwo
            && nonLegacy
            && identityConfigured;
        bool feedEnforced = string.Equals(
                options.DeviceFeedBindingMode,
                DeviceFeedBindingModes.Enforce,
                StringComparison.Ordinal)
            && string.Equals(
                options.DefaultNewDeviceFeedPolicy,
                DeviceFeedPolicyModes.Enforce,
                StringComparison.Ordinal)
            && versionTwo
            && identityConfigured
            && options.RequireDeviceIssuanceKey;
        if (feedEnforced)
        {
            return DeviceSecurityPostures.FeedEnforced;
        }

        return observing ? DeviceSecurityPostures.Observe : DeviceSecurityPostures.None;
    }

    public static bool MeetsRequired(string effective, string required)
    {
        return Rank(effective) >= Rank(required);
    }

    private static int Rank(string value)
    {
        return value switch
        {
            DeviceSecurityPostures.None => 0,
            DeviceSecurityPostures.Observe => 1,
            DeviceSecurityPostures.FeedEnforced => 2,
            _ => -1
        };
    }
}

public static class DeviceFeedBindingStates
{
    public const string Grandfathered = "grandfathered";
    public const string Unbound = "unbound";
    public const string Bound = "bound";
    public const string Review = "review";
}

public static class DeviceFeedPolicyDecisions
{
    public const string AllowGlobalOff = "allow_global_off";
    public const string AllowLegacy = "allow_legacy";
    public const string AllowObserve = "allow_observe";
    public const string AllowSameBinding = "allow_same_binding";
    public const string AllowUnknownClient = "allow_unknown_client";
    public const string AllowIdentityMatch = "allow_identity_match";
    public const string BindAndAllow = "bind_and_allow";
    public const string BindIdentityAndAllow = "bind_identity_and_allow";
    public const string RequireIdentity = "require_identity";
    public const string RequireTransfer = "require_transfer";
    public const string RejectUnsupportedPolicy = "reject_unsupported_policy";
}

public static class DeviceIdentitySources
{
    public const string HappHwid = "happ_hwid";
}

public sealed record DeviceIdentityEvidence(
    bool IsPresent,
    bool IsValid,
    string? CurrentHash,
    string? CurrentKeyId,
    string? PreviousHash,
    string? PreviousKeyId,
    string? Source,
    string? ValidationError)
{
    public bool Matches(string? boundHash)
    {
        if (string.IsNullOrWhiteSpace(boundHash))
        {
            return false;
        }

        return FixedTimeEquals(CurrentHash, boundHash)
            || FixedTimeEquals(PreviousHash, boundHash);
    }

    public bool MatchesCurrent(string? boundHash)
    {
        return FixedTimeEquals(CurrentHash, boundHash);
    }

    private static bool FixedTimeEquals(string? left, string? right)
    {
        if (left is null || right is null)
        {
            return false;
        }

        byte[] leftBytes = Encoding.UTF8.GetBytes(left);
        byte[] rightBytes = Encoding.UTF8.GetBytes(right);
        return leftBytes.Length == rightBytes.Length
            && CryptographicOperations.FixedTimeEquals(leftBytes, rightBytes);
    }
}

public sealed record DeviceAccessRequestContext(
    DeviceMetadata Metadata,
    DeviceIdentityEvidence Identity,
    string? ClientFamily,
    string? NetworkFingerprint,
    string? OsVersion)
{
    public bool HasReliablePlatform => !string.IsNullOrWhiteSpace(Metadata.Platform);
}

public sealed record DeviceFeedPolicyState(
    int PolicyVersion,
    string PolicyMode,
    string BindingState,
    string? RequestedPlatform,
    string? BoundPlatform,
    string? BoundClientFamily,
    string? BoundIdentityHash,
    string? BoundIdentityKeyId,
    string? BoundIdentitySource);

public sealed record DeviceFeedPolicyDecision(
    bool Allowed,
    string Decision,
    string? ReasonCode,
    bool ShouldBind,
    bool ShouldRefreshIdentityHash,
    bool ShouldRecordObservation);

public static class DeviceFeedPolicyEvaluator
{
    public static DeviceFeedPolicyDecision Evaluate(
        string globalMode,
        DeviceFeedPolicyState state,
        DeviceAccessRequestContext context)
    {
        if (string.Equals(globalMode, DeviceFeedBindingModes.Off, StringComparison.Ordinal))
        {
            return Decision(
                allowed: true,
                DeviceFeedPolicyDecisions.AllowGlobalOff,
                shouldRecordObservation: false);
        }

        if (string.Equals(state.PolicyMode, DeviceFeedPolicyModes.Legacy, StringComparison.Ordinal))
        {
            return Decision(
                allowed: true,
                DeviceFeedPolicyDecisions.AllowLegacy,
                shouldRecordObservation: true);
        }

        bool observing = string.Equals(globalMode, DeviceFeedBindingModes.Observe, StringComparison.Ordinal)
            || string.Equals(state.PolicyMode, DeviceFeedPolicyModes.Observe, StringComparison.Ordinal);

        return state.PolicyVersion switch
        {
            DeviceFeedPolicyVersions.PlatformHeuristic => EvaluatePlatformHeuristic(
                state,
                context,
                observing),
            DeviceFeedPolicyVersions.HwidIdentity => EvaluateHwidIdentity(
                state,
                context,
                observing),
            _ => observing
                ? Decision(
                    allowed: true,
                    DeviceFeedPolicyDecisions.AllowObserve,
                    reasonCode: "unsupported_feed_policy_version",
                    shouldRecordObservation: true)
                : Decision(
                    allowed: false,
                    DeviceFeedPolicyDecisions.RejectUnsupportedPolicy,
                    reasonCode: "unsupported_feed_policy_version",
                    shouldRecordObservation: true)
        };
    }

    private static DeviceFeedPolicyDecision EvaluatePlatformHeuristic(
        DeviceFeedPolicyState state,
        DeviceAccessRequestContext context,
        bool observing)
    {
        if (!context.HasReliablePlatform)
        {
            return Decision(
                allowed: true,
                DeviceFeedPolicyDecisions.AllowUnknownClient,
                shouldRecordObservation: true);
        }

        string observedPlatform = context.Metadata.Platform!;
        string? expectedPlatform = state.BoundPlatform ?? state.RequestedPlatform;
        bool hasBinding = IsBound(state) && !string.IsNullOrWhiteSpace(state.BoundPlatform);

        if (!hasBinding)
        {
            if (state.RequestedPlatform is not null
                && !string.Equals(
                    state.RequestedPlatform,
                    observedPlatform,
                    StringComparison.Ordinal))
            {
                return MismatchDecision(observing, "requested_platform_mismatch");
            }

            return Decision(
                allowed: true,
                observing
                    ? DeviceFeedPolicyDecisions.AllowObserve
                    : DeviceFeedPolicyDecisions.BindAndAllow,
                shouldBind: true,
                shouldRecordObservation: true);
        }

        if (!string.Equals(expectedPlatform, observedPlatform, StringComparison.Ordinal))
        {
            return MismatchDecision(observing, "platform_mismatch");
        }

        return Decision(
            allowed: true,
            observing
                ? DeviceFeedPolicyDecisions.AllowObserve
                : DeviceFeedPolicyDecisions.AllowSameBinding,
            shouldRecordObservation: true);
    }

    private static DeviceFeedPolicyDecision EvaluateHwidIdentity(
        DeviceFeedPolicyState state,
        DeviceAccessRequestContext context,
        bool observing)
    {
        DeviceIdentityEvidence identity = context.Identity;
        if (!identity.IsPresent)
        {
            return IdentityUnavailableDecision(observing, "identity_missing");
        }

        if (!identity.IsValid || identity.CurrentHash is null)
        {
            return IdentityUnavailableDecision(
                observing,
                identity.ValidationError ?? "identity_invalid");
        }

        string? expectedPlatform = state.BoundPlatform ?? state.RequestedPlatform;
        bool hasIdentityBinding = !string.IsNullOrWhiteSpace(state.BoundIdentityHash);

        if (hasIdentityBinding)
        {
            if (!identity.Matches(state.BoundIdentityHash))
            {
                return MismatchDecision(observing, "identity_mismatch");
            }

            if (context.HasReliablePlatform
                && expectedPlatform is not null
                && !string.Equals(
                    expectedPlatform,
                    context.Metadata.Platform,
                    StringComparison.Ordinal))
            {
                return MismatchDecision(observing, "identity_platform_conflict");
            }

            return Decision(
                allowed: true,
                observing
                    ? DeviceFeedPolicyDecisions.AllowObserve
                    : DeviceFeedPolicyDecisions.AllowIdentityMatch,
                shouldRefreshIdentityHash: !identity.MatchesCurrent(state.BoundIdentityHash),
                shouldRecordObservation: true);
        }

        if (context.HasReliablePlatform
            && state.RequestedPlatform is not null
            && !string.Equals(
                state.RequestedPlatform,
                context.Metadata.Platform,
                StringComparison.Ordinal))
        {
            return MismatchDecision(observing, "requested_platform_mismatch");
        }

        return Decision(
            allowed: true,
            observing
                ? DeviceFeedPolicyDecisions.AllowObserve
                : DeviceFeedPolicyDecisions.BindIdentityAndAllow,
            shouldBind: true,
            shouldRecordObservation: true);
    }

    private static DeviceFeedPolicyDecision IdentityUnavailableDecision(
        bool observing,
        string reasonCode)
    {
        return observing
            ? Decision(
                allowed: true,
                DeviceFeedPolicyDecisions.AllowObserve,
                reasonCode,
                shouldRecordObservation: true)
            : Decision(
                allowed: false,
                DeviceFeedPolicyDecisions.RequireIdentity,
                reasonCode,
                shouldRecordObservation: true);
    }

    private static DeviceFeedPolicyDecision MismatchDecision(
        bool observing,
        string reasonCode)
    {
        return observing
            ? Decision(
                allowed: true,
                DeviceFeedPolicyDecisions.AllowObserve,
                reasonCode,
                shouldRecordObservation: true)
            : Decision(
                allowed: false,
                DeviceFeedPolicyDecisions.RequireTransfer,
                reasonCode,
                shouldRecordObservation: true);
    }

    private static bool IsBound(DeviceFeedPolicyState state)
    {
        return string.Equals(
                state.BindingState,
                DeviceFeedBindingStates.Bound,
                StringComparison.Ordinal)
            || string.Equals(
                state.BindingState,
                DeviceFeedBindingStates.Review,
                StringComparison.Ordinal);
    }

    private static DeviceFeedPolicyDecision Decision(
        bool allowed,
        string decision,
        string? reasonCode = null,
        bool shouldBind = false,
        bool shouldRefreshIdentityHash = false,
        bool shouldRecordObservation = false)
    {
        return new DeviceFeedPolicyDecision(
            allowed,
            decision,
            reasonCode,
            shouldBind,
            shouldRefreshIdentityHash,
            shouldRecordObservation);
    }
}

public static class DeviceAccessRequestContextFactory
{
    private const int MaxHwidLength = 128;
    private const int MaxDeviceOsLength = 32;
    private const int MaxOsVersionLength = 64;
    private const int MaxDeviceModelLength = 128;

    public static DeviceAccessRequestContext Create(
        string? userAgent,
        IPAddress? remoteIpAddress,
        string? observationHashKey,
        string? hwid = null,
        string? deviceOs = null,
        string? osVersion = null,
        string? deviceModel = null,
        string? identityHashKeyId = null,
        string? identityHashKey = null,
        string? previousIdentityHashKeyId = null,
        string? previousIdentityHashKey = null)
    {
        DeviceMetadata metadata = CreateMetadata(userAgent, deviceOs, deviceModel);
        DeviceIdentityEvidence identity = CreateIdentityEvidence(
            hwid,
            identityHashKeyId,
            identityHashKey,
            previousIdentityHashKeyId,
            previousIdentityHashKey);
        string? clientFamily = DetectClientFamily(userAgent);
        string? networkFingerprint = CreateNetworkFingerprint(
            remoteIpAddress,
            observationHashKey);
        string? normalizedOsVersion = NormalizePrintable(osVersion, MaxOsVersionLength);
        return new DeviceAccessRequestContext(
            metadata,
            identity,
            clientFamily,
            networkFingerprint,
            normalizedOsVersion);
    }

    private static DeviceMetadata CreateMetadata(
        string? userAgent,
        string? deviceOs,
        string? deviceModel)
    {
        DeviceMetadata fallback = DeviceMetadataDetector.Detect(userAgent);
        string? platform = NormalizePlatform(deviceOs);
        if (platform is null)
        {
            return fallback;
        }

        string? normalizedModel = NormalizePrintable(deviceModel, MaxDeviceModelLength);
        return new DeviceMetadata(
            DeviceTypeForPlatform(platform),
            platform,
            normalizedModel ?? fallback.DetectedModel,
            "happ_headers");
    }

    private static DeviceIdentityEvidence CreateIdentityEvidence(
        string? hwid,
        string? identityHashKeyId,
        string? identityHashKey,
        string? previousIdentityHashKeyId,
        string? previousIdentityHashKey)
    {
        if (string.IsNullOrWhiteSpace(hwid))
        {
            return new DeviceIdentityEvidence(
                false,
                false,
                null,
                null,
                null,
                null,
                null,
                "identity_missing");
        }

        string normalized = hwid.Trim();
        if (normalized.Length is < 1 or > MaxHwidLength
            || normalized.Any(character => character is < '!' or > '~'))
        {
            return new DeviceIdentityEvidence(
                true,
                false,
                null,
                null,
                null,
                null,
                DeviceIdentitySources.HappHwid,
                "identity_invalid");
        }

        if (Guid.TryParse(normalized, out Guid guid))
        {
            normalized = guid.ToString("D");
        }

        if (string.IsNullOrWhiteSpace(identityHashKey)
            || string.IsNullOrWhiteSpace(identityHashKeyId))
        {
            return new DeviceIdentityEvidence(
                true,
                false,
                null,
                null,
                null,
                null,
                DeviceIdentitySources.HappHwid,
                "identity_hash_unavailable");
        }

        string currentHash = DeviceIdentityHasher.Hash(
            normalized,
            DeviceIdentitySources.HappHwid,
            identityHashKey);
        string? previousHash = string.IsNullOrWhiteSpace(previousIdentityHashKey)
            || string.IsNullOrWhiteSpace(previousIdentityHashKeyId)
            ? null
            : DeviceIdentityHasher.Hash(
                normalized,
                DeviceIdentitySources.HappHwid,
                previousIdentityHashKey);

        return new DeviceIdentityEvidence(
            true,
            true,
            currentHash,
            identityHashKeyId.Trim(),
            previousHash,
            previousIdentityHashKeyId?.Trim(),
            DeviceIdentitySources.HappHwid,
            null);
    }

    private static string? NormalizePlatform(string? value)
    {
        string? normalized = NormalizePrintable(value, MaxDeviceOsLength)?.ToLowerInvariant();
        return normalized switch
        {
            "android" => "android",
            "ios" or "iphone" or "ipad" => "ios",
            "windows" or "win32" or "win64" => "windows",
            "macos" or "mac os" or "darwin" => "macos",
            "linux" => "linux",
            _ => null
        };
    }

    private static string? DeviceTypeForPlatform(string platform)
    {
        return platform switch
        {
            "android" or "ios" => "phone",
            "windows" or "macos" or "linux" => "computer",
            _ => null
        };
    }

    private static string? NormalizePrintable(string? value, int maxLength)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return null;
        }

        string normalized = value.Trim();
        if (normalized.Length > maxLength)
        {
            normalized = normalized[..maxLength];
        }

        return normalized.Any(char.IsControl)
            ? null
            : normalized;
    }

    private static string? DetectClientFamily(string? userAgent)
    {
        if (string.IsNullOrWhiteSpace(userAgent))
        {
            return null;
        }

        string normalized = userAgent.Trim().ToLowerInvariant();
        return normalized.Contains("happ", StringComparison.Ordinal)
            ? "happ"
            : null;
    }

    private static string? CreateNetworkFingerprint(
        IPAddress? remoteIpAddress,
        string? observationHashKey)
    {
        if (remoteIpAddress is null || string.IsNullOrWhiteSpace(observationHashKey))
        {
            return null;
        }

        IPAddress normalizedAddress = remoteIpAddress.IsIPv4MappedToIPv6
            ? remoteIpAddress.MapToIPv4()
            : remoteIpAddress;
        byte[] bytes = normalizedAddress.GetAddressBytes();
        int prefixLength;

        if (normalizedAddress.AddressFamily == System.Net.Sockets.AddressFamily.InterNetwork)
        {
            bytes[3] = 0;
            prefixLength = 24;
        }
        else if (normalizedAddress.AddressFamily == System.Net.Sockets.AddressFamily.InterNetworkV6)
        {
            for (int index = 6; index < bytes.Length; index++)
            {
                bytes[index] = 0;
            }

            prefixLength = 48;
        }
        else
        {
            return null;
        }

        string normalizedNetwork = $"{new IPAddress(bytes)}/{prefixLength}";
        using HMACSHA256 hmac = new(Encoding.UTF8.GetBytes(observationHashKey));
        return Convert.ToHexString(
            hmac.ComputeHash(Encoding.UTF8.GetBytes(normalizedNetwork)))
            .ToLowerInvariant();
    }
}

public static class DeviceIdentityHasher
{
    public static string Hash(string normalizedIdentity, string source, string key)
    {
        using HMACSHA256 hmac = new(Encoding.UTF8.GetBytes(key));
        byte[] payload = Encoding.UTF8.GetBytes($"{source}\0{normalizedIdentity}");
        return Convert.ToHexString(hmac.ComputeHash(payload)).ToLowerInvariant();
    }
}

public static class DeviceTransferOperationIdValidator
{
    public static bool TryNormalize(string? value, out string? normalized)
    {
        normalized = string.IsNullOrWhiteSpace(value) ? null : value.Trim();
        if (normalized is null
            || normalized.Length is < 24 or > 160
            || !normalized.StartsWith("device-transfer:", StringComparison.Ordinal))
        {
            normalized = null;
            return false;
        }

        if (!normalized.All(character =>
            char.IsAsciiLetterOrDigit(character) || character is '-' or '_' or ':' or '.'))
        {
            normalized = null;
            return false;
        }

        return true;
    }
}
