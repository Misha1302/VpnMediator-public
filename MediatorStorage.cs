using System.Buffers;
using System.Diagnostics;
using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.Data.Sqlite;
using Microsoft.Extensions.Options;

public sealed partial class SqliteMediatorRepository : ISubscriptionRepository
{
    public const int CurrentMigrationVersion = 24;

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = false
    };

    private readonly VpnMediatorOptions _options;
    private readonly IEndpointProtector _endpointProtector;
    private readonly IDeviceCredentialProtector _deviceCredentialProtector;
    private readonly SemaphoreSlim _writeLock = new(1, 1);
    private readonly SemaphoreSlim _catalogRefreshLock = new(1, 1);

    public SqliteMediatorRepository(
        IOptions<VpnMediatorOptions> options,
        IEndpointProtector endpointProtector,
        IDeviceCredentialProtector deviceCredentialProtector)
    {
        _options = options.Value;
        _endpointProtector = endpointProtector;
        _deviceCredentialProtector = deviceCredentialProtector;
    }

    public async Task InitializeAsync(CancellationToken cancellationToken)
    {
        string? directory = Path.GetDirectoryName(_options.SqliteDatabasePath);

        if (!string.IsNullOrWhiteSpace(directory))
        {
            Directory.CreateDirectory(directory);
        }

        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await ConfigureDatabaseConcurrencyAsync(connection, cancellationToken);
            await EnsureDatabaseIsNotAheadAsync(connection, cancellationToken);
            await ApplyMigrationsAsync(connection, cancellationToken);
            await ImportLegacyJsonIfNeededAsync(connection, cancellationToken);
            MigrationState migrationState = await GetMigrationStateAsync(
                connection,
                cancellationToken);
            if (!migrationState.IsCurrent)
            {
                throw new InvalidOperationException(
                    $"Database migration history is incomplete. Missing required versions: "
                    + string.Join(",", migrationState.MissingRequiredVersions));
            }
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task CreateSubscriptionAsync(
        SubscriptionRecord subscription,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        CreateSubscriptionCommand command = new(
            ExternalRequestId: null,
            CustomerReference: subscription.CustomerName,
            Note: subscription.Note,
            PublicGuid: subscription.PublicGuid,
            Entitlement: new EntitlementUpdateRequest(
                Version: 1,
                Status: subscription.IsActive ? EntitlementStatuses.Active : EntitlementStatuses.Disabled,
                ValidUntilUtc: subscription.ExpiresAtUtc,
                MaxDeviceTokens: subscription.MaxDevices));

        await CreateMediatedSubscriptionAsync(command, now, cancellationToken);

        if (!string.IsNullOrWhiteSpace(subscription.UpstreamSubscriptionUrl))
        {
            await CreateSourceAsync(
                new CreateSourceRequest("legacy rent source", SourceKinds.SubscriptionUrl, subscription.UpstreamSubscriptionUrl),
                now,
                cancellationToken);
        }
    }

    public async Task<IReadOnlyList<SubscriptionRecord>> GetSubscriptionsAsync(
        CancellationToken cancellationToken)
    {
        await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
        List<SubscriptionRecord> subscriptions = [];

        const string sql = """
            SELECT s.public_guid, s.customer_reference, s.note, s.created_at_utc, s.updated_at_utc,
                   e.status, e.valid_until_utc, e.max_device_tokens
            FROM mediated_subscriptions s
            LEFT JOIN entitlement_mirrors e ON e.subscription_id = s.id
            ORDER BY s.created_at_utc DESC, s.id DESC;
            """;

        await using SqliteCommand command = new(sql, connection);
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);

        while (await reader.ReadAsync(cancellationToken))
        {
            subscriptions.Add(await ReadSubscriptionRecordAsync(connection, reader, cancellationToken));
        }

        return subscriptions;
    }

    public async Task<SubscriptionRecord?> GetSubscriptionAsync(
        Guid publicGuid,
        CancellationToken cancellationToken)
    {
        await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);

        const string sql = """
            SELECT s.public_guid, s.customer_reference, s.note, s.created_at_utc, s.updated_at_utc,
                   e.status, e.valid_until_utc, e.max_device_tokens
            FROM mediated_subscriptions s
            LEFT JOIN entitlement_mirrors e ON e.subscription_id = s.id
            WHERE s.public_guid = $public_guid;
            """;

        await using SqliteCommand command = new(sql, connection);
        command.Parameters.AddWithValue("$public_guid", publicGuid.ToString("D"));

        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);

        if (!await reader.ReadAsync(cancellationToken))
        {
            return null;
        }

        return await ReadSubscriptionRecordAsync(connection, reader, cancellationToken);
    }

    public async Task<DeviceRegistrationResult> RegisterDeviceAccessAsync(
        Guid publicGuid,
        DeviceIdentity deviceIdentity,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        _ = deviceIdentity;
        SubscriptionRuntimeState? state = await GetRuntimeStateAsync(publicGuid, cancellationToken);

        if (state is null)
        {
            return new DeviceRegistrationResult(DeviceRegistrationStatus.SubscriptionNotFound, null, 0, 0);
        }

        int activeCount = await CountActiveDeviceTokensAsync(publicGuid, cancellationToken);

        if (activeCount >= state.MaxDeviceTokens)
        {
            return new DeviceRegistrationResult(DeviceRegistrationStatus.DeviceLimitExceeded, null, activeCount, state.MaxDeviceTokens);
        }

        return new DeviceRegistrationResult(DeviceRegistrationStatus.AllowedExistingDevice, null, activeCount, state.MaxDeviceTokens);
    }

    public async Task<bool> UnbindDeviceAsync(
        Guid publicGuid,
        Guid deviceBindingId,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        return await RevokeDeviceTokenAsync(
            publicGuid,
            deviceBindingId.ToString("N"),
            now,
            cancellationToken);
    }

    public async Task<UnbindAllDevicesResult> UnbindAllDevicesAsync(
        Guid publicGuid,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        DeviceTokenRevokeAllResult result = await RevokeAllDeviceTokensAsync(publicGuid, now, cancellationToken);
        return new UnbindAllDevicesResult(result.SubscriptionFound, result.RevokedCount);
    }

    public async Task<bool> UpdateDeviceLimitAsync(
        Guid publicGuid,
        int maxDevices,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        SubscriptionRuntimeState? state = await GetRuntimeStateAsync(publicGuid, cancellationToken);

        if (state is null)
        {
            return false;
        }

        EntitlementUpdateRequest request = new(
            Version: state.Version + 1,
            Status: state.Status,
            ValidUntilUtc: state.ValidUntilUtc,
            MaxDeviceTokens: maxDevices);

        EntitlementUpdateResult result = await ApplyEntitlementAsync(
            publicGuid,
            request,
            now,
            cancellationToken);

        return result.Status is EntitlementUpdateStatus.Applied or EntitlementUpdateStatus.AlreadyApplied;
    }

    public async Task<bool> SetSubscriptionActiveAsync(
        Guid publicGuid,
        bool isActive,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        SubscriptionRuntimeState? state = await GetRuntimeStateAsync(publicGuid, cancellationToken);

        if (state is null)
        {
            return false;
        }

        EntitlementUpdateRequest request = new(
            Version: state.Version + 1,
            Status: isActive ? EntitlementStatuses.Active : EntitlementStatuses.Disabled,
            ValidUntilUtc: state.ValidUntilUtc,
            MaxDeviceTokens: state.MaxDeviceTokens);

        EntitlementUpdateResult result = await ApplyEntitlementAsync(
            publicGuid,
            request,
            now,
            cancellationToken);

        return result.Status is EntitlementUpdateStatus.Applied or EntitlementUpdateStatus.AlreadyApplied;
    }

    public async Task<CreateSubscriptionResult> CreateMediatedSubscriptionAsync(
        CreateSubscriptionCommand command,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        ValidateEntitlement(command.Entitlement);

        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await using SqliteTransaction transaction = (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);

            if (!string.IsNullOrWhiteSpace(command.ExternalRequestId))
            {
                SubscriptionIdentity? existing = await FindByExternalRequestIdAsync(
                    connection,
                    transaction,
                    command.ExternalRequestId,
                    cancellationToken);

                if (existing is not null)
                {
                    await transaction.CommitAsync(cancellationToken);
                    return new CreateSubscriptionResult(existing.PublicGuid, true);
                }
            }

            Guid publicGuid = command.PublicGuid ?? Guid.NewGuid();

            const string insertSubscription = """
                INSERT INTO mediated_subscriptions
                    (public_guid, external_request_id, customer_reference, note, created_at_utc, updated_at_utc)
                VALUES
                    ($public_guid, $external_request_id, $customer_reference, $note, $created_at_utc, $updated_at_utc)
                RETURNING id;
                """;

            await using SqliteCommand insertCommand = new(insertSubscription, connection, transaction);
            insertCommand.Parameters.AddWithValue("$public_guid", publicGuid.ToString("D"));
            insertCommand.Parameters.AddWithValue("$external_request_id", DbValue(command.ExternalRequestId));
            insertCommand.Parameters.AddWithValue("$customer_reference", DbValue(command.CustomerReference));
            insertCommand.Parameters.AddWithValue("$note", DbValue(command.Note));
            insertCommand.Parameters.AddWithValue("$created_at_utc", Format(now));
            insertCommand.Parameters.AddWithValue("$updated_at_utc", Format(now));
            long subscriptionId = (long)(await insertCommand.ExecuteScalarAsync(cancellationToken)
                ?? throw new InvalidOperationException("Subscription insert did not return an id."));

            await UpsertEntitlementAsync(
                connection,
                transaction,
                subscriptionId,
                command.Entitlement,
                now,
                cancellationToken);

            string creationOperationType = "subscription_creation";
            string creationOperationId = $"subscription-creation:{publicGuid:N}";
            string creationFingerprint = ComputeEntitlementFingerprint(
                publicGuid,
                creationOperationType,
                0,
                command.Entitlement.Status,
                command.Entitlement.ValidUntilUtc,
                command.Entitlement.MaxDeviceTokens);
            await InsertEntitlementOperationAsync(
                connection,
                transaction,
                subscriptionId,
                creationOperationId,
                creationOperationType,
                creationFingerprint,
                0,
                command.Entitlement,
                now,
                cancellationToken);

            await AddAuditAsync(
                connection,
                transaction,
                now,
                "subscription.created",
                publicGuid,
                null,
                null,
                null,
                cancellationToken);

            await AddAuditAsync(
                connection,
                transaction,
                now,
                "entitlement.updated",
                publicGuid,
                null,
                null,
                null,
                cancellationToken);

            await transaction.CommitAsync(cancellationToken);
            return new CreateSubscriptionResult(publicGuid, false);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task<EntitlementOperationResult> ApplyEntitlementOperationAsync(
        Guid publicGuid,
        EntitlementOperationRequest request,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        ValidateEntitlementOperation(request);
        string fingerprint = ComputeEntitlementOperationFingerprint(publicGuid, request);

        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);

            EntitlementOperationResult? existing = await GetEntitlementOperationAsync(
                connection,
                transaction,
                request.OperationId,
                cancellationToken);

            if (existing is not null)
            {
                await transaction.CommitAsync(cancellationToken);
                return string.Equals(existing.RequestFingerprint, fingerprint, StringComparison.Ordinal)
                    ? existing with { Status = EntitlementOperationStatus.AlreadyApplied }
                    : existing with { Status = EntitlementOperationStatus.IdempotencyConflict };
            }

            SubscriptionIdentity? subscription = await FindByPublicGuidAsync(
                connection,
                transaction,
                publicGuid,
                cancellationToken);

            if (subscription is null)
            {
                await transaction.CommitAsync(cancellationToken);
                return EntitlementOperationResult.NotFound(request.OperationId, fingerprint);
            }

            EntitlementMirror? current = await GetEntitlementAsync(
                connection,
                transaction,
                subscription.Id,
                cancellationToken);

            if (current is null || current.Version != request.ExpectedVersion)
            {
                await transaction.CommitAsync(cancellationToken);
                return EntitlementOperationResult.VersionConflict(
                    request.OperationId,
                    fingerprint,
                    current?.Version);
            }

            if (request.MaxDeviceTokens < current.MaxDeviceTokens)
            {
                await transaction.CommitAsync(cancellationToken);
                return EntitlementOperationResult.DeviceLimitDecrease(
                    request.OperationId,
                    fingerprint,
                    current.Version);
            }

            int activeDeviceTokens = await CountActiveDeviceTokensAsync(
                connection,
                transaction,
                subscription.Id,
                cancellationToken);
            if (request.MaxDeviceTokens < activeDeviceTokens)
            {
                await transaction.CommitAsync(cancellationToken);
                return EntitlementOperationResult.ActiveDevicesConflict(
                    request.OperationId,
                    fingerprint,
                    current.Version,
                    activeDeviceTokens);
            }

            EntitlementUpdateRequest update = new(
                Version: current.Version + 1,
                Status: request.Status,
                ValidUntilUtc: request.ValidUntilUtc,
                MaxDeviceTokens: request.MaxDeviceTokens);
            ValidateEntitlement(update);
            await UpsertEntitlementAsync(
                connection,
                transaction,
                subscription.Id,
                update,
                now,
                cancellationToken);

            await InsertEntitlementOperationAsync(
                connection,
                transaction,
                subscription.Id,
                request.OperationId,
                request.OperationType,
                fingerprint,
                request.ExpectedVersion,
                update,
                now,
                cancellationToken);

            await AddAuditAsync(
                connection,
                transaction,
                now,
                $"entitlement.operation.{request.OperationType}",
                publicGuid,
                null,
                null,
                null,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);

            return new EntitlementOperationResult(
                EntitlementOperationStatus.Applied,
                request.OperationId,
                fingerprint,
                publicGuid,
                request.OperationType,
                request.ExpectedVersion,
                update.Version,
                update.Status,
                update.ValidUntilUtc,
                update.MaxDeviceTokens,
                now,
                activeDeviceTokens);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task<EntitlementOperationResult?> GetEntitlementOperationAsync(
        string operationId,
        CancellationToken cancellationToken)
    {
        await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
        return await GetEntitlementOperationAsync(connection, null, operationId, cancellationToken);
    }

    public async Task<EntitlementOperationResult?> GetEntitlementOperationByResultVersionAsync(
        Guid publicGuid,
        long resultVersion,
        CancellationToken cancellationToken)
    {
        if (resultVersion < 1)
        {
            throw new ArgumentOutOfRangeException(nameof(resultVersion));
        }

        await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
        const string sql = """
            SELECT o.operation_id, o.request_fingerprint, s.public_guid,
                   o.operation_type, o.expected_version, o.result_version,
                   o.result_status, o.result_valid_until_utc,
                   o.result_max_device_tokens, o.applied_at_utc
            FROM entitlement_operations o
            JOIN mediated_subscriptions s ON s.id = o.subscription_id
            WHERE s.public_guid = $public_guid
              AND o.result_version = $result_version;
            """;
        await using SqliteCommand command = new(sql, connection);
        command.Parameters.AddWithValue("$public_guid", publicGuid.ToString("D"));
        command.Parameters.AddWithValue("$result_version", resultVersion);
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);
        return await ReadEntitlementOperationAsync(reader, cancellationToken);
    }

    public async Task<EntitlementUpdateResult> ApplyEntitlementAsync(
        Guid publicGuid,
        EntitlementUpdateRequest request,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        ValidateEntitlement(request);

        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await using SqliteTransaction transaction = (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            SubscriptionIdentity? subscription = await FindByPublicGuidAsync(connection, transaction, publicGuid, cancellationToken);

            if (subscription is null)
            {
                await transaction.CommitAsync(cancellationToken);
                return new EntitlementUpdateResult(EntitlementUpdateStatus.SubscriptionNotFound, null);
            }

            EntitlementMirror? current = await GetEntitlementAsync(connection, transaction, subscription.Id, cancellationToken);
            int activeDeviceTokens = await CountActiveDeviceTokensAsync(
                connection,
                transaction,
                subscription.Id,
                cancellationToken);

            if (current is not null && request.MaxDeviceTokens < current.MaxDeviceTokens)
            {
                await transaction.CommitAsync(cancellationToken);
                return new EntitlementUpdateResult(
                    EntitlementUpdateStatus.DeviceLimitDecreaseNotAllowed,
                    current.Version,
                    activeDeviceTokens);
            }

            if (request.MaxDeviceTokens < activeDeviceTokens)
            {
                await transaction.CommitAsync(cancellationToken);
                return new EntitlementUpdateResult(
                    EntitlementUpdateStatus.ActiveDevicesExceedNewLimit,
                    current?.Version,
                    activeDeviceTokens);
            }

            if (current is not null)
            {
                if (request.Version < current.Version)
                {
                    await transaction.CommitAsync(cancellationToken);
                    return new EntitlementUpdateResult(EntitlementUpdateStatus.StaleVersionRejected, current.Version, activeDeviceTokens);
                }

                if (request.Version == current.Version)
                {
                    bool same = string.Equals(request.Status, current.Status, StringComparison.Ordinal)
                        && request.ValidUntilUtc == current.ValidUntilUtc
                        && request.MaxDeviceTokens == current.MaxDeviceTokens;

                    await transaction.CommitAsync(cancellationToken);
                    return new EntitlementUpdateResult(
                        same ? EntitlementUpdateStatus.AlreadyApplied : EntitlementUpdateStatus.InvalidState,
                        current.Version,
                        activeDeviceTokens);
                }
            }

            await UpsertEntitlementAsync(
                connection,
                transaction,
                subscription.Id,
                request,
                now,
                cancellationToken);

            await AddAuditAsync(
                connection,
                transaction,
                now,
                "entitlement.updated",
                publicGuid,
                null,
                null,
                null,
                cancellationToken);

            await transaction.CommitAsync(cancellationToken);
            return new EntitlementUpdateResult(EntitlementUpdateStatus.Applied, request.Version, activeDeviceTokens);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task<DeviceTokenCreateResult> CreateDeviceTokenAsync(
        Guid publicGuid,
        CreateDeviceTokenRequest request,
        HttpContext httpContext,
        IDeviceSubscriptionLinkFactory linkFactory,
        DateTimeOffset now,
        CancellationToken cancellationToken,
        string? issuanceKey = null)
    {
        if (!DeviceIssuanceKeyValidator.TryNormalize(issuanceKey, out string? normalizedIssuanceKey))
        {
            return DeviceTokenCreateResult.Invalid("invalid_idempotency_key");
        }

        string? requestedPlatform = NormalizeRequestedPlatform(request.RequestedPlatform);

        if (request.RequestedPlatform is not null && requestedPlatform is null)
        {
            return DeviceTokenCreateResult.Invalid("invalid_requested_platform");
        }

        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await using SqliteTransaction transaction = connection.BeginTransaction(deferred: false);
            SubscriptionIdentity? subscription = await FindByPublicGuidAsync(
                connection,
                transaction,
                publicGuid,
                cancellationToken);

            if (subscription is null)
            {
                await transaction.CommitAsync(cancellationToken);
                return DeviceTokenCreateResult.NotFound();
            }

            EntitlementMirror? entitlement = await GetEntitlementAsync(
                connection,
                transaction,
                subscription.Id,
                cancellationToken);

            if (entitlement is null)
            {
                await transaction.CommitAsync(cancellationToken);
                return DeviceTokenCreateResult.Invalid("entitlement_missing");
            }

            if (string.Equals(
                entitlement.Status,
                EntitlementStatuses.Expired,
                StringComparison.Ordinal))
            {
                await transaction.CommitAsync(cancellationToken);
                return DeviceTokenCreateResult.Invalid("subscription_expired");
            }

            if (!string.Equals(
                entitlement.Status,
                EntitlementStatuses.Active,
                StringComparison.Ordinal))
            {
                await transaction.CommitAsync(cancellationToken);
                return DeviceTokenCreateResult.Invalid("subscription_disabled");
            }

            if (entitlement.ValidUntilUtc is not null && entitlement.ValidUntilUtc <= now)
            {
                await transaction.CommitAsync(cancellationToken);
                return DeviceTokenCreateResult.Invalid("subscription_expired");
            }

            await ExpirePendingDeviceTokensAsync(
                connection,
                transaction,
                subscription.Id,
                now,
                cancellationToken);

            int activeCount = await CountActiveDeviceTokensAsync(
                connection,
                transaction,
                subscription.Id,
                cancellationToken);
            int pendingCount = await CountPendingDeviceTokensAsync(
                connection,
                transaction,
                subscription.Id,
                now,
                cancellationToken);
            string? requestedDisplayName = TextSanitizer.NullIfWhiteSpace(request.DisplayName);
            string displayName = requestedDisplayName
                ?? $"Устройство {activeCount + pendingCount + 1}";
            DeviceFeedPolicySeed requestedPolicy = CreateNewDeviceFeedPolicySeed();
            bool compatibilityRequest = normalizedIssuanceKey is null;

            if (compatibilityRequest && _options.RequireDeviceIssuanceKey)
            {
                await transaction.CommitAsync(cancellationToken);
                return DeviceTokenCreateResult.Invalid("device_issuance_key_required");
            }

            bool legacyCompatibilityRequest = compatibilityRequest
                && string.Equals(
                    requestedPolicy.PolicyMode,
                    DeviceFeedPolicyModes.Legacy,
                    StringComparison.Ordinal);
            string effectiveIssuanceKey = normalizedIssuanceKey
                ?? (legacyCompatibilityRequest
                    ? CreateLegacyCompatibilityIssuanceKey(displayName)
                    : CreateProtectedCompatibilityIssuanceKey(
                        requestedDisplayName,
                        requestedPlatform,
                        requestedPolicy.PolicyMode));
            string issuanceRequestHash = ComputeDeviceIssuanceRequestHash(
                publicGuid,
                requestedDisplayName,
                requestedPlatform);

            ExistingDeviceCredential? existing = await GetDeviceCredentialByIssuanceKeyAsync(
                connection,
                transaction,
                subscription.Id,
                effectiveIssuanceKey,
                cancellationToken);

            if (existing is not null)
            {
                if (!IsIssuanceReplayCompatible(
                    existing,
                    requestedDisplayName,
                    requestedPlatform,
                    issuanceRequestHash))
                {
                    await transaction.CommitAsync(cancellationToken);
                    return DeviceTokenCreateResult.Invalid("idempotency_key_reused");
                }

                if (existing.IssuanceRequestHash is null)
                {
                    await BackfillIssuanceRequestHashAsync(
                        connection,
                        transaction,
                        existing.Id,
                        issuanceRequestHash,
                        cancellationToken);
                }

                if (existing.RevokedAtUtc is not null)
                {
                    await transaction.CommitAsync(cancellationToken);
                    return DeviceTokenCreateResult.Invalid("device_issuance_revoked");
                }

                if (existing.ProtectedCredential is null)
                {
                    await transaction.CommitAsync(cancellationToken);
                    return DeviceTokenCreateResult.Invalid("credential_reissue_required");
                }

                if (!TryUnprotectCredential(
                    existing.ProtectedCredential,
                    out string? rawSecret,
                    out string? credentialError))
                {
                    await transaction.CommitAsync(cancellationToken);
                    return DeviceTokenCreateResult.Invalid(credentialError!);
                }

                if (compatibilityRequest)
                {
                    await AddAuditAsync(
                        connection,
                        transaction,
                        now,
                        legacyCompatibilityRequest
                            ? "legacy_issuance.used"
                            : "protected_compatibility_issuance.used",
                        publicGuid,
                        null,
                        null,
                        null,
                        cancellationToken);
                }

                await transaction.CommitAsync(cancellationToken);
                string existingUrl = linkFactory.CreateDeviceSubscriptionLink(
                    httpContext,
                    publicGuid,
                    existing.PublicId,
                    rawSecret!);

                return DeviceTokenCreateResult.Existing(
                    existing.PublicId,
                    existing.DisplayName,
                    existingUrl,
                    activeCount,
                    pendingCount,
                    activeCount + pendingCount,
                    entitlement.MaxDeviceTokens,
                    existing.PendingExpiresAtUtc);
            }

            ExistingDeviceCredential? legacyCandidate = null;

            if (legacyCompatibilityRequest)
            {
                legacyCandidate = await GetLegacyDeviceCredentialCandidateAsync(
                    connection,
                    transaction,
                    subscription.Id,
                    displayName,
                    cancellationToken);

                if (legacyCandidate?.ProtectedCredential is not null)
                {
                    await AssignIssuanceIdentityAsync(
                        connection,
                        transaction,
                        legacyCandidate.Id,
                        effectiveIssuanceKey,
                        issuanceRequestHash,
                        requestedPlatform,
                        cancellationToken);

                    if (!TryUnprotectCredential(
                        legacyCandidate.ProtectedCredential,
                        out string? rawSecret,
                        out string? credentialError))
                    {
                        await transaction.CommitAsync(cancellationToken);
                        return DeviceTokenCreateResult.Invalid(credentialError!);
                    }

                    await AddAuditAsync(
                        connection,
                        transaction,
                        now,
                        "legacy_issuance.used",
                        publicGuid,
                        null,
                        null,
                        null,
                        cancellationToken);
                    await transaction.CommitAsync(cancellationToken);
                    string existingUrl = linkFactory.CreateDeviceSubscriptionLink(
                        httpContext,
                        publicGuid,
                        legacyCandidate.PublicId,
                        rawSecret!);

                    return DeviceTokenCreateResult.Existing(
                        legacyCandidate.PublicId,
                        legacyCandidate.DisplayName,
                        existingUrl,
                        activeCount,
                        pendingCount,
                        activeCount + pendingCount,
                        entitlement.MaxDeviceTokens,
                        legacyCandidate.PendingExpiresAtUtc);
                }
            }

            if (activeCount + pendingCount >= entitlement.MaxDeviceTokens)
            {
                await transaction.CommitAsync(cancellationToken);
                return DeviceTokenCreateResult.LimitReached(
                    activeCount,
                    pendingCount,
                    activeCount + pendingCount,
                    entitlement.MaxDeviceTokens);
            }

            string publicId = Guid.NewGuid().ToString("N");
            string rawNewSecret = TokenSecretGenerator.CreateSecret();
            string secretHash = DeviceTokenHasher.Hash(
                rawNewSecret,
                GetDeviceTokenHashKey());
            ProtectedDeviceCredential protectedCredential =
                _deviceCredentialProtector.Protect(rawNewSecret);
            DateTimeOffset pendingExpiresAt = now.AddMinutes(
                _options.PendingDeviceTokenTtlMinutes);

            await InsertDeviceCredentialAsync(
                connection,
                transaction,
                subscription.Id,
                publicId,
                secretHash,
                displayName,
                now,
                pendingExpiresAt,
                protectedCredential,
                effectiveIssuanceKey,
                issuanceRequestHash,
                requestedPlatform,
                requestedPolicy,
                cancellationToken);

            await AddAuditAsync(
                connection,
                transaction,
                now,
                "device_token.created",
                publicGuid,
                null,
                null,
                null,
                cancellationToken);

            if (compatibilityRequest)
            {
                await AddAuditAsync(
                    connection,
                    transaction,
                    now,
                    legacyCompatibilityRequest
                        ? legacyCandidate is null
                            ? "legacy_issuance.created"
                            : "legacy_issuance.replacement_created"
                        : "protected_compatibility_issuance.created",
                    publicGuid,
                    null,
                    null,
                    null,
                    cancellationToken);
            }

            await transaction.CommitAsync(cancellationToken);

            string connectionUrl = linkFactory.CreateDeviceSubscriptionLink(
                httpContext,
                publicGuid,
                publicId,
                rawNewSecret);

            return DeviceTokenCreateResult.Created(
                publicId,
                displayName,
                connectionUrl,
                activeCount,
                pendingCount + 1,
                activeCount + pendingCount + 1,
                entitlement.MaxDeviceTokens,
                pendingExpiresAt,
                false);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task<DeviceCredentialResult> GetDeviceCredentialAsync(
        Guid publicGuid,
        string devicePublicId,
        HttpContext httpContext,
        IDeviceSubscriptionLinkFactory linkFactory,
        CancellationToken cancellationToken)
    {
        await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
        const string sql = """
            SELECT d.id, d.display_name, d.revoked_at_utc, d.credential_key_id,
                   d.credential_nonce, d.credential_ciphertext, d.credential_tag
            FROM device_access_tokens d
            JOIN mediated_subscriptions s ON s.id = d.subscription_id
            WHERE s.public_guid = $public_guid
              AND d.public_id = $public_id;
            """;
        await using SqliteCommand command = new(sql, connection);
        command.Parameters.AddWithValue("$public_guid", publicGuid.ToString("D"));
        command.Parameters.AddWithValue("$public_id", devicePublicId);
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);

        if (!await reader.ReadAsync(cancellationToken))
        {
            return DeviceCredentialResult.NotFound();
        }

        long deviceId = reader.GetInt64(0);
        string displayName = reader.GetString(1);

        if (ReadString(reader, 2) is not null)
        {
            return DeviceCredentialResult.Invalid("device_token_revoked");
        }

        string? keyId = ReadString(reader, 3);
        string? nonce = ReadString(reader, 4);
        string? ciphertext = ReadString(reader, 5);
        string? tag = ReadString(reader, 6);

        if (keyId is null || nonce is null || ciphertext is null || tag is null)
        {
            return DeviceCredentialResult.Invalid("credential_reissue_required");
        }

        ProtectedDeviceCredential protectedCredential = new(keyId, nonce, ciphertext, tag);

        if (!TryUnprotectCredential(
            protectedCredential,
            out string? rawSecret,
            out string? credentialError))
        {
            return DeviceCredentialResult.Invalid(credentialError!);
        }

        await reader.DisposeAsync();

        if (_deviceCredentialProtector.NeedsReencryption(protectedCredential))
        {
            await ReadRepairDeviceCredentialAsync(
                deviceId,
                protectedCredential,
                rawSecret!,
                publicGuid,
                cancellationToken);
        }

        string connectionUrl = linkFactory.CreateDeviceSubscriptionLink(
            httpContext,
            publicGuid,
            devicePublicId,
            rawSecret!);
        return DeviceCredentialResult.Available(
            devicePublicId,
            displayName,
            connectionUrl);
    }

    public async Task<DeviceTokenCreateResult> RegenerateDeviceTokenAsync(
        Guid publicGuid,
        string devicePublicId,
        HttpContext httpContext,
        IDeviceSubscriptionLinkFactory linkFactory,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            SubscriptionIdentity? subscription = await FindByPublicGuidAsync(
                connection,
                transaction,
                publicGuid,
                cancellationToken);

            if (subscription is null)
            {
                await transaction.CommitAsync(cancellationToken);
                return DeviceTokenCreateResult.NotFound();
            }

            EntitlementMirror? entitlement = await GetEntitlementAsync(
                connection,
                transaction,
                subscription.Id,
                cancellationToken);

            if (entitlement is null)
            {
                await transaction.CommitAsync(cancellationToken);
                return DeviceTokenCreateResult.Invalid("entitlement_missing");
            }

            if (string.Equals(
                entitlement.Status,
                EntitlementStatuses.Expired,
                StringComparison.Ordinal)
                || entitlement.ValidUntilUtc is not null && entitlement.ValidUntilUtc <= now)
            {
                await transaction.CommitAsync(cancellationToken);
                return DeviceTokenCreateResult.Invalid("subscription_expired");
            }

            if (!string.Equals(
                entitlement.Status,
                EntitlementStatuses.Active,
                StringComparison.Ordinal))
            {
                await transaction.CommitAsync(cancellationToken);
                return DeviceTokenCreateResult.Invalid("subscription_disabled");
            }

            const string selectSql = """
                SELECT id, display_name, issuance_key, issuance_request_hash,
                       requested_platform, feed_policy_version, feed_policy_mode, binding_state,
                       bound_platform, bound_client_family, bound_at_utc,
                       bound_identity_hash, bound_identity_key_id, bound_identity_source,
                       last_identity_seen_at_utc, last_identity_mismatch_at_utc,
                       last_transfer_at_utc, transfer_count
                FROM device_access_tokens
                WHERE subscription_id = $subscription_id
                  AND public_id = $public_id
                  AND revoked_at_utc IS NULL;
                """;
            long existingId;
            string displayName;
            string? issuanceKey;
            string? issuanceRequestHash;
            string? requestedPlatform;
            DeviceFeedPolicySeed policySeed;
            await using (SqliteCommand selectCommand = new(selectSql, connection, transaction))
            {
                selectCommand.Parameters.AddWithValue(
                    "$subscription_id",
                    subscription.Id);
                selectCommand.Parameters.AddWithValue("$public_id", devicePublicId);
                await using SqliteDataReader reader =
                    await selectCommand.ExecuteReaderAsync(cancellationToken);

                if (!await reader.ReadAsync(cancellationToken))
                {
                    await transaction.CommitAsync(cancellationToken);
                    return DeviceTokenCreateResult.Invalid("device_token_not_found");
                }

                existingId = reader.GetInt64(0);
                displayName = reader.GetString(1);
                issuanceKey = ReadString(reader, 2);
                issuanceRequestHash = ReadString(reader, 3);
                requestedPlatform = ReadString(reader, 4);
                policySeed = new DeviceFeedPolicySeed(
                    PolicyVersion: reader.GetInt32(5),
                    PolicyMode: reader.GetString(6),
                    BindingState: reader.GetString(7),
                    BoundPlatform: ReadString(reader, 8),
                    BoundClientFamily: ReadString(reader, 9),
                    BoundAtUtc: ReadDate(reader, 10),
                    BoundIdentityHash: ReadString(reader, 11),
                    BoundIdentityKeyId: ReadString(reader, 12),
                    BoundIdentitySource: ReadString(reader, 13),
                    LastIdentitySeenAtUtc: ReadDate(reader, 14),
                    LastIdentityMismatchAtUtc: ReadDate(reader, 15),
                    LastTransferAtUtc: ReadDate(reader, 16),
                    TransferCount: reader.GetInt32(17));
            }

            if (issuanceKey is not null)
            {
                await using SqliteCommand clearIssuance = new(
                    "UPDATE device_access_tokens SET issuance_key = NULL, issuance_request_hash = NULL WHERE id = $id;",
                    connection,
                    transaction);
                clearIssuance.Parameters.AddWithValue("$id", existingId);
                await clearIssuance.ExecuteNonQueryAsync(cancellationToken);
            }

            await RevokeDeviceTokenByIdAsync(
                connection,
                transaction,
                existingId,
                now,
                "regenerated",
                cancellationToken);
            await ExpirePendingDeviceTokensAsync(
                connection,
                transaction,
                subscription.Id,
                now,
                cancellationToken);
            int activeCount = await CountActiveDeviceTokensAsync(
                connection,
                transaction,
                subscription.Id,
                cancellationToken);
            int pendingCount = await CountPendingDeviceTokensAsync(
                connection,
                transaction,
                subscription.Id,
                now,
                cancellationToken);

            if (activeCount + pendingCount >= entitlement.MaxDeviceTokens)
            {
                await transaction.RollbackAsync(cancellationToken);
                return DeviceTokenCreateResult.LimitReached(
                    activeCount,
                    pendingCount,
                    activeCount + pendingCount,
                    entitlement.MaxDeviceTokens);
            }

            string newPublicId = Guid.NewGuid().ToString("N");
            string rawSecret = TokenSecretGenerator.CreateSecret();
            string secretHash = DeviceTokenHasher.Hash(rawSecret, GetDeviceTokenHashKey());
            ProtectedDeviceCredential protectedCredential =
                _deviceCredentialProtector.Protect(rawSecret);
            DateTimeOffset pendingExpiresAt = now.AddMinutes(
                _options.PendingDeviceTokenTtlMinutes);
            await InsertDeviceCredentialAsync(
                connection,
                transaction,
                subscription.Id,
                newPublicId,
                secretHash,
                displayName,
                now,
                pendingExpiresAt,
                protectedCredential,
                issuanceKey,
                issuanceRequestHash,
                requestedPlatform,
                policySeed,
                cancellationToken);
            await AddAuditAsync(
                connection,
                transaction,
                now,
                "device_token.regenerated",
                publicGuid,
                null,
                null,
                null,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);

            string connectionUrl = linkFactory.CreateDeviceSubscriptionLink(
                httpContext,
                publicGuid,
                newPublicId,
                rawSecret);
            return DeviceTokenCreateResult.Created(
                newPublicId,
                displayName,
                connectionUrl,
                activeCount,
                pendingCount + 1,
                activeCount + pendingCount + 1,
                entitlement.MaxDeviceTokens,
                pendingExpiresAt,
                false);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task<DeviceTokenCreateResult> TransferDeviceTokenAsync(
        Guid publicGuid,
        string devicePublicId,
        TransferDeviceTokenRequest request,
        HttpContext httpContext,
        IDeviceSubscriptionLinkFactory linkFactory,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        if (!DeviceTransferOperationIdValidator.TryNormalize(
            request.OperationId,
            out string? operationId))
        {
            return DeviceTokenCreateResult.Invalid("invalid_idempotency_key");
        }

        string? requestedPlatform = NormalizeRequestedPlatform(request.RequestedPlatform);
        if (requestedPlatform is null)
        {
            return DeviceTokenCreateResult.Invalid("invalid_requested_platform");
        }

        string requestFingerprint = ComputeDeviceTransferFingerprint(
            publicGuid,
            devicePublicId,
            requestedPlatform);

        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await using SqliteTransaction transaction = connection.BeginTransaction(deferred: false);

            const string replaySql = """
                SELECT o.request_fingerprint, source.public_id,
                       result.id, result.public_id, result.display_name,
                       result.pending_expires_at_utc, result.credential_key_id,
                       result.credential_nonce, result.credential_ciphertext,
                       result.credential_tag, o.subscription_id, e.max_device_tokens
                FROM device_feed_transfer_operations o
                JOIN device_access_tokens source ON source.id = o.source_device_token_id
                JOIN device_access_tokens result ON result.id = o.result_device_token_id
                JOIN entitlement_mirrors e ON e.subscription_id = o.subscription_id
                JOIN mediated_subscriptions s ON s.id = o.subscription_id
                WHERE o.operation_id = $operation_id
                  AND s.public_guid = $public_guid;
                """;
            await using (SqliteCommand replayCommand = new(replaySql, connection, transaction))
            {
                replayCommand.Parameters.AddWithValue("$operation_id", operationId!);
                replayCommand.Parameters.AddWithValue("$public_guid", publicGuid.ToString("D"));
                await using SqliteDataReader replayReader =
                    await replayCommand.ExecuteReaderAsync(cancellationToken);

                if (await replayReader.ReadAsync(cancellationToken))
                {
                    string storedFingerprint = replayReader.GetString(0);
                    string storedSourcePublicId = replayReader.GetString(1);
                    if (!string.Equals(
                            storedFingerprint,
                            requestFingerprint,
                            StringComparison.Ordinal)
                        || !string.Equals(
                            storedSourcePublicId,
                            devicePublicId,
                            StringComparison.Ordinal))
                    {
                        await transaction.CommitAsync(cancellationToken);
                        return DeviceTokenCreateResult.Invalid("idempotency_key_reused");
                    }

                    string replayResultPublicId = replayReader.GetString(3);
                    string replayResultDisplayName = replayReader.GetString(4);
                    DateTimeOffset? pendingExpiresAt = ReadDate(replayReader, 5);
                    string? keyId = ReadString(replayReader, 6);
                    string? nonce = ReadString(replayReader, 7);
                    string? ciphertext = ReadString(replayReader, 8);
                    string? tag = ReadString(replayReader, 9);
                    long subscriptionId = replayReader.GetInt64(10);
                    int maxDeviceTokens = replayReader.GetInt32(11);
                    await replayReader.DisposeAsync();

                    if (keyId is null || nonce is null || ciphertext is null || tag is null)
                    {
                        await transaction.CommitAsync(cancellationToken);
                        return DeviceTokenCreateResult.Invalid("credential_reissue_required");
                    }

                    if (!TryUnprotectCredential(
                        new ProtectedDeviceCredential(keyId, nonce, ciphertext, tag),
                        out string? replayRawSecret,
                        out string? credentialError))
                    {
                        await transaction.CommitAsync(cancellationToken);
                        return DeviceTokenCreateResult.Invalid(credentialError!);
                    }

                    int activeCount = await CountActiveDeviceTokensAsync(
                        connection,
                        transaction,
                        subscriptionId,
                        cancellationToken);
                    int pendingCount = await CountPendingDeviceTokensAsync(
                        connection,
                        transaction,
                        subscriptionId,
                        now,
                        cancellationToken);
                    await transaction.CommitAsync(cancellationToken);
                    string replayUrl = linkFactory.CreateDeviceSubscriptionLink(
                        httpContext,
                        publicGuid,
                        replayResultPublicId,
                        replayRawSecret!);
                    return DeviceTokenCreateResult.Existing(
                        replayResultPublicId,
                        replayResultDisplayName,
                        replayUrl,
                        activeCount,
                        pendingCount,
                        activeCount + pendingCount,
                        maxDeviceTokens,
                        pendingExpiresAt);
                }
            }

            SubscriptionIdentity? subscription = await FindByPublicGuidAsync(
                connection,
                transaction,
                publicGuid,
                cancellationToken);
            if (subscription is null)
            {
                await transaction.CommitAsync(cancellationToken);
                return DeviceTokenCreateResult.NotFound();
            }

            EntitlementMirror? entitlement = await GetEntitlementAsync(
                connection,
                transaction,
                subscription.Id,
                cancellationToken);
            if (entitlement is null)
            {
                await transaction.CommitAsync(cancellationToken);
                return DeviceTokenCreateResult.Invalid("entitlement_missing");
            }

            if (string.Equals(entitlement.Status, EntitlementStatuses.Expired, StringComparison.Ordinal)
                || entitlement.ValidUntilUtc is not null && entitlement.ValidUntilUtc <= now)
            {
                await transaction.CommitAsync(cancellationToken);
                return DeviceTokenCreateResult.Invalid("subscription_expired");
            }

            if (!string.Equals(entitlement.Status, EntitlementStatuses.Active, StringComparison.Ordinal))
            {
                await transaction.CommitAsync(cancellationToken);
                return DeviceTokenCreateResult.Invalid("subscription_disabled");
            }

            const string sourceSql = """
                SELECT id, display_name, issuance_key, issuance_request_hash,
                       last_transfer_at_utc, transfer_count
                FROM device_access_tokens
                WHERE subscription_id = $subscription_id
                  AND public_id = $public_id
                  AND revoked_at_utc IS NULL;
                """;
            long sourceDeviceId;
            string displayName;
            string? issuanceKey;
            string? issuanceRequestHash;
            DateTimeOffset? lastTransferAtUtc;
            int transferCount;
            await using (SqliteCommand sourceCommand = new(sourceSql, connection, transaction))
            {
                sourceCommand.Parameters.AddWithValue("$subscription_id", subscription.Id);
                sourceCommand.Parameters.AddWithValue("$public_id", devicePublicId);
                await using SqliteDataReader sourceReader =
                    await sourceCommand.ExecuteReaderAsync(cancellationToken);
                if (!await sourceReader.ReadAsync(cancellationToken))
                {
                    await transaction.CommitAsync(cancellationToken);
                    return DeviceTokenCreateResult.Invalid("device_token_not_found");
                }

                sourceDeviceId = sourceReader.GetInt64(0);
                displayName = sourceReader.GetString(1);
                issuanceKey = ReadString(sourceReader, 2);
                issuanceRequestHash = ReadString(sourceReader, 3);
                lastTransferAtUtc = ReadDate(sourceReader, 4);
                transferCount = sourceReader.GetInt32(5);
            }

            if (lastTransferAtUtc is not null
                && lastTransferAtUtc.Value.AddHours(_options.DeviceFeedTransferCooldownHours) > now)
            {
                await transaction.CommitAsync(cancellationToken);
                return DeviceTokenCreateResult.Invalid("device_transfer_cooldown_active");
            }

            if (issuanceKey is not null)
            {
                await using SqliteCommand clearIssuance = new(
                    "UPDATE device_access_tokens SET issuance_key = NULL, issuance_request_hash = NULL WHERE id = $id;",
                    connection,
                    transaction);
                clearIssuance.Parameters.AddWithValue("$id", sourceDeviceId);
                await clearIssuance.ExecuteNonQueryAsync(cancellationToken);
            }

            await RevokeDeviceTokenByIdAsync(
                connection,
                transaction,
                sourceDeviceId,
                now,
                "transferred",
                cancellationToken);
            await ExpirePendingDeviceTokensAsync(
                connection,
                transaction,
                subscription.Id,
                now,
                cancellationToken);
            int activeDeviceTokens = await CountActiveDeviceTokensAsync(
                connection,
                transaction,
                subscription.Id,
                cancellationToken);
            int pendingDeviceTokens = await CountPendingDeviceTokensAsync(
                connection,
                transaction,
                subscription.Id,
                now,
                cancellationToken);

            if (activeDeviceTokens + pendingDeviceTokens >= entitlement.MaxDeviceTokens)
            {
                await transaction.RollbackAsync(cancellationToken);
                return DeviceTokenCreateResult.LimitReached(
                    activeDeviceTokens,
                    pendingDeviceTokens,
                    activeDeviceTokens + pendingDeviceTokens,
                    entitlement.MaxDeviceTokens);
            }

            string resultPublicId = Guid.NewGuid().ToString("N");
            string rawSecret = TokenSecretGenerator.CreateSecret();
            string secretHash = DeviceTokenHasher.Hash(rawSecret, GetDeviceTokenHashKey());
            ProtectedDeviceCredential protectedCredential =
                _deviceCredentialProtector.Protect(rawSecret);
            DateTimeOffset pendingExpiresAtUtc = now.AddMinutes(
                _options.PendingDeviceTokenTtlMinutes);
            DeviceFeedPolicySeed policySeed = new(
                PolicyVersion: DeviceFeedPolicyVersions.HwidIdentity,
                PolicyMode: DeviceFeedPolicyModes.Enforce,
                BindingState: DeviceFeedBindingStates.Unbound,
                BoundPlatform: null,
                BoundClientFamily: null,
                BoundAtUtc: null,
                BoundIdentityHash: null,
                BoundIdentityKeyId: null,
                BoundIdentitySource: null,
                LastIdentitySeenAtUtc: null,
                LastIdentityMismatchAtUtc: null,
                LastTransferAtUtc: now,
                TransferCount: transferCount + 1);
            long resultDeviceId = await InsertDeviceCredentialAsync(
                connection,
                transaction,
                subscription.Id,
                resultPublicId,
                secretHash,
                displayName,
                now,
                pendingExpiresAtUtc,
                protectedCredential,
                issuanceKey,
                issuanceRequestHash,
                requestedPlatform,
                policySeed,
                cancellationToken);

            const string insertOperation = """
                INSERT INTO device_feed_transfer_operations
                    (operation_id, subscription_id, source_device_token_id,
                     result_device_token_id, request_fingerprint, applied_at_utc)
                VALUES
                    ($operation_id, $subscription_id, $source_device_token_id,
                     $result_device_token_id, $request_fingerprint, $applied_at_utc);
                """;
            await using (SqliteCommand operationCommand = new(
                insertOperation,
                connection,
                transaction))
            {
                operationCommand.Parameters.AddWithValue("$operation_id", operationId!);
                operationCommand.Parameters.AddWithValue("$subscription_id", subscription.Id);
                operationCommand.Parameters.AddWithValue("$source_device_token_id", sourceDeviceId);
                operationCommand.Parameters.AddWithValue("$result_device_token_id", resultDeviceId);
                operationCommand.Parameters.AddWithValue("$request_fingerprint", requestFingerprint);
                operationCommand.Parameters.AddWithValue("$applied_at_utc", Format(now));
                await operationCommand.ExecuteNonQueryAsync(cancellationToken);
            }

            await AddAuditAsync(
                connection,
                transaction,
                now,
                "device_token.transferred",
                publicGuid,
                null,
                null,
                null,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);

            string connectionUrl = linkFactory.CreateDeviceSubscriptionLink(
                httpContext,
                publicGuid,
                resultPublicId,
                rawSecret);
            return DeviceTokenCreateResult.Created(
                resultPublicId,
                displayName,
                connectionUrl,
                activeDeviceTokens,
                pendingDeviceTokens + 1,
                activeDeviceTokens + pendingDeviceTokens + 1,
                entitlement.MaxDeviceTokens,
                pendingExpiresAtUtc,
                false);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task<IReadOnlyList<DeviceTokenListItem>> ListDeviceTokensAsync(
        Guid publicGuid,
        CancellationToken cancellationToken)
    {
        await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);

        const string sql = """
            SELECT d.public_id, d.display_name, d.created_at_utc, d.activated_at_utc,
                   d.last_used_at_utc, d.revoked_at_utc, d.pending_expires_at_utc,
                   d.revocation_reason, d.device_type, d.platform, d.detected_model,
                   d.detection_source, d.first_fetched_at_utc, d.feed_policy_version,
                   d.feed_policy_mode, d.binding_state, d.bound_platform, d.bound_client_family,
                   d.bound_at_utc, d.bound_identity_hash IS NOT NULL,
                   d.bound_identity_source, d.last_identity_seen_at_utc,
                   d.last_identity_mismatch_at_utc, d.last_transfer_at_utc,
                   d.transfer_count, d.risk_score, d.access_channel, d.device_state
            FROM device_access_tokens d
            JOIN mediated_subscriptions s ON s.id = d.subscription_id
            WHERE s.public_guid = $public_guid
            ORDER BY d.created_at_utc ASC, d.id ASC;
            """;

        await using SqliteCommand command = new(sql, connection);
        command.Parameters.AddWithValue("$public_guid", publicGuid.ToString("D"));

        List<DeviceTokenListItem> devices = [];
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);

        while (await reader.ReadAsync(cancellationToken))
        {
            devices.Add(new DeviceTokenListItem(
                PublicId: reader.GetString(0),
                DisplayName: reader.GetString(1),
                CreatedAtUtc: Parse(reader.GetString(2)),
                ActivatedAtUtc: ReadDate(reader, 3),
                LastUsedAtUtc: ReadDate(reader, 4),
                RevokedAtUtc: ReadDate(reader, 5),
                PendingExpiresAtUtc: ReadDate(reader, 6),
                RevocationReason: ReadString(reader, 7),
                DeviceType: ReadString(reader, 8),
                Platform: ReadString(reader, 9),
                DetectedModel: ReadString(reader, 10),
                DetectionSource: ReadString(reader, 11),
                FirstFetchedAtUtc: ReadDate(reader, 12),
                State: string.Equals(ReadString(reader, 26), "unified_feed", StringComparison.Ordinal)
                    ? UnifiedDeviceStateMapper.ToPublicState(
                        ReadString(reader, 27),
                        ReadDate(reader, 5),
                        ReadDate(reader, 6),
                        DateTimeOffset.UtcNow)
                    : DeviceTokenState.FromColumns(
                        ReadDate(reader, 3),
                        ReadDate(reader, 5),
                        ReadDate(reader, 6),
                        DateTimeOffset.UtcNow),
                FeedPolicyVersion: reader.GetInt32(13),
                FeedPolicyMode: reader.GetString(14),
                BindingState: reader.GetString(15),
                BoundPlatform: ReadString(reader, 16),
                BoundClientFamily: ReadString(reader, 17),
                BoundAtUtc: ReadDate(reader, 18),
                IdentityBound: reader.GetBoolean(19),
                IdentitySource: ReadString(reader, 20),
                LastIdentitySeenAtUtc: ReadDate(reader, 21),
                LastIdentityMismatchAtUtc: ReadDate(reader, 22),
                LastTransferAtUtc: ReadDate(reader, 23),
                TransferCount: reader.GetInt32(24),
                RiskScore: reader.GetInt32(25),
                AccessChannel: ReadString(reader, 26) ?? "device_link",
                DeviceState: ReadString(reader, 27)));
        }

        return devices;
    }

    public async Task<bool> RevokeDeviceTokenAsync(
        Guid publicGuid,
        string devicePublicId,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await using SqliteTransaction transaction = (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);

            const string sql = """
                UPDATE device_access_tokens
                SET revoked_at_utc = COALESCE(revoked_at_utc, $revoked_at_utc),
                    revocation_reason = COALESCE(revocation_reason, $revocation_reason),
                    credential_key_id = NULL,
                    credential_nonce = NULL,
                    credential_ciphertext = NULL,
                    credential_tag = NULL,
                    device_state = CASE
                        WHEN access_channel = 'unified_feed' THEN 'disabled'
                        ELSE device_state
                    END
                WHERE public_id = $public_id
                  AND subscription_id = (
                      SELECT id FROM mediated_subscriptions WHERE public_guid = $public_guid
                  );
                """;

            await using SqliteCommand command = new(sql, connection, transaction);
            command.Parameters.AddWithValue("$revoked_at_utc", Format(now));
            command.Parameters.AddWithValue("$revocation_reason", "user_revoked");
            command.Parameters.AddWithValue("$public_id", devicePublicId);
            command.Parameters.AddWithValue("$public_guid", publicGuid.ToString("D"));
            int affected = await command.ExecuteNonQueryAsync(cancellationToken);

            if (affected > 0)
            {
                await AddAuditAsync(connection, transaction, now, "device_token.revoked", publicGuid, null, null, null, cancellationToken);
            }

            await transaction.CommitAsync(cancellationToken);
            return affected > 0;
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task<DeviceTokenRevokeAllResult> RevokeAllDeviceTokensAsync(
        Guid publicGuid,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await using SqliteTransaction transaction = (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            SubscriptionIdentity? subscription = await FindByPublicGuidAsync(connection, transaction, publicGuid, cancellationToken);

            if (subscription is null)
            {
                await transaction.CommitAsync(cancellationToken);
                return new DeviceTokenRevokeAllResult(false, 0);
            }

            const string sql = """
                UPDATE device_access_tokens
                SET revoked_at_utc = $revoked_at_utc,
                    revocation_reason = COALESCE(revocation_reason, $revocation_reason),
                    credential_key_id = NULL,
                    credential_nonce = NULL,
                    credential_ciphertext = NULL,
                    credential_tag = NULL,
                    device_state = CASE
                        WHEN access_channel = 'unified_feed' THEN 'disabled'
                        ELSE device_state
                    END
                WHERE subscription_id = $subscription_id
                  AND revoked_at_utc IS NULL;
                """;

            await using SqliteCommand command = new(sql, connection, transaction);
            command.Parameters.AddWithValue("$revoked_at_utc", Format(now));
            command.Parameters.AddWithValue("$revocation_reason", "reset");
            command.Parameters.AddWithValue("$subscription_id", subscription.Id);
            int affected = await command.ExecuteNonQueryAsync(cancellationToken);

            if (affected > 0)
            {
                await AddAuditAsync(connection, transaction, now, "device_token.revoked", publicGuid, null, null, null, cancellationToken);
            }

            await transaction.CommitAsync(cancellationToken);
            return new DeviceTokenRevokeAllResult(true, affected);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task<TokenSubscriptionAccessResult> ValidateDeviceTokenAccessAsync(
        Guid publicGuid,
        string devicePublicId,
        string? token,
        DateTimeOffset now,
        CancellationToken cancellationToken,
        string? userAgent = null,
        IPAddress? remoteIpAddress = null,
        string? hwid = null,
        string? deviceOs = null,
        string? osVersion = null,
        string? deviceModel = null)
    {
        if (string.IsNullOrWhiteSpace(token))
        {
            return TokenSubscriptionAccessResult.Forbidden(UserFacingStatus.DeviceTokenInvalid);
        }

        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await using SqliteTransaction transaction = connection.BeginTransaction(deferred: false);

            const string sql = """
                SELECT s.id, e.status, e.valid_until_utc, e.max_device_tokens,
                       d.id, d.secret_hash, d.revoked_at_utc, d.activated_at_utc,
                       d.last_used_at_utc, d.pending_expires_at_utc,
                       d.credential_key_id, d.credential_nonce,
                       d.credential_ciphertext, d.credential_tag,
                       d.feed_policy_version, d.feed_policy_mode, d.binding_state,
                       d.requested_platform, d.bound_platform, d.bound_client_family,
                       d.bound_identity_hash, d.bound_identity_key_id,
                       d.bound_identity_source
                FROM mediated_subscriptions s
                JOIN entitlement_mirrors e ON e.subscription_id = s.id
                JOIN device_access_tokens d ON d.subscription_id = s.id
                WHERE s.public_guid = $public_guid
                  AND d.public_id = $public_id;
                """;

            await using SqliteCommand command = new(sql, connection, transaction);
            command.Parameters.AddWithValue("$public_guid", publicGuid.ToString("D"));
            command.Parameters.AddWithValue("$public_id", devicePublicId);
            await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);

            if (!await reader.ReadAsync(cancellationToken))
            {
                await transaction.CommitAsync(cancellationToken);
                return TokenSubscriptionAccessResult.Forbidden(UserFacingStatus.DeviceTokenInvalid);
            }

            long deviceId = reader.GetInt64(4);
            string secretHash = reader.GetString(5);
            string? revokedAt = ReadString(reader, 6);
            DateTimeOffset? activatedAt = ReadDate(reader, 7);
            DateTimeOffset? lastUsedAt = ReadDate(reader, 8);
            DateTimeOffset? pendingExpiresAt = ReadDate(reader, 9);
            string? credentialKeyId = ReadString(reader, 10);
            string? credentialNonce = ReadString(reader, 11);
            string? credentialCiphertext = ReadString(reader, 12);
            string? credentialTag = ReadString(reader, 13);
            DeviceFeedPolicyState feedPolicyState = new(
                PolicyVersion: reader.GetInt32(14),
                PolicyMode: reader.GetString(15),
                BindingState: reader.GetString(16),
                RequestedPlatform: ReadString(reader, 17),
                BoundPlatform: ReadString(reader, 18),
                BoundClientFamily: ReadString(reader, 19),
                BoundIdentityHash: ReadString(reader, 20),
                BoundIdentityKeyId: ReadString(reader, 21),
                BoundIdentitySource: ReadString(reader, 22));
            string status = reader.GetString(1);
            DateTimeOffset? validUntil = ReadDate(reader, 2);
            int maxDevices = reader.GetInt32(3);
            long subscriptionId = reader.GetInt64(0);
            await reader.DisposeAsync();

            if (revokedAt is not null)
            {
                await transaction.CommitAsync(cancellationToken);
                return TokenSubscriptionAccessResult.Forbidden(UserFacingStatus.DeviceTokenRevoked);
            }

            if (!DeviceTokenHasher.Verify(token, secretHash, GetDeviceTokenHashKey()))
            {
                await transaction.CommitAsync(cancellationToken);
                return TokenSubscriptionAccessResult.Forbidden(UserFacingStatus.DeviceTokenInvalid);
            }

            if (activatedAt is null && pendingExpiresAt is not null && pendingExpiresAt <= now)
            {
                await RevokeDeviceTokenByIdAsync(
                    connection,
                    transaction,
                    deviceId,
                    now,
                    "expired_pending",
                    cancellationToken);
                await transaction.CommitAsync(cancellationToken);
                return TokenSubscriptionAccessResult.Forbidden(UserFacingStatus.DeviceTokenExpired);
            }

            if (string.Equals(status, EntitlementStatuses.Expired, StringComparison.Ordinal))
            {
                await transaction.CommitAsync(cancellationToken);
                return TokenSubscriptionAccessResult.Forbidden(UserFacingStatus.SubscriptionExpired);
            }

            if (!string.Equals(status, EntitlementStatuses.Active, StringComparison.Ordinal))
            {
                await transaction.CommitAsync(cancellationToken);
                return TokenSubscriptionAccessResult.Forbidden(UserFacingStatus.SubscriptionDisabled);
            }

            if (validUntil is not null && validUntil <= now)
            {
                await transaction.CommitAsync(cancellationToken);
                return TokenSubscriptionAccessResult.Forbidden(UserFacingStatus.SubscriptionExpired);
            }

            int activeCount = await CountActiveDeviceTokensAsync(
                connection,
                transaction,
                subscriptionId,
                cancellationToken);

            if (activatedAt is null && activeCount >= maxDevices)
            {
                await transaction.CommitAsync(cancellationToken);
                return TokenSubscriptionAccessResult.Forbidden(
                    UserFacingStatus.DeviceLimitReached,
                    activeCount,
                    maxDevices);
            }

            if (credentialKeyId is null
                || credentialNonce is null
                || credentialCiphertext is null
                || credentialTag is null)
            {
                ProtectedDeviceCredential protectedCredential =
                    _deviceCredentialProtector.Protect(token);
                await BackfillDeviceCredentialAsync(
                    connection,
                    transaction,
                    deviceId,
                    protectedCredential,
                    cancellationToken);
                await AddAuditAsync(
                    connection,
                    transaction,
                    now,
                    "device_token.credential_backfilled",
                    publicGuid,
                    null,
                    null,
                    null,
                    cancellationToken);
            }

            DeviceAccessRequestContext requestContext = DeviceAccessRequestContextFactory.Create(
                userAgent,
                remoteIpAddress,
                _options.DeviceObservationHashKey,
                hwid,
                deviceOs,
                osVersion,
                deviceModel,
                _options.DeviceIdentityHashKeyId,
                _options.DeviceIdentityHashKey,
                _options.PreviousDeviceIdentityHashKeyId,
                _options.PreviousDeviceIdentityHashKey);
            DeviceFeedPolicyDecision policyDecision = DeviceFeedPolicyEvaluator.Evaluate(
                _options.DeviceFeedBindingMode,
                feedPolicyState,
                requestContext);

            if (policyDecision.ShouldRecordObservation)
            {
                await DeleteExpiredDeviceFeedObservationsAsync(
                    connection,
                    transaction,
                    deviceId,
                    now,
                    cancellationToken);
                await RecordDeviceFeedObservationAsync(
                    connection,
                    transaction,
                    deviceId,
                    feedPolicyState,
                    requestContext,
                    policyDecision,
                    now,
                    cancellationToken);
            }

            if (policyDecision.ShouldBind)
            {
                await BindDeviceFeedAsync(
                    connection,
                    transaction,
                    deviceId,
                    feedPolicyState,
                    requestContext,
                    now,
                    cancellationToken);
                await RecordDeviceFeedPolicyEventAsync(
                    connection,
                    transaction,
                    deviceId,
                    now,
                    "binding_created",
                    null,
                    requestContext.Metadata.Platform,
                    feedPolicyState.RequestedPlatform,
                    policyDecision.Decision,
                    requestContext,
                    feedPolicyState,
                    cancellationToken);
            }
            else if (policyDecision.ShouldRefreshIdentityHash)
            {
                await RefreshDeviceIdentityHashAsync(
                    connection,
                    transaction,
                    deviceId,
                    requestContext,
                    now,
                    cancellationToken);
            }
            else if (policyDecision.ReasonCode is not null)
            {
                await RecordDeviceFeedPolicyEventAsync(
                    connection,
                    transaction,
                    deviceId,
                    now,
                    policyDecision.Allowed
                        ? "policy_mismatch_observed"
                        : "policy_mismatch_denied",
                    policyDecision.ReasonCode,
                    requestContext.Metadata.Platform,
                    feedPolicyState.BoundPlatform ?? feedPolicyState.RequestedPlatform,
                    policyDecision.Decision,
                    requestContext,
                    feedPolicyState,
                    cancellationToken);
            }

            if (!policyDecision.Allowed)
            {
                await MarkIdentityMismatchAsync(
                    connection,
                    transaction,
                    deviceId,
                    policyDecision.ReasonCode,
                    now,
                    cancellationToken);
                await transaction.CommitAsync(cancellationToken);
                UserFacingStatus deniedStatus = policyDecision.ReasonCode
                    is "identity_missing" or "identity_invalid" or "identity_hash_unavailable"
                    ? UserFacingStatus.DeviceIdentityRequired
                    : UserFacingStatus.DeviceTransferRequired;
                return TokenSubscriptionAccessResult.Forbidden(
                    deniedStatus,
                    activeCount,
                    maxDevices,
                    policyDecision.ReasonCode,
                    feedPolicyState.BoundPlatform ?? feedPolicyState.RequestedPlatform);
            }

            if (requestContext.Identity.IsValid)
            {
                await MarkIdentitySeenAsync(
                    connection,
                    transaction,
                    deviceId,
                    now,
                    cancellationToken);
            }

            await MarkDeviceUsedAsync(
                connection,
                transaction,
                deviceId,
                activatedAt,
                lastUsedAt,
                now,
                requestContext.Metadata,
                cancellationToken);

            int resultingActiveCount = activatedAt is null ? activeCount + 1 : activeCount;
            await transaction.CommitAsync(cancellationToken);
            return TokenSubscriptionAccessResult.Permit(
                resultingActiveCount,
                maxDevices,
                validUntil);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task<TokenSubscriptionAccessResult> ValidateLegacyAccessAsync(
        Guid publicGuid,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        if (!_options.AllowLegacySubscriptionLinks)
        {
            return TokenSubscriptionAccessResult.Forbidden(UserFacingStatus.LegacyLinkDisabled);
        }

        if (await IsLegacyLinkRevokedAsync(publicGuid, cancellationToken))
        {
            return TokenSubscriptionAccessResult.Forbidden(UserFacingStatus.LegacyLinkDisabled);
        }

        SubscriptionRuntimeState? state = await GetRuntimeStateAsync(publicGuid, cancellationToken);

        if (state is null)
        {
            return TokenSubscriptionAccessResult.NotFound();
        }

        if (string.Equals(state.Status, EntitlementStatuses.Expired, StringComparison.Ordinal))
        {
            return TokenSubscriptionAccessResult.Forbidden(UserFacingStatus.SubscriptionExpired);
        }

        if (!string.Equals(state.Status, EntitlementStatuses.Active, StringComparison.Ordinal))
        {
            return TokenSubscriptionAccessResult.Forbidden(UserFacingStatus.SubscriptionDisabled);
        }

        if (state.ValidUntilUtc is not null && state.ValidUntilUtc <= now)
        {
            return TokenSubscriptionAccessResult.Forbidden(UserFacingStatus.SubscriptionExpired);
        }

        int activeCount = await CountActiveDeviceTokensAsync(publicGuid, cancellationToken);
        return TokenSubscriptionAccessResult.Permit(
            activeCount,
            state.MaxDeviceTokens,
            state.ValidUntilUtc);
    }

    public async Task<SourceDetails> CreateSourceAsync(
        CreateSourceRequest request,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        SourceValidator.ValidateCreate(request);
        string protectedEndpoint = _endpointProtector.Protect(request.Endpoint);

        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            const string sql = """
                INSERT INTO upstream_sources
                    (name, kind, encrypted_endpoint, state, sort_order, created_at_utc, updated_at_utc)
                VALUES
                    ($name, $kind, $endpoint, $state, $sort_order, $created_at_utc, $updated_at_utc)
                RETURNING id;
                """;

            await using SqliteCommand command = new(sql, connection, transaction);
            command.Parameters.AddWithValue("$name", request.Name.Trim());
            command.Parameters.AddWithValue("$kind", request.Kind.Trim());
            command.Parameters.AddWithValue("$endpoint", protectedEndpoint);
            command.Parameters.AddWithValue("$state", SourceStates.Draft);
            command.Parameters.AddWithValue("$sort_order", 100);
            command.Parameters.AddWithValue("$created_at_utc", Format(now));
            command.Parameters.AddWithValue("$updated_at_utc", Format(now));
            long id = (long)(await command.ExecuteScalarAsync(cancellationToken)
                ?? throw new InvalidOperationException("Source insert did not return an id."));

            await AddAuditAsync(
                connection,
                transaction,
                now,
                "source.created",
                null,
                id,
                null,
                null,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
            return new SourceDetails(id, request.Name.Trim(), request.Kind.Trim(), SourceStates.Draft, 100, null, null, null, null);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task<int> ReencryptSourceEndpointsAsync(
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            List<(long Id, string ProtectedEndpoint)> rows = [];
            const string selectSql = "SELECT id, encrypted_endpoint FROM upstream_sources;";
            await using (SqliteCommand command = new(selectSql, connection))
            await using (SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken))
            {
                while (await reader.ReadAsync(cancellationToken))
                {
                    rows.Add((reader.GetInt64(0), reader.GetString(1)));
                }
            }

            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            int reencrypted = 0;
            foreach ((long id, string protectedEndpoint) in rows)
            {
                if (!_endpointProtector.NeedsReencryption(protectedEndpoint))
                {
                    continue;
                }

                const string updateSql = """
                    UPDATE upstream_sources
                    SET encrypted_endpoint = $endpoint,
                        updated_at_utc = $updated_at_utc
                    WHERE id = $id;
                    """;
                await using SqliteCommand update = new(updateSql, connection, transaction);
                update.Parameters.AddWithValue(
                    "$endpoint",
                    _endpointProtector.Protect(_endpointProtector.Unprotect(protectedEndpoint)));
                update.Parameters.AddWithValue("$updated_at_utc", Format(now));
                update.Parameters.AddWithValue("$id", id);
                await update.ExecuteNonQueryAsync(cancellationToken);
                reencrypted++;
            }

            await AddAuditAsync(
                connection,
                transaction,
                now,
                "source.endpoints_reencrypted",
                null,
                null,
                null,
                null,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
            return reencrypted;
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task<IReadOnlyList<SourceDetails>> ListSourcesAsync(CancellationToken cancellationToken)
    {
        await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);

        const string sql = """
            SELECT id, name, kind, state, sort_order, last_tested_at_utc,
                   last_successful_refresh_at_utc, last_failed_refresh_at_utc, last_error_code
            FROM upstream_sources
            ORDER BY sort_order ASC, id ASC;
            """;

        await using SqliteCommand command = new(sql, connection);
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);
        List<SourceDetails> sources = [];

        while (await reader.ReadAsync(cancellationToken))
        {
            sources.Add(ReadSourceDetails(reader));
        }

        return sources;
    }

    public async Task<UpstreamSource?> GetSourceAsync(long sourceId, CancellationToken cancellationToken)
    {
        await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
        return await GetSourceAsync(connection, null, sourceId, includeEndpoint: true, cancellationToken);
    }

    public async Task<SourceTestResult> SaveSourceTestResultAsync(
        long sourceId,
        SourceReadResult readResult,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await using SqliteTransaction transaction = (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            UpstreamSource? source = await GetSourceAsync(connection, transaction, sourceId, includeEndpoint: false, cancellationToken);

            if (source is null)
            {
                await transaction.CommitAsync(cancellationToken);
                return SourceTestResult.NotFound(sourceId);
            }

            if (readResult.Success)
            {
                await InsertSourceSnapshotAsync(connection, transaction, sourceId, readResult, now, cancellationToken);
            }

            string nextState = readResult.Success && source.State == SourceStates.Draft
                ? SourceStates.Tested
                : source.State;

            const string updateSql = """
                UPDATE upstream_sources
                SET state = $state,
                    updated_at_utc = $updated_at_utc,
                    last_tested_at_utc = $last_tested_at_utc,
                    last_successful_refresh_at_utc = CASE WHEN $success = 1 THEN $now ELSE last_successful_refresh_at_utc END,
                    last_failed_refresh_at_utc = CASE WHEN $success = 0 THEN $now ELSE last_failed_refresh_at_utc END,
                    last_error_code = $last_error_code
                WHERE id = $id;
                """;

            await using SqliteCommand command = new(updateSql, connection, transaction);
            command.Parameters.AddWithValue("$state", nextState);
            command.Parameters.AddWithValue("$updated_at_utc", Format(now));
            command.Parameters.AddWithValue("$last_tested_at_utc", Format(now));
            command.Parameters.AddWithValue("$success", readResult.Success ? 1 : 0);
            command.Parameters.AddWithValue("$now", Format(now));
            command.Parameters.AddWithValue("$last_error_code", DbValue(readResult.ErrorCode));
            command.Parameters.AddWithValue("$id", sourceId);
            await command.ExecuteNonQueryAsync(cancellationToken);

            await AddAuditAsync(connection, transaction, now, "source.tested", null, sourceId, null, readResult.ErrorCode, cancellationToken);
            await transaction.CommitAsync(cancellationToken);

            return new SourceTestResult(
                sourceId,
                readResult.Success,
                readResult.ResponseTimeMs,
                readResult.ServerLinks.Count,
                readResult.AcceptedSchemes,
                readResult.FilteredCount,
                readResult.ValidationErrors,
                readResult.ErrorCode);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task<bool> SetSourceStateAsync(
        long sourceId,
        string desiredState,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        if (!SourceStates.All.Contains(desiredState, StringComparer.Ordinal))
        {
            throw new InvalidOperationException("Invalid source state.");
        }

        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await using SqliteTransaction transaction = (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            UpstreamSource? source = await GetSourceAsync(connection, transaction, sourceId, includeEndpoint: false, cancellationToken);

            if (source is null)
            {
                await transaction.CommitAsync(cancellationToken);
                return false;
            }

            if (desiredState == SourceStates.Enabled && source.State is not SourceStates.Tested and not SourceStates.Degraded and not SourceStates.Enabled)
            {
                throw new InvalidOperationException("Only tested or degraded sources can be enabled.");
            }

            const string sql = """
                UPDATE upstream_sources
                SET state = $state, updated_at_utc = $updated_at_utc
                WHERE id = $id;
                """;

            await using SqliteCommand command = new(sql, connection, transaction);
            command.Parameters.AddWithValue("$state", desiredState);
            command.Parameters.AddWithValue("$updated_at_utc", Format(now));
            command.Parameters.AddWithValue("$id", sourceId);
            await command.ExecuteNonQueryAsync(cancellationToken);

            string eventType = desiredState switch
            {
                SourceStates.Enabled => "source.enabled",
                SourceStates.Disabled => "source.disabled",
                SourceStates.Revoked => "source.revoked",
                _ => "source.updated"
            };

            await AddAuditAsync(connection, transaction, now, eventType, null, sourceId, null, null, cancellationToken);
            await transaction.CommitAsync(cancellationToken);
            return true;
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task<CatalogRefreshResult> RefreshCatalogAsync(
        IUpstreamSourceReaderRegistry readerRegistry,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await _catalogRefreshLock.WaitAsync(cancellationToken);

        try
        {
            IReadOnlyList<UpstreamSource> sources;

            await using (SqliteConnection connection = await OpenConnectionAsync(cancellationToken))
            {
                sources = await GetRefreshableSourcesAsync(connection, cancellationToken);
            }

            List<CatalogSourceRefreshResult> sourceResults = [];
            List<string> merged = [];
            Dictionary<long, long> sourceVersions = [];
            List<DateTimeOffset> sourceDataAges = [];

            foreach (UpstreamSource source in sources)
            {
                if (!readerRegistry.TryGet(source.Kind, out IUpstreamSourceReader? reader))
                {
                    sourceResults.Add(CatalogSourceRefreshResult.Failed(source.Id, "reader_missing"));
                    await MarkSourceRefreshFailedShortAsync(source.Id, now, "reader_missing", cancellationToken);
                    continue;
                }

                SourceReadResult readResult = await reader!.ReadAsync(source, cancellationToken);

                if (readResult.Success)
                {
                    long version = await PersistSourceRefreshSucceededAsync(
                        source.Id,
                        readResult,
                        now,
                        cancellationToken);

                    merged.AddRange(readResult.ServerLinks);
                    sourceVersions[source.Id] = version;
                    sourceDataAges.Add(now);
                    sourceResults.Add(CatalogSourceRefreshResult.Succeeded(source.Id, readResult.ServerLinks.Count));
                    continue;
                }

                SourceSnapshot? cached;

                await using (SqliteConnection connection = await OpenConnectionAsync(cancellationToken))
                {
                    cached = await GetLatestUsableSourceSnapshotAsync(connection, source.Id, now, cancellationToken);
                }

                if (cached is not null)
                {
                    merged.AddRange(cached.ServerLinks);
                    sourceVersions[source.Id] = cached.Version;
                    sourceDataAges.Add(cached.CreatedAtUtc);
                    sourceResults.Add(CatalogSourceRefreshResult.UsedCached(source.Id, cached.Version, readResult.ErrorCode ?? "refresh_failed"));
                }
                else
                {
                    sourceResults.Add(CatalogSourceRefreshResult.Failed(source.Id, readResult.ErrorCode ?? "refresh_failed"));
                }

                await MarkSourceRefreshFailedShortAsync(source.Id, now, readResult.ErrorCode ?? "refresh_failed", cancellationToken);
            }

            IReadOnlyList<string> uniqueServers = CatalogPresentation.Deduplicate(merged);
            PublishedSnapshot? currentSnapshot = await GetLatestPublishedSnapshotAsync(
                cancellationToken);
            Dictionary<long, long> currentSourceVersions = currentSnapshot is null
                ? []
                : JsonSerializer.Deserialize<Dictionary<long, long>>(
                    currentSnapshot.SourceVersionsPayload,
                    JsonOptions) ?? [];
            IReadOnlyList<string> currentSourceCatalog = [];
            if (currentSnapshot is not null)
            {
                await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
                currentSourceCatalog = await LoadSourceCatalogSnapshotAsync(
                    connection,
                    currentSourceVersions,
                    cancellationToken);
            }

            if (uniqueServers.Count == 0)
            {
                if (currentSnapshot is not null
                    && currentSnapshot.ServerLinks.Count > 0
                    && now - currentSnapshot.DataAsOfUtc
                        <= TimeSpan.FromHours(_options.ServerCatalogMaxStaleHours))
                {
                    await PublishSnapshotShortAsync(
                        now,
                        currentSnapshot.DataAsOfUtc,
                        PublishedSnapshotStates.Stale,
                        currentSnapshot.ServerLinks,
                        currentSourceVersions,
                        cancellationToken);
                    return new CatalogRefreshResult(
                        PublishedSnapshotStates.Stale,
                        currentSnapshot.ServerLinks.Count,
                        sourceResults);
                }

                await PublishSnapshotShortAsync(
                    now,
                    now,
                    PublishedSnapshotStates.Unavailable,
                    [],
                    sourceVersions,
                    cancellationToken);
                return new CatalogRefreshResult(PublishedSnapshotStates.Unavailable, 0, sourceResults);
            }

            if (CatalogAnomalyGuard.ShouldReject(currentSourceCatalog, uniqueServers))
            {
                await PublishSnapshotShortAsync(
                    now,
                    currentSnapshot!.DataAsOfUtc,
                    PublishedSnapshotStates.Stale,
                    currentSnapshot.ServerLinks,
                    currentSourceVersions,
                    cancellationToken);
                return new CatalogRefreshResult(
                    "anomaly_rejected",
                    currentSnapshot.ServerLinks.Count,
                    sourceResults);
            }

            DateTimeOffset dataAsOf = sourceDataAges.Count == 0 ? now : sourceDataAges.Min();
            string state = dataAsOf == now
                ? PublishedSnapshotStates.Fresh
                : PublishedSnapshotStates.Stale;
            IReadOnlyList<string> publishedServers = CatalogPresentation.Build(
                uniqueServers,
                _options.ServerCatalogMaxServers);
            await PublishSnapshotShortAsync(
                now,
                dataAsOf,
                state,
                publishedServers,
                sourceVersions,
                cancellationToken);
            return new CatalogRefreshResult(
                state,
                publishedServers.Count,
                sourceResults);
        }
        finally
        {
            _catalogRefreshLock.Release();
        }
    }

    public async Task<CatalogStatus> GetCatalogStatusAsync(CancellationToken cancellationToken)
    {
        await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
        PublishedSnapshot? snapshot = await GetLatestPublishedSnapshotAsync(connection, cancellationToken);
        IReadOnlyList<SourceDetails> sources = await ListSourcesAsync(cancellationToken);

        return new CatalogStatus(
            snapshot?.Version,
            snapshot?.CreatedAtUtc,
            snapshot?.DataAsOfUtc,
            snapshot?.State ?? PublishedSnapshotStates.Unavailable,
            snapshot?.ServerLinks.Count ?? 0,
            snapshot?.ContentFingerprint,
            snapshot?.PresentationFingerprint,
            snapshot?.PresentationVersion,
            sources);
    }

    private async Task<long> PersistSourceRefreshSucceededAsync(
        long sourceId,
        SourceReadResult readResult,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await using SqliteTransaction transaction = (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            long version = await InsertSourceSnapshotAsync(connection, transaction, sourceId, readResult, now, cancellationToken);
            await MarkSourceRefreshSucceededAsync(connection, transaction, sourceId, now, cancellationToken);
            await transaction.CommitAsync(cancellationToken);
            return version;
        }
        finally
        {
            _writeLock.Release();
        }
    }

    private async Task MarkSourceRefreshFailedShortAsync(
        long sourceId,
        DateTimeOffset now,
        string errorCode,
        CancellationToken cancellationToken)
    {
        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await MarkSourceRefreshFailedAsync(
                connection,
                transaction,
                sourceId,
                now,
                errorCode,
                cancellationToken);
            await AddAuditAsync(
                connection,
                transaction,
                now,
                "source.refresh_failed",
                null,
                sourceId,
                null,
                errorCode,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    private Task PublishSnapshotShortAsync(
        DateTimeOffset now,
        DateTimeOffset dataAsOfUtc,
        string state,
        IReadOnlyList<string> serverLinks,
        Dictionary<long, long> sourceVersions,
        CancellationToken cancellationToken)
    {
        return PublishSnapshotShortCoreAsync(
            now,
            dataAsOfUtc,
            state,
            serverLinks,
            sourceVersions,
            cancellationToken);
    }

    private async Task PublishSnapshotShortCoreAsync(
        DateTimeOffset now,
        DateTimeOffset dataAsOfUtc,
        string state,
        IReadOnlyList<string> serverLinks,
        Dictionary<long, long> sourceVersions,
        CancellationToken cancellationToken)
    {
        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await PublishSnapshotAsync(
                connection,
                transaction,
                now,
                dataAsOfUtc,
                state,
                serverLinks,
                sourceVersions,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task<MediatorCapacitySnapshot> GetCapacitySnapshotAsync(
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
        const string sql = """
            SELECT
                (
                    SELECT COUNT(*)
                    FROM entitlement_mirrors
                    WHERE status = 'active'
                      AND (valid_until_utc IS NULL OR valid_until_utc > $now_utc)
                ) AS active_subscriptions,
                (
                    SELECT COUNT(*)
                    FROM device_access_tokens d
                    JOIN entitlement_mirrors e ON e.subscription_id = d.subscription_id
                    WHERE d.revoked_at_utc IS NULL
                      AND d.activated_at_utc IS NOT NULL
                      AND e.status = 'active'
                      AND (e.valid_until_utc IS NULL OR e.valid_until_utc > $now_utc)
                ) AS active_devices;
            """;
        await using SqliteCommand command = new(sql, connection);
        command.Parameters.AddWithValue("$now_utc", now.ToString("O"));
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);
        if (!await reader.ReadAsync(cancellationToken))
        {
            return new MediatorCapacitySnapshot(0, 0, now);
        }
        return new MediatorCapacitySnapshot(
            reader.GetInt32(0),
            reader.GetInt32(1),
            now);
    }

    public async Task<ReadinessStatus> GetReadinessStatusAsync(
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            PublishedSnapshot? snapshot = await GetLatestPublishedSnapshotAsync(connection, cancellationToken);
            if (snapshot is null)
            {
                return ReadinessStatus.NotReady("catalog_unavailable", PublishedSnapshotStates.Unavailable, 0, null);
            }

            if (snapshot.State == PublishedSnapshotStates.Unavailable || snapshot.ServerLinks.Count == 0)
            {
                return ReadinessStatus.NotReady("catalog_unavailable", snapshot.State, snapshot.ServerLinks.Count, snapshot.DataAsOfUtc);
            }

            TimeSpan age = now - snapshot.DataAsOfUtc;

            if (age > TimeSpan.FromHours(_options.ServerCatalogMaxStaleHours))
            {
                return ReadinessStatus.NotReady("catalog_too_stale", PublishedSnapshotStates.Unavailable, snapshot.ServerLinks.Count, snapshot.DataAsOfUtc);
            }

            if (snapshot.State == PublishedSnapshotStates.Stale)
            {
                return ReadinessStatus.Degraded(
                    "catalog_stale",
                    snapshot.ServerLinks.Count,
                    snapshot.DataAsOfUtc);
            }

            return ReadinessStatus.Ready(snapshot.ServerLinks.Count, snapshot.DataAsOfUtc);
        }
        catch (SqliteException)
        {
            return ReadinessStatus.NotReady("database_unavailable", PublishedSnapshotStates.Unavailable, 0, null);
        }
    }

    public async Task<PublishedSnapshot?> GetLatestPublishedSnapshotAsync(CancellationToken cancellationToken)
    {
        await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
        return await GetLatestPublishedSnapshotAsync(connection, cancellationToken);
    }

    public async Task<PublishedSnapshot?> GetEffectivePublishedSnapshotAsync(
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
        PublishedSnapshot? snapshot = await GetLatestPublishedSnapshotAsync(connection, cancellationToken);

        if (snapshot is null || snapshot.State == PublishedSnapshotStates.Unavailable)
        {
            return null;
        }

        if (snapshot.ServerLinks.Count == 0)
        {
            return null;
        }

        if (now - snapshot.DataAsOfUtc > TimeSpan.FromHours(_options.ServerCatalogMaxStaleHours))
        {
            return null;
        }

        return snapshot;
    }

    public async Task<PublishedSnapshot?> RollbackPublishedSnapshotAsync(
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);

            const string sql = """
                SELECT version, created_at_utc, data_as_of_utc, server_links_payload, state,
                       source_versions_payload, content_fingerprint, presentation_fingerprint,
                       presentation_version
                FROM published_snapshots
                ORDER BY version DESC
                LIMIT 1 OFFSET 1;
                """;

            PublishedSnapshot? previous;
            await using (SqliteCommand command = new(sql, connection))
            await using (SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken))
            {
                previous = await reader.ReadAsync(cancellationToken)
                    ? ReadPublishedSnapshot(reader)
                    : null;
            }

            if (previous is null)
            {
                return null;
            }

            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await PublishSnapshotAsync(
                connection,
                transaction,
                now,
                previous.DataAsOfUtc,
                previous.State,
                previous.ServerLinks,
                JsonSerializer.Deserialize<Dictionary<long, long>>(previous.SourceVersionsPayload, JsonOptions) ?? [],
                cancellationToken);

            await AddAuditAsync(
                connection,
                transaction,
                now,
                "snapshot.rolled_back",
                null,
                null,
                previous.Version,
                null,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
            return await GetLatestPublishedSnapshotAsync(connection, cancellationToken);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task<int> AppliedMigrationCountAsync(CancellationToken cancellationToken)
    {
        MigrationState state = await GetMigrationStateAsync(cancellationToken);
        return state.DatabaseMaxVersion;
    }

    public async Task<MigrationState> GetMigrationStateAsync(
        CancellationToken cancellationToken)
    {
        await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
        return await GetMigrationStateAsync(connection, cancellationToken);
    }

    private static async Task<MigrationState> GetMigrationStateAsync(
        SqliteConnection connection,
        CancellationToken cancellationToken)
    {
        if (!await TableExistsAsync(connection, "mediator_migrations", cancellationToken))
        {
            return MigrationState.Empty(CurrentMigrationVersion, RequiredMigrationVersions());
        }

        const string sql = "SELECT version FROM mediator_migrations ORDER BY version;";
        List<int> applied = [];
        await using (SqliteCommand command = new(sql, connection))
        await using (SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken))
        {
            while (await reader.ReadAsync(cancellationToken))
            {
                applied.Add(reader.GetInt32(0));
            }
        }

        HashSet<int> appliedSet = applied.ToHashSet();
        int[] missing = RequiredMigrationVersions()
            .Where(version => !appliedSet.Contains(version))
            .ToArray();
        int[] unknown = applied
            .Where(version => version < 1 || version > CurrentMigrationVersion)
            .ToArray();
        int maxVersion = applied.Count == 0 ? 0 : applied.Max();
        bool ahead = maxVersion > CurrentMigrationVersion || unknown.Length > 0;
        bool current = !ahead
            && missing.Length == 0
            && maxVersion == CurrentMigrationVersion;
        return new MigrationState(
            CurrentMigrationVersion,
            maxVersion,
            missing,
            unknown,
            ahead,
            current);
    }

    private static int[] RequiredMigrationVersions()
    {
        return [1, .. Enumerable.Range(3, CurrentMigrationVersion - 2)];
    }

    private static async Task EnsureDatabaseIsNotAheadAsync(
        SqliteConnection connection,
        CancellationToken cancellationToken)
    {
        MigrationState state = await GetMigrationStateAsync(connection, cancellationToken);
        if (state.IsAhead)
        {
            throw new InvalidOperationException(
                $"Database schema version {state.DatabaseMaxVersion} is newer than supported "
                + $"version {state.CurrentBinaryVersion}. Refusing to start or modify the database.");
        }
    }

    private static async Task<bool> TableExistsAsync(
        SqliteConnection connection,
        string tableName,
        CancellationToken cancellationToken)
    {
        const string sql = """
            SELECT COUNT(*)
            FROM sqlite_master
            WHERE type = 'table' AND name = $name;
            """;
        await using SqliteCommand command = new(sql, connection);
        command.Parameters.AddWithValue("$name", tableName);
        return Convert.ToInt64(await command.ExecuteScalarAsync(cancellationToken)) > 0;
    }

    public async Task<EntitlementDetails?> GetEntitlementDetailsAsync(
        Guid publicGuid,
        CancellationToken cancellationToken)
    {
        await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);

        const string sql = """
            SELECT e.version, e.status, e.valid_until_utc, e.max_device_tokens, e.updated_at_utc
            FROM mediated_subscriptions s
            JOIN entitlement_mirrors e ON e.subscription_id = s.id
            WHERE s.public_guid = $public_guid;
            """;

        await using SqliteCommand command = new(sql, connection);
        command.Parameters.AddWithValue("$public_guid", publicGuid.ToString("D"));
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);

        if (!await reader.ReadAsync(cancellationToken))
        {
            return null;
        }

        return new EntitlementDetails(
            publicGuid,
            reader.GetInt64(0),
            reader.GetString(1),
            ReadDate(reader, 2),
            reader.GetInt32(3),
            Parse(reader.GetString(4)));
    }

    public async Task RecordLegacyLinkDeniedAsync(
        Guid publicGuid,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await AddAuditAsync(
                connection,
                null,
                now,
                "legacy_link.denied",
                publicGuid,
                null,
                null,
                "legacy_link_disabled",
                cancellationToken);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    private async Task<SubscriptionRecord> ReadSubscriptionRecordAsync(
        SqliteConnection connection,
        SqliteDataReader reader,
        CancellationToken cancellationToken)
    {
        Guid publicGuid = Guid.Parse(reader.GetString(0));
        int maxDevices = reader.IsDBNull(7) ? _options.DefaultMaxDevices : reader.GetInt32(7);
        string status = reader.IsDBNull(5) ? EntitlementStatuses.Disabled : reader.GetString(5);

        return new SubscriptionRecord
        {
            PublicGuid = publicGuid,
            UpstreamSubscriptionUrl = string.Empty,
            MaxDevices = maxDevices,
            IsActive = string.Equals(status, EntitlementStatuses.Active, StringComparison.Ordinal),
            CustomerName = ReadString(reader, 1),
            Note = ReadString(reader, 2),
            CreatedAtUtc = Parse(reader.GetString(3)),
            ExpiresAtUtc = ReadDate(reader, 6),
            Devices = await ReadDeviceBindingsAsync(connection, publicGuid, cancellationToken)
        };
    }

    private async Task<List<DeviceBindingRecord>> ReadDeviceBindingsAsync(
        SqliteConnection connection,
        Guid publicGuid,
        CancellationToken cancellationToken)
    {
        const string sql = """
            SELECT d.public_id, d.display_name, d.created_at_utc, d.last_used_at_utc,
                   d.revoked_at_utc, d.activated_at_utc
            FROM device_access_tokens d
            JOIN mediated_subscriptions s ON s.id = d.subscription_id
            WHERE s.public_guid = $public_guid
            ORDER BY d.created_at_utc ASC, d.id ASC;
            """;

        await using SqliteCommand command = new(sql, connection);
        command.Parameters.AddWithValue("$public_guid", publicGuid.ToString("D"));
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);
        List<DeviceBindingRecord> devices = [];

        while (await reader.ReadAsync(cancellationToken))
        {
            string publicId = reader.GetString(0);
            Guid bindingId = Guid.TryParse(publicId, out Guid parsed)
                ? parsed
                : Guid.ParseExact(publicId[..32], "N");
            DateTimeOffset created = Parse(reader.GetString(2));

            devices.Add(new DeviceBindingRecord
            {
                DeviceBindingId = bindingId,
                DeviceHash = "device-token",
                DeviceLabel = reader.GetString(1),
                IdentitySource = "device-token",
                IsActive = reader.IsDBNull(4) && !reader.IsDBNull(5),
                FirstSeenAtUtc = created,
                LastSeenAtUtc = ReadDate(reader, 3) ?? created,
                AccessCount = 0
            });
        }

        return devices;
    }

    private async Task ApplyMigrationsAsync(SqliteConnection connection, CancellationToken cancellationToken)
    {
        string[] statements =
        [
            """
            CREATE TABLE IF NOT EXISTS mediator_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at_utc TEXT NOT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS mediated_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_guid TEXT NOT NULL UNIQUE,
                external_request_id TEXT NULL UNIQUE,
                customer_reference TEXT NULL,
                note TEXT NULL,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                legacy_secret_hash TEXT NULL,
                legacy_link_revoked_at_utc TEXT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS entitlement_mirrors (
                subscription_id INTEGER PRIMARY KEY,
                version INTEGER NOT NULL,
                status TEXT NOT NULL,
                valid_until_utc TEXT NULL,
                max_device_tokens INTEGER NOT NULL,
                updated_at_utc TEXT NOT NULL,
                FOREIGN KEY(subscription_id) REFERENCES mediated_subscriptions(id) ON DELETE CASCADE
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS device_access_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL,
                public_id TEXT NOT NULL UNIQUE,
                secret_hash TEXT NOT NULL,
                display_name TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                pending_expires_at_utc TEXT NULL,
                activated_at_utc TEXT NULL,
                last_used_at_utc TEXT NULL,
                revoked_at_utc TEXT NULL,
                revocation_reason TEXT NULL,
                credential_key_id TEXT NULL,
                credential_nonce TEXT NULL,
                credential_ciphertext TEXT NULL,
                credential_tag TEXT NULL,
                first_fetched_at_utc TEXT NULL,
                issuance_key TEXT NULL,
                requested_platform TEXT NULL,
                FOREIGN KEY(subscription_id) REFERENCES mediated_subscriptions(id) ON DELETE CASCADE
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS ix_device_tokens_subscription_active
            ON device_access_tokens(subscription_id, revoked_at_utc);
            """,
            """
            CREATE TABLE IF NOT EXISTS upstream_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                encrypted_endpoint TEXT NOT NULL,
                state TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 100,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                last_tested_at_utc TEXT NULL,
                last_successful_refresh_at_utc TEXT NULL,
                last_failed_refresh_at_utc TEXT NULL,
                last_error_code TEXT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS source_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                version INTEGER NOT NULL,
                created_at_utc TEXT NOT NULL,
                state TEXT NOT NULL,
                server_links_payload TEXT NOT NULL,
                validation_summary TEXT NOT NULL,
                UNIQUE(source_id, version),
                FOREIGN KEY(source_id) REFERENCES upstream_sources(id) ON DELETE CASCADE
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS published_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL UNIQUE,
                created_at_utc TEXT NOT NULL,
                data_as_of_utc TEXT NOT NULL,
                state TEXT NOT NULL,
                server_links_payload TEXT NOT NULL,
                source_versions_payload TEXT NOT NULL,
                content_fingerprint TEXT NULL,
                presentation_fingerprint TEXT NULL,
                presentation_version INTEGER NOT NULL DEFAULT 1
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at_utc TEXT NOT NULL,
                event_type TEXT NOT NULL,
                public_guid TEXT NULL,
                source_id INTEGER NULL,
                snapshot_version INTEGER NULL,
                error_code TEXT NULL
            );
            """
        ];

        foreach (string statement in statements)
        {
            await using SqliteCommand command = new(statement, connection);
            await command.ExecuteNonQueryAsync(cancellationToken);
        }

        const string insertMigration = """
            INSERT OR IGNORE INTO mediator_migrations(version, name, applied_at_utc)
            VALUES(1, 'initial_sqlite_catalog', $applied_at_utc);
            """;
        await using SqliteCommand migrationCommand = new(insertMigration, connection);
        migrationCommand.Parameters.AddWithValue("$applied_at_utc", Format(DateTimeOffset.UtcNow));
        await migrationCommand.ExecuteNonQueryAsync(cancellationToken);

        await ApplyIncrementalMigrationsAsync(connection, cancellationToken);
    }

    private async Task ImportLegacyJsonIfNeededAsync(
        SqliteConnection connection,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(_options.DatabasePath) || !File.Exists(_options.DatabasePath))
        {
            return;
        }

        if (await IsMigrationAppliedAsync(connection, 2, cancellationToken))
        {
            return;
        }

        string backupPath = $"{_options.DatabasePath}.backup.{DateTimeOffset.UtcNow:yyyyMMddHHmmss}";
        File.Copy(_options.DatabasePath, backupPath, overwrite: false);
        string json = await File.ReadAllTextAsync(_options.DatabasePath, cancellationToken);
        VpnMediatorDatabase legacy = JsonSerializer.Deserialize<VpnMediatorDatabase>(json, JsonOptions)
            ?? new VpnMediatorDatabase();

        await using SqliteTransaction transaction = (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
        HashSet<string> importedEndpoints = new(StringComparer.Ordinal);

        foreach (SubscriptionRecord subscription in legacy.Subscriptions)
        {
            SubscriptionIdentity? existing = await FindByPublicGuidAsync(connection, transaction, subscription.PublicGuid, cancellationToken);
            long subscriptionId;

            if (existing is null)
            {
                const string insertSubscription = """
                    INSERT INTO mediated_subscriptions
                        (public_guid, customer_reference, note, created_at_utc, updated_at_utc)
                    VALUES
                        ($public_guid, $customer_reference, $note, $created_at_utc, $updated_at_utc)
                    RETURNING id;
                    """;

                await using SqliteCommand insertCommand = new(insertSubscription, connection, transaction);
                insertCommand.Parameters.AddWithValue("$public_guid", subscription.PublicGuid.ToString("D"));
                insertCommand.Parameters.AddWithValue("$customer_reference", DbValue(subscription.CustomerName));
                insertCommand.Parameters.AddWithValue("$note", DbValue(subscription.Note));
                insertCommand.Parameters.AddWithValue("$created_at_utc", Format(subscription.CreatedAtUtc));
                insertCommand.Parameters.AddWithValue("$updated_at_utc", Format(DateTimeOffset.UtcNow));
                subscriptionId = (long)(await insertCommand.ExecuteScalarAsync(cancellationToken)
                    ?? throw new InvalidOperationException("Legacy subscription insert did not return id."));
            }
            else
            {
                subscriptionId = existing.Id;
            }

            EntitlementUpdateRequest entitlement = new(
                Version: 1,
                Status: subscription.IsActive ? EntitlementStatuses.Active : EntitlementStatuses.Disabled,
                ValidUntilUtc: subscription.ExpiresAtUtc,
                MaxDeviceTokens: subscription.MaxDevices);

            await UpsertEntitlementAsync(connection, transaction, subscriptionId, entitlement, DateTimeOffset.UtcNow, cancellationToken);

            if (!string.IsNullOrWhiteSpace(subscription.UpstreamSubscriptionUrl))
            {
                string endpoint = subscription.UpstreamSubscriptionUrl.Trim();

                if (importedEndpoints.Add(endpoint))
                {
                    await InsertDraftSourceAsync(
                        connection,
                        transaction,
                        endpoint,
                        DateTimeOffset.UtcNow,
                        cancellationToken);
                }
            }
        }

        const string migrationSql = """
            INSERT OR IGNORE INTO mediator_migrations(version, name, applied_at_utc)
            VALUES(2, 'legacy_json_import', $applied_at_utc);
            """;

        await using SqliteCommand migrationCommand = new(migrationSql, connection, transaction);
        migrationCommand.Parameters.AddWithValue("$applied_at_utc", Format(DateTimeOffset.UtcNow));
        await migrationCommand.ExecuteNonQueryAsync(cancellationToken);
        await AddAuditAsync(connection, transaction, DateTimeOffset.UtcNow, "legacy_json.imported", null, null, null, null, cancellationToken);
        await transaction.CommitAsync(cancellationToken);
    }

    private async Task ApplyIncrementalMigrationsAsync(
        SqliteConnection connection,
        CancellationToken cancellationToken)
    {
        if (!await IsMigrationAppliedAsync(connection, 3, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            DateTimeOffset pendingGrace = now.AddMinutes(_options.PendingDeviceTokenTtlMinutes);

            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "pending_expires_at_utc",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "revocation_reason",
                "TEXT NULL",
                cancellationToken);

            await using (SqliteTransaction transaction = (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken))
            {
                await ExecuteNonQueryAsync(
                    connection,
                    transaction,
                    """
                    UPDATE device_access_tokens
                    SET pending_expires_at_utc = $pending_expires_at_utc
                    WHERE activated_at_utc IS NULL
                      AND revoked_at_utc IS NULL
                      AND pending_expires_at_utc IS NULL;
                    """,
                    [
                        new SqliteParameter("$pending_expires_at_utc", Format(pendingGrace))
                    ],
                    cancellationToken);

                await ExecuteNonQueryAsync(
                    connection,
                    transaction,
                    """
                    UPDATE device_access_tokens
                    SET revoked_at_utc = $now,
                        revocation_reason = 'superseded'
                    WHERE activated_at_utc IS NULL
                      AND revoked_at_utc IS NULL
                      AND id NOT IN (
                          SELECT MAX(id)
                          FROM device_access_tokens
                          WHERE activated_at_utc IS NULL AND revoked_at_utc IS NULL
                          GROUP BY subscription_id
                      );
                    """,
                    [
                        new SqliteParameter("$now", Format(now))
                    ],
                    cancellationToken);

                await MarkMigrationAppliedAsync(
                    connection,
                    transaction,
                    3,
                    "device_token_pending_state",
                    now,
                    cancellationToken);
                await transaction.CommitAsync(cancellationToken);
            }
        }

        await ExecuteNonQueryAsync(
            connection,
            null,
            """
            CREATE INDEX IF NOT EXISTS ix_device_tokens_pending
            ON device_access_tokens(subscription_id, pending_expires_at_utc)
            WHERE activated_at_utc IS NULL
              AND revoked_at_utc IS NULL;
            """,
            [],
            cancellationToken);

        if (!await IsMigrationAppliedAsync(connection, 4, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await AddColumnIfMissingAsync(
                connection,
                "published_snapshots",
                "data_as_of_utc",
                "TEXT NULL",
                cancellationToken);

            await using SqliteTransaction transaction = (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await ExecuteNonQueryAsync(
                connection,
                transaction,
                """
                UPDATE published_snapshots
                SET data_as_of_utc = created_at_utc
                WHERE data_as_of_utc IS NULL OR data_as_of_utc = '';
                """,
                [],
                cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                4,
                "published_snapshot_data_as_of",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        if (!await IsMigrationAppliedAsync(connection, 5, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await using SqliteTransaction transaction = (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await ExecuteNonQueryAsync(
                connection,
                transaction,
                """
                UPDATE mediated_subscriptions
                SET legacy_link_revoked_at_utc = COALESCE(legacy_link_revoked_at_utc, $now);
                """,
                [
                    new SqliteParameter("$now", Format(now))
                ],
                cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                5,
                "legacy_subscription_links_revoked",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        if (!await IsMigrationAppliedAsync(connection, 6, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "device_type",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "platform",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "detected_model",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "detection_source",
                "TEXT NULL",
                cancellationToken);

            await using SqliteTransaction transaction = (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                6,
                "device_display_metadata",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        if (!await IsMigrationAppliedAsync(connection, 7, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await using SqliteTransaction transaction = (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await ExecuteNonQueryAsync(
                connection,
                transaction,
                """
                CREATE TABLE IF NOT EXISTS connection_handoff_claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_id TEXT NOT NULL UNIQUE,
                    subscription_id INTEGER NOT NULL,
                    preferred_platform TEXT NULL,
                    secret_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at_utc TEXT NOT NULL,
                    redeemed_at_utc TEXT NULL,
                    device_public_id TEXT NULL,
                    created_at_utc TEXT NOT NULL,
                    failure_code TEXT NULL,
                    FOREIGN KEY(subscription_id) REFERENCES mediated_subscriptions(id) ON DELETE CASCADE
                );
                """,
                [],
                cancellationToken);
            await ExecuteNonQueryAsync(
                connection,
                transaction,
                """
                CREATE INDEX IF NOT EXISTS ix_handoff_claims_status_expires
                ON connection_handoff_claims(status, expires_at_utc);
                """,
                [],
                cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                7,
                "connection_handoff_claims",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        if (!await IsMigrationAppliedAsync(connection, 8, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await AddColumnIfMissingAsync(
                connection,
                "connection_handoff_claims",
                "failed_attempt_count",
                "INTEGER NOT NULL DEFAULT 0",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "connection_handoff_claims",
                "last_failed_attempt_at_utc",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "connection_handoff_claims",
                "locked_until_utc",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "connection_handoff_claims",
                "superseded_at_utc",
                "TEXT NULL",
                cancellationToken);

            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                8,
                "handoff_claim_abuse_protection",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        if (!await IsMigrationAppliedAsync(connection, 9, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "credential_key_id",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "credential_nonce",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "credential_ciphertext",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "credential_tag",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "first_fetched_at_utc",
                "TEXT NULL",
                cancellationToken);

            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await ExecuteNonQueryAsync(
                connection,
                transaction,
                "DROP INDEX IF EXISTS ux_device_tokens_single_pending;",
                [],
                cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                9,
                "encrypted_device_credentials",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        if (!await IsMigrationAppliedAsync(connection, 10, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await AddColumnIfMissingAsync(
                connection,
                "published_snapshots",
                "content_fingerprint",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "published_snapshots",
                "presentation_fingerprint",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "published_snapshots",
                "presentation_version",
                "INTEGER NOT NULL DEFAULT 1",
                cancellationToken);

            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                10,
                "catalog_content_presentation_fingerprints",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        if (!await IsMigrationAppliedAsync(connection, 11, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await ExecuteNonQueryAsync(
                connection,
                transaction,
                """
                CREATE TABLE IF NOT EXISTS device_access_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_token_id INTEGER NOT NULL UNIQUE,
                    provider_profile_id TEXT NULL,
                    credential_fingerprint TEXT NULL,
                    state TEXT NOT NULL,
                    credential_key_id TEXT NULL,
                    credential_nonce TEXT NULL,
                    credential_ciphertext TEXT NULL,
                    credential_tag TEXT NULL,
                    last_error_code TEXT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    revoked_at_utc TEXT NULL,
                    FOREIGN KEY(device_token_id) REFERENCES device_access_tokens(id) ON DELETE CASCADE
                );
                """,
                [],
                cancellationToken);
            await ExecuteNonQueryAsync(
                connection,
                transaction,
                """
                CREATE INDEX IF NOT EXISTS ix_device_access_profiles_state
                ON device_access_profiles(state, updated_at_utc);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_device_access_profiles_provider_profile
                ON device_access_profiles(provider_profile_id)
                WHERE provider_profile_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS device_access_profile_credentials (
                    device_access_profile_id INTEGER NOT NULL,
                    credential_fingerprint TEXT NOT NULL UNIQUE,
                    PRIMARY KEY(device_access_profile_id, credential_fingerprint),
                    FOREIGN KEY(device_access_profile_id) REFERENCES device_access_profiles(id) ON DELETE CASCADE
                );
                """,
                [],
                cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                11,
                "managed_device_access_profiles",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        if (!await IsMigrationAppliedAsync(connection, 12, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await AddColumnIfMissingAsync(
                connection,
                "device_access_profiles",
                "provisioned_valid_until_utc",
                "TEXT NULL",
                cancellationToken);
            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                12,
                "managed_profile_entitlement_sync",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        if (!await IsMigrationAppliedAsync(connection, 13, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "issuance_key",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "requested_platform",
                "TEXT NULL",
                cancellationToken);
            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await ExecuteNonQueryAsync(
                connection,
                transaction,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_device_tokens_subscription_issuance
                ON device_access_tokens(subscription_id, issuance_key)
                WHERE issuance_key IS NOT NULL;
                """,
                [],
                cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                13,
                "device_issuance_identity",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        if (!await IsMigrationAppliedAsync(connection, 14, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await ExecuteNonQueryAsync(
                connection,
                transaction,
                """
                CREATE TABLE IF NOT EXISTS server_health_states (
                    server_fingerprint TEXT PRIMARY KEY,
                    protocol TEXT NOT NULL,
                    state TEXT NOT NULL,
                    consecutive_successes INTEGER NOT NULL,
                    consecutive_failures INTEGER NOT NULL,
                    last_seen_at_utc TEXT NOT NULL,
                    last_checked_at_utc TEXT NULL,
                    last_success_at_utc TEXT NULL,
                    last_failure_at_utc TEXT NULL,
                    last_latency_ms INTEGER NULL,
                    last_error_code TEXT NULL,
                    probe_policy_version TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_server_health_state_updated
                ON server_health_states(state, updated_at_utc);
                """,
                [],
                cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                14,
                "server_health_states",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        if (!await IsMigrationAppliedAsync(connection, 15, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await ExecuteNonQueryAsync(
                connection,
                transaction,
                """
                CREATE TABLE IF NOT EXISTS entitlement_operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL UNIQUE,
                    subscription_id INTEGER NOT NULL,
                    operation_type TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    expected_version INTEGER NOT NULL,
                    result_version INTEGER NOT NULL,
                    result_status TEXT NOT NULL,
                    result_valid_until_utc TEXT NULL,
                    result_max_device_tokens INTEGER NOT NULL,
                    applied_at_utc TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    FOREIGN KEY(subscription_id) REFERENCES mediated_subscriptions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS ix_entitlement_operations_subscription
                ON entitlement_operations(subscription_id, applied_at_utc);
                """,
                [],
                cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                15,
                "durable_entitlement_operations",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        if (!await IsMigrationAppliedAsync(connection, 16, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await ExecuteNonQueryAsync(
                connection,
                transaction,
                """
                CREATE TABLE IF NOT EXISTS published_snapshot_health_metadata (
                    snapshot_version INTEGER PRIMARY KEY,
                    source_server_count INTEGER NOT NULL,
                    healthy_server_count INTEGER NOT NULL,
                    suspected_server_count INTEGER NOT NULL,
                    unhealthy_server_count INTEGER NOT NULL,
                    unknown_server_count INTEGER NOT NULL,
                    removed_server_count INTEGER NOT NULL,
                    fallback_active INTEGER NOT NULL,
                    fallback_reason TEXT NULL,
                    health_evaluated_at_utc TEXT NOT NULL,
                    FOREIGN KEY(snapshot_version) REFERENCES published_snapshots(version)
                        ON DELETE CASCADE
                );
                """,
                [],
                cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                16,
                "published_catalog_health_metadata",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        if (!await IsMigrationAppliedAsync(connection, 17, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await ExecuteNonQueryAsync(
                connection,
                transaction,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_entitlement_operations_subscription_result_version
                ON entitlement_operations(subscription_id, result_version);
                """,
                [],
                cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                17,
                "entitlement_operation_result_provenance",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }


        if (!await IsMigrationAppliedAsync(connection, 18, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "feed_policy_version",
                "INTEGER NOT NULL DEFAULT 1",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "feed_policy_mode",
                "TEXT NOT NULL DEFAULT 'legacy'",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "binding_state",
                "TEXT NOT NULL DEFAULT 'grandfathered'",
                cancellationToken);
            await AddColumnIfMissingAsync(connection, "device_access_tokens", "bound_platform", "TEXT NULL", cancellationToken);
            await AddColumnIfMissingAsync(connection, "device_access_tokens", "bound_client_family", "TEXT NULL", cancellationToken);
            await AddColumnIfMissingAsync(connection, "device_access_tokens", "bound_at_utc", "TEXT NULL", cancellationToken);
            await AddColumnIfMissingAsync(connection, "device_access_tokens", "last_network_fingerprint", "TEXT NULL", cancellationToken);
            await AddColumnIfMissingAsync(connection, "device_access_tokens", "last_network_changed_at_utc", "TEXT NULL", cancellationToken);
            await AddColumnIfMissingAsync(connection, "device_access_tokens", "last_policy_event_at_utc", "TEXT NULL", cancellationToken);
            await AddColumnIfMissingAsync(connection, "device_access_tokens", "last_transfer_at_utc", "TEXT NULL", cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "transfer_count",
                "INTEGER NOT NULL DEFAULT 0",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "risk_score",
                "INTEGER NOT NULL DEFAULT 0",
                cancellationToken);

            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await ExecuteNonQueryAsync(
                connection,
                transaction,
                """
                CREATE TABLE IF NOT EXISTS device_access_sightings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_token_id INTEGER NOT NULL,
                    network_fingerprint TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    client_family TEXT NOT NULL,
                    first_seen_at_utc TEXT NOT NULL,
                    last_seen_at_utc TEXT NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(device_token_id, network_fingerprint, platform, client_family),
                    FOREIGN KEY(device_token_id) REFERENCES device_access_tokens(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS ix_device_access_sightings_recent
                ON device_access_sightings(device_token_id, last_seen_at_utc);

                CREATE TABLE IF NOT EXISTS device_feed_policy_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_token_id INTEGER NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    reason_code TEXT NULL,
                    observed_platform TEXT NULL,
                    expected_platform TEXT NULL,
                    decision TEXT NOT NULL,
                    FOREIGN KEY(device_token_id) REFERENCES device_access_tokens(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS ix_device_feed_policy_events_device_created
                ON device_feed_policy_events(device_token_id, created_at_utc);

                CREATE TABLE IF NOT EXISTS device_feed_transfer_operations (
                    operation_id TEXT PRIMARY KEY,
                    subscription_id INTEGER NOT NULL,
                    source_device_token_id INTEGER NOT NULL,
                    result_device_token_id INTEGER NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    applied_at_utc TEXT NOT NULL,
                    FOREIGN KEY(subscription_id) REFERENCES mediated_subscriptions(id) ON DELETE CASCADE,
                    FOREIGN KEY(source_device_token_id) REFERENCES device_access_tokens(id),
                    FOREIGN KEY(result_device_token_id) REFERENCES device_access_tokens(id)
                );
                """,
                [],
                cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                18,
                "device_feed_binding_policy",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        if (!await IsMigrationAppliedAsync(connection, 19, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "issuance_request_hash",
                "TEXT NULL",
                cancellationToken);

            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                19,
                "device_issuance_policy_contract_v2",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        if (!await IsMigrationAppliedAsync(connection, 20, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "bound_identity_hash",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "bound_identity_key_id",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "bound_identity_source",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "last_identity_seen_at_utc",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "last_identity_mismatch_at_utc",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_feed_policy_events",
                "identity_source",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_feed_policy_events",
                "identity_present",
                "INTEGER NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_feed_policy_events",
                "identity_match",
                "INTEGER NULL",
                cancellationToken);

            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                20,
                "device_hwid_identity_binding",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        if (!await IsMigrationAppliedAsync(connection, 21, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "access_channel",
                "TEXT NOT NULL DEFAULT 'device_link'",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "enrollment_intent_id",
                "INTEGER NULL",
                cancellationToken);

            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await ExecuteNonQueryAsync(
                connection,
                transaction,
                """
                CREATE TABLE IF NOT EXISTS subscription_feed_credentials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    secret_hash TEXT NOT NULL,
                    credential_key_id TEXT NOT NULL,
                    credential_nonce TEXT NOT NULL,
                    credential_ciphertext TEXT NOT NULL,
                    credential_tag TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    revoked_at_utc TEXT NULL,
                    revocation_reason TEXT NULL,
                    FOREIGN KEY(subscription_id) REFERENCES mediated_subscriptions(id) ON DELETE CASCADE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_subscription_feed_credentials_active
                ON subscription_feed_credentials(subscription_id)
                WHERE revoked_at_utc IS NULL;

                CREATE TABLE IF NOT EXISTS device_enrollment_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_id TEXT NOT NULL UNIQUE,
                    subscription_id INTEGER NOT NULL,
                    operation_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    requested_platform TEXT NULL,
                    state TEXT NOT NULL,
                    expires_at_utc TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    claimed_at_utc TEXT NULL,
                    claimed_device_token_id INTEGER NULL,
                    cancelled_at_utc TEXT NULL,
                    FOREIGN KEY(subscription_id) REFERENCES mediated_subscriptions(id) ON DELETE CASCADE,
                    FOREIGN KEY(claimed_device_token_id) REFERENCES device_access_tokens(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_device_enrollment_operation
                ON device_enrollment_intents(subscription_id, operation_id);
                CREATE INDEX IF NOT EXISTS ix_device_enrollment_pending
                ON device_enrollment_intents(subscription_id, state, expires_at_utc);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_unified_device_identity
                ON device_access_tokens(subscription_id, bound_identity_hash)
                WHERE revoked_at_utc IS NULL
                  AND bound_identity_hash IS NOT NULL
                  AND access_channel = 'unified_feed';
                """,
                [],
                cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                21,
                "unified_hwid_subscription_feed",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        if (!await IsMigrationAppliedAsync(connection, 22, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "device_state",
                "TEXT NOT NULL DEFAULT 'legacy'",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "device_access_tokens",
                "provisioning_expires_at_utc",
                "TEXT NULL",
                cancellationToken);

            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await ExecuteNonQueryAsync(
                connection,
                transaction,
                """
                DROP INDEX IF EXISTS ux_unified_device_identity;
                CREATE UNIQUE INDEX ux_unified_device_identity
                ON device_access_tokens(subscription_id, bound_identity_hash)
                WHERE bound_identity_hash IS NOT NULL
                  AND access_channel = 'unified_feed';
                CREATE INDEX IF NOT EXISTS ix_unified_device_state
                ON device_access_tokens(subscription_id, access_channel, device_state, revoked_at_utc);

                UPDATE device_access_tokens
                SET device_state = CASE
                    WHEN revoked_at_utc IS NOT NULL THEN 'disabled'
                    WHEN activated_at_utc IS NOT NULL THEN 'active'
                    ELSE 'provisioning'
                END
                WHERE access_channel = 'unified_feed'
                  AND device_state = 'legacy';
                """,
                [],
                cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                22,
                "unified_subscription_feed_runtime",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        if (!await IsMigrationAppliedAsync(connection, 23, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await AddColumnIfMissingAsync(
                connection,
                "server_health_states",
                "last_success_request_latency_ms",
                "INTEGER NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "server_health_states",
                "smoothed_request_latency_ms",
                "REAL NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "server_health_states",
                "latency_sample_count",
                "INTEGER NOT NULL DEFAULT 0",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "server_health_states",
                "latency_updated_at_utc",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "server_health_states",
                "last_probe_duration_ms",
                "INTEGER NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "server_health_states",
                "last_tunnel_setup_ms",
                "INTEGER NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "server_health_states",
                "last_failure_duration_ms",
                "INTEGER NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "server_health_states",
                "unsupported_reason",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "server_health_states",
                "probe_agent_version",
                "TEXT NOT NULL DEFAULT 'legacy'",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "server_health_states",
                "probe_point_id",
                "TEXT NOT NULL DEFAULT 'central-vps'",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "server_health_states",
                "recovery_in_progress",
                "INTEGER NOT NULL DEFAULT 0",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "server_health_states",
                "last_probe_outcome",
                "TEXT NOT NULL DEFAULT 'unknown'",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "server_health_states",
                "last_infrastructure_error_code",
                "TEXT NULL",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "published_snapshot_health_metadata",
                "filtering_mode",
                "TEXT NOT NULL DEFAULT 'legacy'",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "published_snapshot_health_metadata",
                "candidate_server_count",
                "INTEGER NOT NULL DEFAULT 0",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "published_snapshot_health_metadata",
                "degraded_server_count",
                "INTEGER NOT NULL DEFAULT 0",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "published_snapshot_health_metadata",
                "quarantined_server_count",
                "INTEGER NOT NULL DEFAULT 0",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "published_snapshot_health_metadata",
                "unsupported_server_count",
                "INTEGER NOT NULL DEFAULT 0",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "published_snapshot_health_metadata",
                "eligible_server_count",
                "INTEGER NOT NULL DEFAULT 0",
                cancellationToken);
            await AddColumnIfMissingAsync(
                connection,
                "published_snapshot_health_metadata",
                "ranking_enabled",
                "INTEGER NOT NULL DEFAULT 0",
                cancellationToken);

            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await ExecuteNonQueryAsync(
                connection,
                transaction,
                """
                UPDATE server_health_states
                SET last_success_request_latency_ms = last_latency_ms,
                    smoothed_request_latency_ms = last_latency_ms,
                    latency_sample_count = CASE WHEN last_latency_ms IS NULL THEN 0 ELSE 1 END,
                    latency_updated_at_utc = CASE
                        WHEN last_latency_ms IS NULL THEN NULL
                        ELSE COALESCE(last_success_at_utc, last_checked_at_utc)
                    END
                WHERE last_success_request_latency_ms IS NULL
                  AND last_latency_ms IS NOT NULL
                  AND state IN ('healthy', 'suspected', 'degraded');

                CREATE TABLE IF NOT EXISTS server_probe_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_fingerprint TEXT NOT NULL,
                    protocol TEXT NOT NULL,
                    probe_point_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    core_startup_ms INTEGER NULL,
                    tunnel_setup_ms INTEGER NULL,
                    request_latency_ms INTEGER NULL,
                    total_duration_ms INTEGER NULL,
                    successful_measurements INTEGER NOT NULL,
                    attempted_measurements INTEGER NOT NULL,
                    error_code TEXT NULL,
                    probe_agent_version TEXT NOT NULL,
                    probe_policy_version TEXT NOT NULL,
                    observed_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_server_probe_observations_fingerprint_time
                ON server_probe_observations(server_fingerprint, observed_at_utc DESC);
                CREATE INDEX IF NOT EXISTS ix_server_probe_observations_time
                ON server_probe_observations(observed_at_utc);

                CREATE TABLE IF NOT EXISTS server_health_safe_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_utc TEXT NOT NULL,
                    server_links_payload TEXT NOT NULL,
                    policy_version TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_server_health_safe_snapshots_created
                ON server_health_safe_snapshots(created_at_utc DESC);
                """,
                [],
                cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                23,
                "server_health_observations_ranking_and_safe_snapshot",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        if (!await IsMigrationAppliedAsync(connection, 24, cancellationToken))
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            await using SqliteTransaction transaction =
                (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
            await ExecuteNonQueryAsync(
                connection,
                transaction,
                """
                INSERT INTO entitlement_operations(
                    operation_id, subscription_id, operation_type, request_fingerprint,
                    expected_version, result_version, result_status,
                    result_valid_until_utc, result_max_device_tokens,
                    applied_at_utc, created_at_utc)
                SELECT
                    'legacy-snapshot:' || replace(s.public_guid, '-', '') || ':v' || e.version,
                    e.subscription_id,
                    'legacy_snapshot_import',
                    'legacy_snapshot_import:' || s.public_guid || ':' || e.version,
                    CASE WHEN e.version > 1 THEN e.version - 1 ELSE 0 END,
                    e.version,
                    e.status,
                    e.valid_until_utc,
                    e.max_device_tokens,
                    e.updated_at_utc,
                    e.updated_at_utc
                FROM entitlement_mirrors e
                JOIN mediated_subscriptions s ON s.id = e.subscription_id
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM entitlement_operations o
                    WHERE o.subscription_id = e.subscription_id
                      AND o.result_version = e.version
                );
                """,
                [],
                cancellationToken);
            await MarkMigrationAppliedAsync(
                connection,
                transaction,
                24,
                "entitlement_snapshot_provenance_backfill",
                now,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }
    }

    private static async Task AddColumnIfMissingAsync(
        SqliteConnection connection,
        string table,
        string column,
        string definition,
        CancellationToken cancellationToken)
    {
        await using SqliteCommand tableInfo = new($"PRAGMA table_info({table});", connection);
        await using SqliteDataReader reader = await tableInfo.ExecuteReaderAsync(cancellationToken);

        while (await reader.ReadAsync(cancellationToken))
        {
            if (string.Equals(reader.GetString(1), column, StringComparison.OrdinalIgnoreCase))
            {
                return;
            }
        }

        await reader.DisposeAsync();
        await using SqliteCommand alter = new($"ALTER TABLE {table} ADD COLUMN {column} {definition};", connection);
        await alter.ExecuteNonQueryAsync(cancellationToken);
    }

    private static async Task MarkMigrationAppliedAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        int version,
        string name,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await ExecuteNonQueryAsync(
            connection,
            transaction,
            """
            INSERT OR IGNORE INTO mediator_migrations(version, name, applied_at_utc)
            VALUES($version, $name, $applied_at_utc);
            """,
            [
                new SqliteParameter("$version", version),
                new SqliteParameter("$name", name),
                new SqliteParameter("$applied_at_utc", Format(now))
            ],
            cancellationToken);
    }

    private static async Task ExecuteNonQueryAsync(
        SqliteConnection connection,
        SqliteTransaction? transaction,
        string sql,
        IReadOnlyList<SqliteParameter> parameters,
        CancellationToken cancellationToken)
    {
        await using SqliteCommand command = new(sql, connection, transaction);

        foreach (SqliteParameter parameter in parameters)
        {
            command.Parameters.Add(parameter);
        }

        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private async Task InsertDraftSourceAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        string endpoint,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        string protectedEndpoint = _endpointProtector.Protect(endpoint.Trim());

        const string insertSql = """
            INSERT INTO upstream_sources
                (name, kind, encrypted_endpoint, state, sort_order, created_at_utc, updated_at_utc)
            VALUES
                ($name, $kind, $endpoint, $state, $sort_order, $created_at_utc, $updated_at_utc);
            """;
        await using SqliteCommand insertCommand = new(insertSql, connection, transaction);
        insertCommand.Parameters.AddWithValue("$name", "legacy imported source");
        insertCommand.Parameters.AddWithValue("$kind", SourceKinds.SubscriptionUrl);
        insertCommand.Parameters.AddWithValue("$endpoint", protectedEndpoint);
        insertCommand.Parameters.AddWithValue("$state", SourceStates.Draft);
        insertCommand.Parameters.AddWithValue("$sort_order", 100);
        insertCommand.Parameters.AddWithValue("$created_at_utc", Format(now));
        insertCommand.Parameters.AddWithValue("$updated_at_utc", Format(now));
        await insertCommand.ExecuteNonQueryAsync(cancellationToken);
    }

    private static async Task ConfigureDatabaseConcurrencyAsync(
        SqliteConnection connection,
        CancellationToken cancellationToken)
    {
        await using SqliteCommand command = new(
            "PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL;",
            connection);
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private async Task<bool> IsMigrationAppliedAsync(
        SqliteConnection connection,
        int version,
        CancellationToken cancellationToken)
    {
        const string sql = "SELECT COUNT(*) FROM mediator_migrations WHERE version = $version;";
        await using SqliteCommand command = new(sql, connection);
        command.Parameters.AddWithValue("$version", version);
        long count = (long)(await command.ExecuteScalarAsync(cancellationToken) ?? 0L);
        return count > 0;
    }

    private async Task<SqliteConnection> OpenConnectionAsync(CancellationToken cancellationToken)
    {
        SqliteConnection connection = new($"Data Source={_options.SqliteDatabasePath}");
        await connection.OpenAsync(cancellationToken);

        await using SqliteCommand pragma = new(
            "PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 5000;",
            connection);
        await pragma.ExecuteNonQueryAsync(cancellationToken);

        return connection;
    }

    private static async Task AddAuditAsync(
        SqliteConnection connection,
        SqliteTransaction? transaction,
        DateTimeOffset now,
        string eventType,
        Guid? publicGuid,
        long? sourceId,
        long? snapshotVersion,
        string? errorCode,
        CancellationToken cancellationToken)
    {
        const string sql = """
            INSERT INTO audit_events
                (created_at_utc, event_type, public_guid, source_id, snapshot_version, error_code)
            VALUES
                ($created_at_utc, $event_type, $public_guid, $source_id, $snapshot_version, $error_code);
            """;

        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$created_at_utc", Format(now));
        command.Parameters.AddWithValue("$event_type", eventType);
        command.Parameters.AddWithValue("$public_guid", DbValue(publicGuid?.ToString("D")));
        command.Parameters.AddWithValue("$source_id", DbValue(sourceId));
        command.Parameters.AddWithValue("$snapshot_version", DbValue(snapshotVersion));
        command.Parameters.AddWithValue("$error_code", DbValue(errorCode));
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private static void ValidateEntitlementOperation(EntitlementOperationRequest request)
    {
        if (string.IsNullOrWhiteSpace(request.OperationId) || request.OperationId.Length > 128)
        {
            throw new InvalidOperationException("Entitlement operation id is invalid.");
        }

        if (!Regex.IsMatch(request.OperationId, "^[A-Za-z0-9:_-]+$", RegexOptions.CultureInvariant))
        {
            throw new InvalidOperationException("Entitlement operation id contains invalid characters.");
        }

        if (string.IsNullOrWhiteSpace(request.OperationType) || request.OperationType.Length > 32)
        {
            throw new InvalidOperationException("Entitlement operation type is invalid.");
        }

        if (request.ExpectedVersion <= 0)
        {
            throw new InvalidOperationException("Expected entitlement version must be positive.");
        }

        ValidateEntitlement(new EntitlementUpdateRequest(
            request.ExpectedVersion + 1,
            request.Status,
            request.ValidUntilUtc,
            request.MaxDeviceTokens));
    }

    private static string ComputeEntitlementOperationFingerprint(
        Guid publicGuid,
        EntitlementOperationRequest request)
    {
        return ComputeEntitlementFingerprint(
            publicGuid,
            request.OperationType,
            request.ExpectedVersion,
            request.Status,
            request.ValidUntilUtc,
            request.MaxDeviceTokens);
    }

    private static string ComputeEntitlementFingerprint(
        Guid publicGuid,
        string operationType,
        long expectedVersion,
        string status,
        DateTimeOffset? validUntilUtc,
        int maxDeviceTokens)
    {
        string canonical = string.Join(
            "\n",
            publicGuid.ToString("D"),
            operationType.Trim(),
            expectedVersion.ToString(CultureInfo.InvariantCulture),
            status.Trim(),
            validUntilUtc?.ToUniversalTime().ToString("O", CultureInfo.InvariantCulture)
                ?? string.Empty,
            maxDeviceTokens.ToString(CultureInfo.InvariantCulture));
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(canonical)))
            .ToLowerInvariant();
    }

    private static async Task InsertEntitlementOperationAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long subscriptionId,
        string operationId,
        string operationType,
        string requestFingerprint,
        long expectedVersion,
        EntitlementUpdateRequest result,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        const string sql = """
            INSERT INTO entitlement_operations(
                operation_id, subscription_id, operation_type, request_fingerprint,
                expected_version, result_version, result_status,
                result_valid_until_utc, result_max_device_tokens,
                applied_at_utc, created_at_utc)
            VALUES(
                $operation_id, $subscription_id, $operation_type, $request_fingerprint,
                $expected_version, $result_version, $result_status,
                $result_valid_until_utc, $result_max_device_tokens,
                $applied_at_utc, $created_at_utc);
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$operation_id", operationId);
        command.Parameters.AddWithValue("$subscription_id", subscriptionId);
        command.Parameters.AddWithValue("$operation_type", operationType);
        command.Parameters.AddWithValue("$request_fingerprint", requestFingerprint);
        command.Parameters.AddWithValue("$expected_version", expectedVersion);
        command.Parameters.AddWithValue("$result_version", result.Version);
        command.Parameters.AddWithValue("$result_status", result.Status);
        command.Parameters.AddWithValue(
            "$result_valid_until_utc",
            result.ValidUntilUtc is null ? DBNull.Value : Format(result.ValidUntilUtc.Value));
        command.Parameters.AddWithValue("$result_max_device_tokens", result.MaxDeviceTokens);
        command.Parameters.AddWithValue("$applied_at_utc", Format(now));
        command.Parameters.AddWithValue("$created_at_utc", Format(now));
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private static async Task<EntitlementOperationResult?> GetEntitlementOperationAsync(
        SqliteConnection connection,
        SqliteTransaction? transaction,
        string operationId,
        CancellationToken cancellationToken)
    {
        const string sql = """
            SELECT o.operation_id, o.request_fingerprint, s.public_guid,
                   o.operation_type, o.expected_version, o.result_version,
                   o.result_status, o.result_valid_until_utc,
                   o.result_max_device_tokens, o.applied_at_utc
            FROM entitlement_operations o
            JOIN mediated_subscriptions s ON s.id = o.subscription_id
            WHERE o.operation_id = $operation_id;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$operation_id", operationId);
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);
        return await ReadEntitlementOperationAsync(reader, cancellationToken);
    }

    private static async Task<EntitlementOperationResult?> ReadEntitlementOperationAsync(
        SqliteDataReader reader,
        CancellationToken cancellationToken)
    {
        if (!await reader.ReadAsync(cancellationToken))
        {
            return null;
        }

        return new EntitlementOperationResult(
            EntitlementOperationStatus.Applied,
            reader.GetString(0),
            reader.GetString(1),
            Guid.Parse(reader.GetString(2)),
            reader.GetString(3),
            reader.GetInt64(4),
            reader.GetInt64(5),
            reader.GetString(6),
            ReadDate(reader, 7),
            reader.GetInt32(8),
            Parse(reader.GetString(9)),
            0);
    }

    private static void ValidateEntitlement(EntitlementUpdateRequest request)
    {
        if (request.Version < 1)
        {
            throw new InvalidOperationException("Entitlement version must be positive.");
        }

        if (!EntitlementStatuses.All.Contains(request.Status, StringComparer.Ordinal))
        {
            throw new InvalidOperationException("Entitlement status is invalid.");
        }

        if (request.MaxDeviceTokens < 1 || request.MaxDeviceTokens > 100)
        {
            throw new InvalidOperationException("MaxDeviceTokens must be between 1 and 100.");
        }
    }

    private static object DbValue(object? value)
    {
        return value ?? DBNull.Value;
    }

    private static string Format(DateTimeOffset value)
    {
        return value.ToUniversalTime().ToString("O");
    }

    private static DateTimeOffset Parse(string value)
    {
        return DateTimeOffset.Parse(value, null, System.Globalization.DateTimeStyles.RoundtripKind);
    }

    private static string? ReadString(SqliteDataReader reader, int index)
    {
        return reader.IsDBNull(index) ? null : reader.GetString(index);
    }

    private static DateTimeOffset? ReadDate(SqliteDataReader reader, int index)
    {
        return reader.IsDBNull(index) ? null : Parse(reader.GetString(index));
    }

    private static SourceDetails ReadSourceDetails(SqliteDataReader reader)
    {
        return new SourceDetails(
            Id: reader.GetInt64(0),
            Name: reader.GetString(1),
            Kind: reader.GetString(2),
            State: reader.GetString(3),
            SortOrder: reader.GetInt32(4),
            LastTestedAtUtc: ReadDate(reader, 5),
            LastSuccessfulRefreshAtUtc: ReadDate(reader, 6),
            LastFailedRefreshAtUtc: ReadDate(reader, 7),
            LastErrorCode: ReadString(reader, 8));
    }

    private static PublishedSnapshot ReadPublishedSnapshot(SqliteDataReader reader)
    {
        string payload = reader.GetString(3);
        IReadOnlyList<string> serverLinks = JsonSerializer.Deserialize<List<string>>(payload, JsonOptions) ?? [];

        return new PublishedSnapshot(
            Version: reader.GetInt64(0),
            CreatedAtUtc: Parse(reader.GetString(1)),
            DataAsOfUtc: Parse(reader.GetString(2)),
            State: reader.GetString(4),
            ServerLinks: serverLinks,
            SourceVersionsPayload: reader.GetString(5),
            ContentFingerprint: ReadString(reader, 6),
            PresentationFingerprint: ReadString(reader, 7),
            PresentationVersion: reader.IsDBNull(8) ? 1 : reader.GetInt32(8));
    }

    private async Task<SubscriptionRuntimeState?> GetRuntimeStateAsync(
        Guid publicGuid,
        CancellationToken cancellationToken)
    {
        await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);

        const string sql = """
            SELECT e.version, e.status, e.valid_until_utc, e.max_device_tokens
            FROM mediated_subscriptions s
            JOIN entitlement_mirrors e ON e.subscription_id = s.id
            WHERE s.public_guid = $public_guid;
            """;

        await using SqliteCommand command = new(sql, connection);
        command.Parameters.AddWithValue("$public_guid", publicGuid.ToString("D"));
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);

        if (!await reader.ReadAsync(cancellationToken))
        {
            return null;
        }

        return new SubscriptionRuntimeState(
            Version: reader.GetInt64(0),
            Status: reader.GetString(1),
            ValidUntilUtc: ReadDate(reader, 2),
            MaxDeviceTokens: reader.GetInt32(3));
    }

    private async Task<bool> IsLegacyLinkRevokedAsync(
        Guid publicGuid,
        CancellationToken cancellationToken)
    {
        await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);

        const string sql = """
            SELECT legacy_link_revoked_at_utc
            FROM mediated_subscriptions
            WHERE public_guid = $public_guid;
            """;
        await using SqliteCommand command = new(sql, connection);
        command.Parameters.AddWithValue("$public_guid", publicGuid.ToString("D"));
        object? value = await command.ExecuteScalarAsync(cancellationToken);
        return value is not null && value != DBNull.Value;
    }

    private string GetDeviceTokenHashKey()
    {
        string? configured = TextSanitizer.NullIfWhiteSpace(_options.DeviceTokenHashKey);

        if (configured is not null)
        {
            return configured;
        }

        string? legacy = TextSanitizer.NullIfWhiteSpace(_options.LinkSigningSecret);

        if (legacy is not null)
        {
            return legacy;
        }

        throw new InvalidOperationException("VpnMediator:DeviceTokenHashKey is required.");
    }

    private static async Task<ExistingDeviceCredential?> GetDeviceCredentialByIssuanceKeyAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long subscriptionId,
        string issuanceKey,
        CancellationToken cancellationToken)
    {
        const string sql = """
            SELECT id, public_id, display_name, pending_expires_at_utc, revoked_at_utc,
                   issuance_key, issuance_request_hash, requested_platform,
                   credential_key_id, credential_nonce, credential_ciphertext, credential_tag
            FROM device_access_tokens
            WHERE subscription_id = $subscription_id
              AND issuance_key = $issuance_key
            LIMIT 1;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$subscription_id", subscriptionId);
        command.Parameters.AddWithValue("$issuance_key", issuanceKey);
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);
        return await reader.ReadAsync(cancellationToken)
            ? ReadExistingDeviceCredential(reader)
            : null;
    }

    private static async Task<ExistingDeviceCredential?> GetLegacyDeviceCredentialCandidateAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long subscriptionId,
        string displayName,
        CancellationToken cancellationToken)
    {
        const string sql = """
            SELECT id, public_id, display_name, pending_expires_at_utc, revoked_at_utc,
                   issuance_key, issuance_request_hash, requested_platform,
                   credential_key_id, credential_nonce, credential_ciphertext, credential_tag
            FROM device_access_tokens
            WHERE subscription_id = $subscription_id
              AND display_name = $display_name
              AND revoked_at_utc IS NULL
              AND issuance_key IS NULL
              AND feed_policy_mode = 'legacy'
            ORDER BY
                CASE
                    WHEN credential_key_id IS NOT NULL
                     AND credential_nonce IS NOT NULL
                     AND credential_ciphertext IS NOT NULL
                     AND credential_tag IS NOT NULL
                    THEN 0
                    ELSE 1
                END,
                id DESC
            LIMIT 1;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$subscription_id", subscriptionId);
        command.Parameters.AddWithValue("$display_name", displayName);
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);
        return await reader.ReadAsync(cancellationToken)
            ? ReadExistingDeviceCredential(reader)
            : null;
    }

    private static ExistingDeviceCredential ReadExistingDeviceCredential(SqliteDataReader reader)
    {
        string? keyId = ReadString(reader, 8);
        string? nonce = ReadString(reader, 9);
        string? ciphertext = ReadString(reader, 10);
        string? tag = ReadString(reader, 11);
        ProtectedDeviceCredential? protectedCredential =
            keyId is not null && nonce is not null && ciphertext is not null && tag is not null
                ? new ProtectedDeviceCredential(keyId, nonce, ciphertext, tag)
                : null;

        return new ExistingDeviceCredential(
            reader.GetInt64(0),
            reader.GetString(1),
            reader.GetString(2),
            ReadDate(reader, 3),
            ReadDate(reader, 4),
            ReadString(reader, 5),
            ReadString(reader, 6),
            ReadString(reader, 7),
            protectedCredential);
    }

    private static async Task AssignIssuanceIdentityAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long deviceId,
        string issuanceKey,
        string issuanceRequestHash,
        string? requestedPlatform,
        CancellationToken cancellationToken)
    {
        const string sql = """
            UPDATE device_access_tokens
            SET issuance_key = $issuance_key,
                issuance_request_hash = $issuance_request_hash,
                requested_platform = COALESCE(requested_platform, $requested_platform)
            WHERE id = $id
              AND issuance_key IS NULL;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$issuance_key", issuanceKey);
        command.Parameters.AddWithValue("$issuance_request_hash", issuanceRequestHash);
        command.Parameters.AddWithValue("$requested_platform", DbValue(requestedPlatform));
        command.Parameters.AddWithValue("$id", deviceId);
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private static async Task BackfillIssuanceRequestHashAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long deviceId,
        string issuanceRequestHash,
        CancellationToken cancellationToken)
    {
        const string sql = """
            UPDATE device_access_tokens
            SET issuance_request_hash = $issuance_request_hash
            WHERE id = $id
              AND issuance_request_hash IS NULL;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$issuance_request_hash", issuanceRequestHash);
        command.Parameters.AddWithValue("$id", deviceId);
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private static async Task BackfillDeviceCredentialAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long deviceId,
        ProtectedDeviceCredential protectedCredential,
        CancellationToken cancellationToken)
    {
        const string sql = """
            UPDATE device_access_tokens
            SET credential_key_id = $credential_key_id,
                credential_nonce = $credential_nonce,
                credential_ciphertext = $credential_ciphertext,
                credential_tag = $credential_tag
            WHERE id = $id
              AND (
                  credential_key_id IS NULL
                  OR credential_nonce IS NULL
                  OR credential_ciphertext IS NULL
                  OR credential_tag IS NULL
              );
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$credential_key_id", protectedCredential.KeyId);
        command.Parameters.AddWithValue("$credential_nonce", protectedCredential.Nonce);
        command.Parameters.AddWithValue("$credential_ciphertext", protectedCredential.Ciphertext);
        command.Parameters.AddWithValue("$credential_tag", protectedCredential.Tag);
        command.Parameters.AddWithValue("$id", deviceId);
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private async Task ReadRepairDeviceCredentialAsync(
        long deviceId,
        ProtectedDeviceCredential previousCredential,
        string rawSecret,
        Guid publicGuid,
        CancellationToken cancellationToken)
    {
        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await using SqliteTransaction transaction = connection.BeginTransaction(deferred: false);
            ProtectedDeviceCredential currentCredential =
                _deviceCredentialProtector.Protect(rawSecret);
            const string sql = """
                UPDATE device_access_tokens
                SET credential_key_id = $credential_key_id,
                    credential_nonce = $credential_nonce,
                    credential_ciphertext = $credential_ciphertext,
                    credential_tag = $credential_tag
                WHERE id = $id
                  AND credential_key_id = $previous_key_id
                  AND credential_nonce = $previous_nonce
                  AND credential_ciphertext = $previous_ciphertext
                  AND credential_tag = $previous_tag;
                """;
            await using SqliteCommand command = new(sql, connection, transaction);
            command.Parameters.AddWithValue("$credential_key_id", currentCredential.KeyId);
            command.Parameters.AddWithValue("$credential_nonce", currentCredential.Nonce);
            command.Parameters.AddWithValue(
                "$credential_ciphertext",
                currentCredential.Ciphertext);
            command.Parameters.AddWithValue("$credential_tag", currentCredential.Tag);
            command.Parameters.AddWithValue("$id", deviceId);
            command.Parameters.AddWithValue("$previous_key_id", previousCredential.KeyId);
            command.Parameters.AddWithValue("$previous_nonce", previousCredential.Nonce);
            command.Parameters.AddWithValue(
                "$previous_ciphertext",
                previousCredential.Ciphertext);
            command.Parameters.AddWithValue("$previous_tag", previousCredential.Tag);
            int changed = await command.ExecuteNonQueryAsync(cancellationToken);

            if (changed > 0)
            {
                await AddAuditAsync(
                    connection,
                    transaction,
                    DateTimeOffset.UtcNow,
                    "device_token.credential_reencrypted",
                    publicGuid,
                    null,
                    null,
                    null,
                    cancellationToken);
            }

            await transaction.CommitAsync(cancellationToken);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    private static async Task<long> InsertDeviceCredentialAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long subscriptionId,
        string publicId,
        string secretHash,
        string displayName,
        DateTimeOffset createdAtUtc,
        DateTimeOffset pendingExpiresAtUtc,
        ProtectedDeviceCredential protectedCredential,
        string? issuanceKey,
        string? issuanceRequestHash,
        string? requestedPlatform,
        DeviceFeedPolicySeed policySeed,
        CancellationToken cancellationToken)
    {
        const string sql = """
            INSERT INTO device_access_tokens
                (subscription_id, public_id, secret_hash, display_name, created_at_utc,
                 pending_expires_at_utc, credential_key_id, credential_nonce,
                 credential_ciphertext, credential_tag, issuance_key, issuance_request_hash,
                 requested_platform, feed_policy_version, feed_policy_mode, binding_state,
                 bound_platform, bound_client_family, bound_at_utc,
                 bound_identity_hash, bound_identity_key_id, bound_identity_source,
                 last_identity_seen_at_utc, last_identity_mismatch_at_utc,
                 last_transfer_at_utc, transfer_count)
            VALUES
                ($subscription_id, $public_id, $secret_hash, $display_name, $created_at_utc,
                 $pending_expires_at_utc, $credential_key_id, $credential_nonce,
                 $credential_ciphertext, $credential_tag, $issuance_key, $issuance_request_hash,
                 $requested_platform, $feed_policy_version, $feed_policy_mode, $binding_state,
                 $bound_platform, $bound_client_family, $bound_at_utc,
                 $bound_identity_hash, $bound_identity_key_id, $bound_identity_source,
                 $last_identity_seen_at_utc, $last_identity_mismatch_at_utc,
                 $last_transfer_at_utc, $transfer_count)
            RETURNING id;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$subscription_id", subscriptionId);
        command.Parameters.AddWithValue("$public_id", publicId);
        command.Parameters.AddWithValue("$secret_hash", secretHash);
        command.Parameters.AddWithValue("$display_name", displayName);
        command.Parameters.AddWithValue("$created_at_utc", Format(createdAtUtc));
        command.Parameters.AddWithValue("$pending_expires_at_utc", Format(pendingExpiresAtUtc));
        command.Parameters.AddWithValue("$credential_key_id", protectedCredential.KeyId);
        command.Parameters.AddWithValue("$credential_nonce", protectedCredential.Nonce);
        command.Parameters.AddWithValue("$credential_ciphertext", protectedCredential.Ciphertext);
        command.Parameters.AddWithValue("$credential_tag", protectedCredential.Tag);
        command.Parameters.AddWithValue("$issuance_key", DbValue(issuanceKey));
        command.Parameters.AddWithValue("$issuance_request_hash", DbValue(issuanceRequestHash));
        command.Parameters.AddWithValue("$requested_platform", DbValue(requestedPlatform));
        command.Parameters.AddWithValue("$feed_policy_version", policySeed.PolicyVersion);
        command.Parameters.AddWithValue("$feed_policy_mode", policySeed.PolicyMode);
        command.Parameters.AddWithValue("$binding_state", policySeed.BindingState);
        command.Parameters.AddWithValue("$bound_platform", DbValue(policySeed.BoundPlatform));
        command.Parameters.AddWithValue("$bound_client_family", DbValue(policySeed.BoundClientFamily));
        command.Parameters.AddWithValue(
            "$bound_at_utc",
            DbValue(policySeed.BoundAtUtc is null ? null : Format(policySeed.BoundAtUtc.Value)));
        command.Parameters.AddWithValue(
            "$bound_identity_hash",
            DbValue(policySeed.BoundIdentityHash));
        command.Parameters.AddWithValue(
            "$bound_identity_key_id",
            DbValue(policySeed.BoundIdentityKeyId));
        command.Parameters.AddWithValue(
            "$bound_identity_source",
            DbValue(policySeed.BoundIdentitySource));
        command.Parameters.AddWithValue(
            "$last_identity_seen_at_utc",
            DbValue(policySeed.LastIdentitySeenAtUtc is null
                ? null
                : Format(policySeed.LastIdentitySeenAtUtc.Value)));
        command.Parameters.AddWithValue(
            "$last_identity_mismatch_at_utc",
            DbValue(policySeed.LastIdentityMismatchAtUtc is null
                ? null
                : Format(policySeed.LastIdentityMismatchAtUtc.Value)));
        command.Parameters.AddWithValue(
            "$last_transfer_at_utc",
            DbValue(policySeed.LastTransferAtUtc is null ? null : Format(policySeed.LastTransferAtUtc.Value)));
        command.Parameters.AddWithValue("$transfer_count", policySeed.TransferCount);
        return Convert.ToInt64(await command.ExecuteScalarAsync(cancellationToken));
    }

    private DeviceFeedPolicySeed CreateNewDeviceFeedPolicySeed()
    {
        string policyMode = _options.DefaultNewDeviceFeedPolicy;
        string bindingState = string.Equals(
            policyMode,
            DeviceFeedPolicyModes.Legacy,
            StringComparison.Ordinal)
            ? DeviceFeedBindingStates.Grandfathered
            : DeviceFeedBindingStates.Unbound;

        return new DeviceFeedPolicySeed(
            PolicyVersion: _options.DefaultNewDeviceFeedPolicyVersion,
            PolicyMode: policyMode,
            BindingState: bindingState,
            BoundPlatform: null,
            BoundClientFamily: null,
            BoundAtUtc: null,
            BoundIdentityHash: null,
            BoundIdentityKeyId: null,
            BoundIdentitySource: null,
            LastIdentitySeenAtUtc: null,
            LastIdentityMismatchAtUtc: null,
            LastTransferAtUtc: null,
            TransferCount: 0);
    }

    private static string ComputeDeviceIssuanceRequestHash(
        Guid publicGuid,
        string? requestedDisplayName,
        string? requestedPlatform)
    {
        const string domain = "device-issuance-request:v2";
        string payload = string.Join(
            '\0',
            domain,
            publicGuid.ToString("D"),
            requestedDisplayName ?? string.Empty,
            requestedPlatform ?? string.Empty);
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(payload)));
    }

    private static bool IsIssuanceReplayCompatible(
        ExistingDeviceCredential existing,
        string? requestedDisplayName,
        string? requestedPlatform,
        string issuanceRequestHash)
    {
        if (existing.IssuanceRequestHash is not null)
        {
            return string.Equals(
                existing.IssuanceRequestHash,
                issuanceRequestHash,
                StringComparison.Ordinal);
        }

        return (requestedDisplayName is null
                || string.Equals(
                    existing.DisplayName,
                    requestedDisplayName,
                    StringComparison.Ordinal))
            && string.Equals(
                existing.RequestedPlatform,
                requestedPlatform,
                StringComparison.Ordinal);
    }

    private string CreateLegacyCompatibilityIssuanceKey(string displayName)
    {
        const string domain = "legacy-display-name:v1";
        using HMACSHA256 hmac = new(Encoding.UTF8.GetBytes(GetDeviceTokenHashKey()));
        byte[] payload = Encoding.UTF8.GetBytes($"{domain}\0{displayName}");
        return $"{domain}:{Base64Url.Encode(hmac.ComputeHash(payload))}";
    }

    private string CreateProtectedCompatibilityIssuanceKey(
        string? requestedDisplayName,
        string? requestedPlatform,
        string policyMode)
    {
        const string domain = "protected-compatibility:v2";
        using HMACSHA256 hmac = new(Encoding.UTF8.GetBytes(GetDeviceTokenHashKey()));
        byte[] payload = Encoding.UTF8.GetBytes(string.Join(
            '\0',
            domain,
            requestedDisplayName ?? string.Empty,
            requestedPlatform ?? string.Empty,
            policyMode));
        return $"{domain}:{Base64Url.Encode(hmac.ComputeHash(payload))}";
    }

    private bool TryUnprotectCredential(
        ProtectedDeviceCredential protectedCredential,
        out string? rawSecret,
        out string? errorCode)
    {
        try
        {
            rawSecret = _deviceCredentialProtector.Unprotect(protectedCredential);
            errorCode = null;
            return true;
        }
        catch (DeviceCredentialKeyUnavailableException)
        {
            rawSecret = null;
            errorCode = "credential_key_unavailable";
            return false;
        }
        catch (Exception exception) when (
            exception is CryptographicException or FormatException or ArgumentException)
        {
            rawSecret = null;
            errorCode = "credential_corrupted";
            return false;
        }
    }

    private static string ComputeDeviceTransferFingerprint(
        Guid publicGuid,
        string devicePublicId,
        string requestedPlatform)
    {
        string payload = $"{publicGuid:D}\0{devicePublicId}\0{requestedPlatform}";
        return Base64Url.Encode(SHA256.HashData(Encoding.UTF8.GetBytes(payload)));
    }

    private static string? NormalizeRequestedPlatform(string? value)
    {
        string? normalized = TextSanitizer.NullIfWhiteSpace(value)?.Trim().ToLowerInvariant();

        if (normalized is null)
        {
            return null;
        }

        return normalized.Length <= 32
            && normalized.All(character =>
                char.IsAsciiLetterOrDigit(character) || character is '-' or '_')
            ? normalized
            : null;
    }

    private async Task<int> CountActiveDeviceTokensAsync(
        Guid publicGuid,
        CancellationToken cancellationToken)
    {
        await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);

        const string sql = """
            SELECT COUNT(*)
            FROM device_access_tokens d
            JOIN mediated_subscriptions s ON s.id = d.subscription_id
            WHERE s.public_guid = $public_guid
              AND d.revoked_at_utc IS NULL
              AND d.activated_at_utc IS NOT NULL;
            """;

        await using SqliteCommand command = new(sql, connection);
        command.Parameters.AddWithValue("$public_guid", publicGuid.ToString("D"));
        return Convert.ToInt32(await command.ExecuteScalarAsync(cancellationToken));
    }

    private static async Task<int> CountActiveDeviceTokensAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long subscriptionId,
        CancellationToken cancellationToken)
    {
        const string sql = """
            SELECT COUNT(*)
            FROM device_access_tokens
            WHERE subscription_id = $subscription_id
              AND revoked_at_utc IS NULL
              AND activated_at_utc IS NOT NULL;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$subscription_id", subscriptionId);
        return Convert.ToInt32(await command.ExecuteScalarAsync(cancellationToken));
    }

    private static async Task<int> CountPendingDeviceTokensAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long subscriptionId,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        const string sql = """
            SELECT COUNT(*)
            FROM device_access_tokens
            WHERE subscription_id = $subscription_id
              AND revoked_at_utc IS NULL
              AND activated_at_utc IS NULL
              AND (pending_expires_at_utc IS NULL OR pending_expires_at_utc > $now);
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$subscription_id", subscriptionId);
        command.Parameters.AddWithValue("$now", Format(now));
        return Convert.ToInt32(await command.ExecuteScalarAsync(cancellationToken));
    }

    private static async Task ExpirePendingDeviceTokensAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long subscriptionId,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        const string sql = """
            UPDATE device_access_tokens
            SET revoked_at_utc = $now,
                revocation_reason = 'expired_pending'
            WHERE subscription_id = $subscription_id
              AND revoked_at_utc IS NULL
              AND activated_at_utc IS NULL
              AND pending_expires_at_utc IS NOT NULL
              AND pending_expires_at_utc <= $now;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$subscription_id", subscriptionId);
        command.Parameters.AddWithValue("$now", Format(now));
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private static async Task RevokePendingDeviceTokensAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long subscriptionId,
        DateTimeOffset now,
        string reason,
        CancellationToken cancellationToken)
    {
        const string sql = """
            UPDATE device_access_tokens
            SET revoked_at_utc = $now,
                revocation_reason = $reason
            WHERE subscription_id = $subscription_id
              AND revoked_at_utc IS NULL
              AND activated_at_utc IS NULL;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$subscription_id", subscriptionId);
        command.Parameters.AddWithValue("$now", Format(now));
        command.Parameters.AddWithValue("$reason", reason);
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private static async Task RevokeDeviceTokenByIdAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long deviceId,
        DateTimeOffset now,
        string reason,
        CancellationToken cancellationToken)
    {
        const string sql = """
            UPDATE device_access_tokens
            SET revoked_at_utc = COALESCE(revoked_at_utc, $now),
                revocation_reason = COALESCE(revocation_reason, $reason),
                credential_key_id = NULL,
                credential_nonce = NULL,
                credential_ciphertext = NULL,
                credential_tag = NULL
            WHERE id = $id;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$id", deviceId);
        command.Parameters.AddWithValue("$now", Format(now));
        command.Parameters.AddWithValue("$reason", reason);
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private static async Task<SubscriptionIdentity?> FindByPublicGuidAsync(
        SqliteConnection connection,
        SqliteTransaction? transaction,
        Guid publicGuid,
        CancellationToken cancellationToken)
    {
        const string sql = "SELECT id, public_guid FROM mediated_subscriptions WHERE public_guid = $public_guid;";
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$public_guid", publicGuid.ToString("D"));
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);

        if (!await reader.ReadAsync(cancellationToken))
        {
            return null;
        }

        return new SubscriptionIdentity(reader.GetInt64(0), Guid.Parse(reader.GetString(1)));
    }

    private static async Task<SubscriptionIdentity?> FindByExternalRequestIdAsync(
        SqliteConnection connection,
        SqliteTransaction? transaction,
        string externalRequestId,
        CancellationToken cancellationToken)
    {
        const string sql = "SELECT id, public_guid FROM mediated_subscriptions WHERE external_request_id = $external_request_id;";
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$external_request_id", externalRequestId);
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);

        if (!await reader.ReadAsync(cancellationToken))
        {
            return null;
        }

        return new SubscriptionIdentity(reader.GetInt64(0), Guid.Parse(reader.GetString(1)));
    }

    private static async Task<EntitlementMirror?> GetEntitlementAsync(
        SqliteConnection connection,
        SqliteTransaction? transaction,
        long subscriptionId,
        CancellationToken cancellationToken)
    {
        const string sql = """
            SELECT version, status, valid_until_utc, max_device_tokens
            FROM entitlement_mirrors
            WHERE subscription_id = $subscription_id;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$subscription_id", subscriptionId);
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);

        if (!await reader.ReadAsync(cancellationToken))
        {
            return null;
        }

        return new EntitlementMirror(
            Version: reader.GetInt64(0),
            Status: reader.GetString(1),
            ValidUntilUtc: ReadDate(reader, 2),
            MaxDeviceTokens: reader.GetInt32(3));
    }

    private static async Task UpsertEntitlementAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long subscriptionId,
        EntitlementUpdateRequest request,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        const string sql = """
            INSERT INTO entitlement_mirrors
                (subscription_id, version, status, valid_until_utc, max_device_tokens, updated_at_utc)
            VALUES
                ($subscription_id, $version, $status, $valid_until_utc, $max_device_tokens, $updated_at_utc)
            ON CONFLICT(subscription_id)
            DO UPDATE SET
                version = excluded.version,
                status = excluded.status,
                valid_until_utc = excluded.valid_until_utc,
                max_device_tokens = excluded.max_device_tokens,
                updated_at_utc = excluded.updated_at_utc;
            """;

        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$subscription_id", subscriptionId);
        command.Parameters.AddWithValue("$version", request.Version);
        command.Parameters.AddWithValue("$status", request.Status);
        command.Parameters.AddWithValue("$valid_until_utc", DbValue(request.ValidUntilUtc is null ? null : Format(request.ValidUntilUtc.Value)));
        command.Parameters.AddWithValue("$max_device_tokens", request.MaxDeviceTokens);
        command.Parameters.AddWithValue("$updated_at_utc", Format(now));
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private async Task DeleteExpiredDeviceFeedObservationsAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long deviceId,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        DateTimeOffset cutoff = now.AddDays(-_options.DeviceFeedObservationRetentionDays);
        const string sql = """
            DELETE FROM device_access_sightings
            WHERE device_token_id = $device_token_id
              AND last_seen_at_utc < $cutoff;

            DELETE FROM device_feed_policy_events
            WHERE device_token_id = $device_token_id
              AND created_at_utc < $cutoff;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$device_token_id", deviceId);
        command.Parameters.AddWithValue("$cutoff", Format(cutoff));
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private async Task RecordDeviceFeedObservationAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long deviceId,
        DeviceFeedPolicyState state,
        DeviceAccessRequestContext context,
        DeviceFeedPolicyDecision decision,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        _ = state;
        _ = decision;

        if (context.NetworkFingerprint is null)
        {
            return;
        }

        const string upsert = """
            INSERT INTO device_access_sightings
                (device_token_id, network_fingerprint, platform, client_family,
                 first_seen_at_utc, last_seen_at_utc, request_count)
            VALUES
                ($device_token_id, $network_fingerprint, $platform, $client_family,
                 $now, $now, 1)
            ON CONFLICT(device_token_id, network_fingerprint, platform, client_family)
            DO UPDATE SET
                last_seen_at_utc = excluded.last_seen_at_utc,
                request_count = device_access_sightings.request_count + 1;
            """;
        await using (SqliteCommand command = new(upsert, connection, transaction))
        {
            command.Parameters.AddWithValue("$device_token_id", deviceId);
            command.Parameters.AddWithValue("$network_fingerprint", context.NetworkFingerprint);
            command.Parameters.AddWithValue("$platform", context.Metadata.Platform ?? string.Empty);
            command.Parameters.AddWithValue("$client_family", context.ClientFamily ?? string.Empty);
            command.Parameters.AddWithValue("$now", Format(now));
            await command.ExecuteNonQueryAsync(cancellationToken);
        }

        const string updateNetwork = """
            UPDATE device_access_tokens
            SET last_network_changed_at_utc = CASE
                    WHEN last_network_fingerprint IS NULL
                      OR last_network_fingerprint <> $network_fingerprint
                    THEN $now
                    ELSE last_network_changed_at_utc
                END,
                last_network_fingerprint = $network_fingerprint
            WHERE id = $id;
            """;
        await using (SqliteCommand command = new(updateNetwork, connection, transaction))
        {
            command.Parameters.AddWithValue("$network_fingerprint", context.NetworkFingerprint);
            command.Parameters.AddWithValue("$now", Format(now));
            command.Parameters.AddWithValue("$id", deviceId);
            await command.ExecuteNonQueryAsync(cancellationToken);
        }

        DateTimeOffset cutoff = now.AddMinutes(-_options.DeviceFeedConcurrentNetworkWindowMinutes);
        const string countRecent = """
            SELECT COUNT(DISTINCT network_fingerprint)
            FROM device_access_sightings
            WHERE device_token_id = $device_token_id
              AND last_seen_at_utc >= $cutoff;
            """;
        long recentNetworks;
        await using (SqliteCommand command = new(countRecent, connection, transaction))
        {
            command.Parameters.AddWithValue("$device_token_id", deviceId);
            command.Parameters.AddWithValue("$cutoff", Format(cutoff));
            recentNetworks = Convert.ToInt64(await command.ExecuteScalarAsync(cancellationToken));
        }

        if (recentNetworks < 2)
        {
            return;
        }

        bool sameIdentity = state.PolicyVersion == DeviceFeedPolicyVersions.HwidIdentity
            && context.Identity.IsValid
            && (string.IsNullOrWhiteSpace(state.BoundIdentityHash)
                || context.Identity.Matches(state.BoundIdentityHash));
        if (sameIdentity)
        {
            return;
        }

        const string raiseRisk = """
            UPDATE device_access_tokens
            SET risk_score = risk_score + 1,
                binding_state = CASE
                    WHEN feed_policy_mode <> 'legacy' AND risk_score + 1 >= 3
                    THEN 'review'
                    ELSE binding_state
                END,
                last_policy_event_at_utc = $now
            WHERE id = $id
              AND (
                  last_policy_event_at_utc IS NULL
                  OR last_policy_event_at_utc < $cutoff
              );
            """;
        int changed;
        await using (SqliteCommand command = new(raiseRisk, connection, transaction))
        {
            command.Parameters.AddWithValue("$now", Format(now));
            command.Parameters.AddWithValue("$cutoff", Format(cutoff));
            command.Parameters.AddWithValue("$id", deviceId);
            changed = await command.ExecuteNonQueryAsync(cancellationToken);
        }

        if (changed > 0)
        {
            await RecordDeviceFeedPolicyEventAsync(
                connection,
                transaction,
                deviceId,
                now,
                "parallel_networks_observed",
                "multiple_recent_networks",
                context.Metadata.Platform,
                state.BoundPlatform ?? state.RequestedPlatform,
                decision.Decision,
                context,
                state,
                cancellationToken);
        }
    }

    private static async Task BindDeviceFeedAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long deviceId,
        DeviceFeedPolicyState state,
        DeviceAccessRequestContext context,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        if (state.PolicyVersion == DeviceFeedPolicyVersions.PlatformHeuristic)
        {
            if (context.Metadata.Platform is null)
            {
                return;
            }

            const string platformSql = """
                UPDATE device_access_tokens
                SET binding_state = $binding_state,
                    bound_platform = $bound_platform,
                    bound_client_family = $bound_client_family,
                    bound_at_utc = COALESCE(bound_at_utc, $bound_at_utc)
                WHERE id = $id;
                """;
            await using SqliteCommand platformCommand = new(platformSql, connection, transaction);
            platformCommand.Parameters.AddWithValue("$binding_state", DeviceFeedBindingStates.Bound);
            platformCommand.Parameters.AddWithValue("$bound_platform", context.Metadata.Platform);
            platformCommand.Parameters.AddWithValue("$bound_client_family", DbValue(context.ClientFamily));
            platformCommand.Parameters.AddWithValue("$bound_at_utc", Format(now));
            platformCommand.Parameters.AddWithValue("$id", deviceId);
            await platformCommand.ExecuteNonQueryAsync(cancellationToken);
            return;
        }

        if (state.PolicyVersion != DeviceFeedPolicyVersions.HwidIdentity
            || !context.Identity.IsValid
            || context.Identity.CurrentHash is null
            || context.Identity.CurrentKeyId is null
            || context.Identity.Source is null)
        {
            throw new InvalidOperationException(
                "A valid device identity is required before creating a version 2 device binding.");
        }

        const string identitySql = """
            UPDATE device_access_tokens
            SET binding_state = $binding_state,
                bound_platform = COALESCE($observed_platform, requested_platform, bound_platform),
                bound_client_family = $bound_client_family,
                bound_at_utc = COALESCE(bound_at_utc, $bound_at_utc),
                bound_identity_hash = $bound_identity_hash,
                bound_identity_key_id = $bound_identity_key_id,
                bound_identity_source = $bound_identity_source,
                last_identity_seen_at_utc = $last_identity_seen_at_utc
            WHERE id = $id
              AND bound_identity_hash IS NULL;
            """;
        int changed;
        await using (SqliteCommand identityCommand = new(identitySql, connection, transaction))
        {
            identityCommand.Parameters.AddWithValue("$binding_state", DeviceFeedBindingStates.Bound);
            identityCommand.Parameters.AddWithValue("$observed_platform", DbValue(context.Metadata.Platform));
            identityCommand.Parameters.AddWithValue("$bound_client_family", DbValue(context.ClientFamily));
            identityCommand.Parameters.AddWithValue("$bound_at_utc", Format(now));
            identityCommand.Parameters.AddWithValue("$bound_identity_hash", context.Identity.CurrentHash);
            identityCommand.Parameters.AddWithValue("$bound_identity_key_id", context.Identity.CurrentKeyId);
            identityCommand.Parameters.AddWithValue("$bound_identity_source", context.Identity.Source);
            identityCommand.Parameters.AddWithValue("$last_identity_seen_at_utc", Format(now));
            identityCommand.Parameters.AddWithValue("$id", deviceId);
            changed = await identityCommand.ExecuteNonQueryAsync(cancellationToken);
        }

        if (changed == 1)
        {
            return;
        }

        const string readSql = """
            SELECT bound_identity_hash
            FROM device_access_tokens
            WHERE id = $id;
            """;
        await using SqliteCommand readCommand = new(readSql, connection, transaction);
        readCommand.Parameters.AddWithValue("$id", deviceId);
        string? persistedHash = await readCommand.ExecuteScalarAsync(cancellationToken) as string;
        if (!context.Identity.Matches(persistedHash))
        {
            throw new InvalidOperationException(
                "The device token was concurrently bound to a different device identity.");
        }
    }

    private static async Task RefreshDeviceIdentityHashAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long deviceId,
        DeviceAccessRequestContext context,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        if (!context.Identity.IsValid
            || context.Identity.CurrentHash is null
            || context.Identity.CurrentKeyId is null
            || context.Identity.Source is null)
        {
            throw new InvalidOperationException(
                "A valid current device identity hash is required for key rotation.");
        }

        const string sql = """
            UPDATE device_access_tokens
            SET bound_identity_hash = $bound_identity_hash,
                bound_identity_key_id = $bound_identity_key_id,
                bound_identity_source = $bound_identity_source,
                last_identity_seen_at_utc = $last_identity_seen_at_utc
            WHERE id = $id;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$bound_identity_hash", context.Identity.CurrentHash);
        command.Parameters.AddWithValue("$bound_identity_key_id", context.Identity.CurrentKeyId);
        command.Parameters.AddWithValue("$bound_identity_source", context.Identity.Source);
        command.Parameters.AddWithValue("$last_identity_seen_at_utc", Format(now));
        command.Parameters.AddWithValue("$id", deviceId);
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private static async Task MarkIdentitySeenAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long deviceId,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        const string sql = """
            UPDATE device_access_tokens
            SET last_identity_seen_at_utc = $now
            WHERE id = $id;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$now", Format(now));
        command.Parameters.AddWithValue("$id", deviceId);
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private static async Task MarkIdentityMismatchAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long deviceId,
        string? reasonCode,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        if (reasonCode is null
            || !reasonCode.StartsWith("identity_", StringComparison.Ordinal))
        {
            return;
        }

        const string sql = """
            UPDATE device_access_tokens
            SET last_identity_mismatch_at_utc = $now
            WHERE id = $id;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$now", Format(now));
        command.Parameters.AddWithValue("$id", deviceId);
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private static async Task RecordDeviceFeedPolicyEventAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long deviceId,
        DateTimeOffset now,
        string eventType,
        string? reasonCode,
        string? observedPlatform,
        string? expectedPlatform,
        string decision,
        DeviceAccessRequestContext context,
        DeviceFeedPolicyState state,
        CancellationToken cancellationToken)
    {
        bool? identityMatch = !context.Identity.IsPresent
            ? null
            : string.Equals(eventType, "binding_created", StringComparison.Ordinal)
                ? true
                : string.IsNullOrWhiteSpace(state.BoundIdentityHash)
                    ? null
                    : context.Identity.Matches(state.BoundIdentityHash);
        const string sql = """
            INSERT INTO device_feed_policy_events
                (device_token_id, created_at_utc, event_type, reason_code,
                 observed_platform, expected_platform, decision,
                 identity_source, identity_present, identity_match)
            VALUES
                ($device_token_id, $created_at_utc, $event_type, $reason_code,
                 $observed_platform, $expected_platform, $decision,
                 $identity_source, $identity_present, $identity_match);
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$device_token_id", deviceId);
        command.Parameters.AddWithValue("$created_at_utc", Format(now));
        command.Parameters.AddWithValue("$event_type", eventType);
        command.Parameters.AddWithValue("$reason_code", DbValue(reasonCode));
        command.Parameters.AddWithValue("$observed_platform", DbValue(observedPlatform));
        command.Parameters.AddWithValue("$expected_platform", DbValue(expectedPlatform));
        command.Parameters.AddWithValue("$decision", decision);
        command.Parameters.AddWithValue("$identity_source", DbValue(context.Identity.Source));
        command.Parameters.AddWithValue("$identity_present", context.Identity.IsPresent ? 1 : 0);
        command.Parameters.AddWithValue(
            "$identity_match",
            identityMatch is null ? DBNull.Value : identityMatch.Value ? 1 : 0);
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private static async Task MarkDeviceUsedAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long deviceId,
        DateTimeOffset? activatedAt,
        DateTimeOffset? lastUsedAt,
        DateTimeOffset now,
        DeviceMetadata metadata,
        CancellationToken cancellationToken)
    {
        if (activatedAt is not null && lastUsedAt is not null && now - lastUsedAt < TimeSpan.FromMinutes(5))
        {
            return;
        }

        const string sql = """
            UPDATE device_access_tokens
            SET activated_at_utc = COALESCE(activated_at_utc, $now),
                first_fetched_at_utc = COALESCE(first_fetched_at_utc, $now),
                last_used_at_utc = $now,
                device_type = COALESCE(device_type, $device_type),
                platform = COALESCE(platform, $platform),
                detected_model = COALESCE(detected_model, $detected_model),
                detection_source = COALESCE(detection_source, $detection_source)
            WHERE id = $id;
            """;

        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$now", Format(now));
        command.Parameters.AddWithValue("$device_type", DbValue(metadata.DeviceType));
        command.Parameters.AddWithValue("$platform", DbValue(metadata.Platform));
        command.Parameters.AddWithValue("$detected_model", DbValue(metadata.DetectedModel));
        command.Parameters.AddWithValue("$detection_source", DbValue(metadata.DetectionSource));
        command.Parameters.AddWithValue("$id", deviceId);
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private async Task<UpstreamSource?> GetSourceAsync(
        SqliteConnection connection,
        SqliteTransaction? transaction,
        long sourceId,
        bool includeEndpoint,
        CancellationToken cancellationToken)
    {
        const string sql = """
            SELECT id, name, kind, encrypted_endpoint, state, sort_order
            FROM upstream_sources
            WHERE id = $id;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$id", sourceId);
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);

        if (!await reader.ReadAsync(cancellationToken))
        {
            return null;
        }

        string endpoint = includeEndpoint
            ? _endpointProtector.Unprotect(reader.GetString(3))
            : string.Empty;

        return new UpstreamSource(
            Id: reader.GetInt64(0),
            Name: reader.GetString(1),
            Kind: reader.GetString(2),
            Endpoint: endpoint,
            State: reader.GetString(4),
            SortOrder: reader.GetInt32(5));
    }

    private async Task<IReadOnlyList<UpstreamSource>> GetRefreshableSourcesAsync(
        SqliteConnection connection,
        CancellationToken cancellationToken)
    {
        const string sql = """
            SELECT id, name, kind, encrypted_endpoint, state, sort_order
            FROM upstream_sources
            WHERE state IN ($enabled, $degraded)
            ORDER BY sort_order ASC, id ASC;
            """;
        await using SqliteCommand command = new(sql, connection);
        command.Parameters.AddWithValue("$enabled", SourceStates.Enabled);
        command.Parameters.AddWithValue("$degraded", SourceStates.Degraded);
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);
        List<UpstreamSource> sources = [];

        while (await reader.ReadAsync(cancellationToken))
        {
            sources.Add(new UpstreamSource(
                Id: reader.GetInt64(0),
                Name: reader.GetString(1),
                Kind: reader.GetString(2),
                Endpoint: _endpointProtector.Unprotect(reader.GetString(3)),
                State: reader.GetString(4),
                SortOrder: reader.GetInt32(5)));
        }

        return sources;
    }

    private static async Task<long> InsertSourceSnapshotAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long sourceId,
        SourceReadResult readResult,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        const string versionSql = "SELECT COALESCE(MAX(version), 0) + 1 FROM source_snapshots WHERE source_id = $source_id;";
        await using SqliteCommand versionCommand = new(versionSql, connection, transaction);
        versionCommand.Parameters.AddWithValue("$source_id", sourceId);
        long version = (long)(await versionCommand.ExecuteScalarAsync(cancellationToken) ?? 1L);

        const string insertSql = """
            INSERT INTO source_snapshots
                (source_id, version, created_at_utc, state, server_links_payload, validation_summary)
            VALUES
                ($source_id, $version, $created_at_utc, $state, $server_links_payload, $validation_summary);
            """;
        await using SqliteCommand insertCommand = new(insertSql, connection, transaction);
        insertCommand.Parameters.AddWithValue("$source_id", sourceId);
        insertCommand.Parameters.AddWithValue("$version", version);
        insertCommand.Parameters.AddWithValue("$created_at_utc", Format(now));
        insertCommand.Parameters.AddWithValue("$state", readResult.Success ? "valid" : "invalid");
        insertCommand.Parameters.AddWithValue("$server_links_payload", JsonSerializer.Serialize(readResult.ServerLinks, JsonOptions));
        insertCommand.Parameters.AddWithValue("$validation_summary", JsonSerializer.Serialize(new
        {
            readResult.AcceptedSchemes,
            readResult.FilteredCount,
            readResult.ValidationErrors
        }, JsonOptions));
        await insertCommand.ExecuteNonQueryAsync(cancellationToken);
        return version;
    }

    private async Task<SourceSnapshot?> GetLatestUsableSourceSnapshotAsync(
        SqliteConnection connection,
        long sourceId,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        const string sql = """
            SELECT version, created_at_utc, server_links_payload
            FROM source_snapshots
            WHERE source_id = $source_id AND state = 'valid'
            ORDER BY version DESC
            LIMIT 1;
            """;
        await using SqliteCommand command = new(sql, connection);
        command.Parameters.AddWithValue("$source_id", sourceId);
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);

        if (!await reader.ReadAsync(cancellationToken))
        {
            return null;
        }

        DateTimeOffset createdAt = Parse(reader.GetString(1));

        if (now - createdAt > TimeSpan.FromHours(_options.ServerCatalogMaxStaleHours))
        {
            return null;
        }

        IReadOnlyList<string> links = JsonSerializer.Deserialize<List<string>>(reader.GetString(2), JsonOptions) ?? [];
        return new SourceSnapshot(reader.GetInt64(0), createdAt, links);
    }

    private static async Task<IReadOnlyList<string>> LoadSourceCatalogSnapshotAsync(
        SqliteConnection connection,
        IReadOnlyDictionary<long, long> sourceVersions,
        CancellationToken cancellationToken)
    {
        const string sql = """
            SELECT server_links_payload
            FROM source_snapshots
            WHERE source_id = $source_id
              AND version = $version
              AND state = 'valid';
            """;
        List<string> links = [];

        foreach ((long sourceId, long version) in sourceVersions.OrderBy(entry => entry.Key))
        {
            await using SqliteCommand command = new(sql, connection);
            command.Parameters.AddWithValue("$source_id", sourceId);
            command.Parameters.AddWithValue("$version", version);
            object? payload = await command.ExecuteScalarAsync(cancellationToken);
            if (payload is string serialized)
            {
                links.AddRange(
                    JsonSerializer.Deserialize<List<string>>(serialized, JsonOptions) ?? []);
            }
        }

        return CatalogPresentation.Deduplicate(links);
    }

    private static async Task MarkSourceRefreshSucceededAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long sourceId,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        const string sql = """
            UPDATE upstream_sources
            SET last_successful_refresh_at_utc = $now,
                last_error_code = NULL,
                state = CASE WHEN state = $degraded THEN $enabled ELSE state END,
                updated_at_utc = $now
            WHERE id = $id;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$now", Format(now));
        command.Parameters.AddWithValue("$degraded", SourceStates.Degraded);
        command.Parameters.AddWithValue("$enabled", SourceStates.Enabled);
        command.Parameters.AddWithValue("$id", sourceId);
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private static async Task MarkSourceRefreshFailedAsync(
        SqliteConnection connection,
        SqliteTransaction? transaction,
        long sourceId,
        DateTimeOffset now,
        string errorCode,
        CancellationToken cancellationToken)
    {
        const string sql = """
            UPDATE upstream_sources
            SET last_failed_refresh_at_utc = $now,
                last_error_code = $error_code,
                state = CASE WHEN state = $enabled THEN $degraded ELSE state END,
                updated_at_utc = $now
            WHERE id = $id;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$now", Format(now));
        command.Parameters.AddWithValue("$error_code", errorCode);
        command.Parameters.AddWithValue("$enabled", SourceStates.Enabled);
        command.Parameters.AddWithValue("$degraded", SourceStates.Degraded);
        command.Parameters.AddWithValue("$id", sourceId);
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private static async Task<long> PublishSnapshotAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        DateTimeOffset now,
        DateTimeOffset dataAsOfUtc,
        string state,
        IReadOnlyList<string> serverLinks,
        Dictionary<long, long> sourceVersions,
        CancellationToken cancellationToken)
    {
        const string versionSql = "SELECT COALESCE(MAX(version), 0) + 1 FROM published_snapshots;";
        await using SqliteCommand versionCommand = new(versionSql, connection, transaction);
        long version = (long)(await versionCommand.ExecuteScalarAsync(cancellationToken) ?? 1L);
        string contentFingerprint = CatalogFingerprint.ComputeContent(serverLinks);
        string presentationFingerprint = CatalogFingerprint.ComputePresentation(serverLinks);

        const string insertSql = """
            INSERT INTO published_snapshots
                (version, created_at_utc, data_as_of_utc, state, server_links_payload,
                 source_versions_payload, content_fingerprint, presentation_fingerprint,
                 presentation_version)
            VALUES
                ($version, $created_at_utc, $data_as_of_utc, $state, $server_links_payload,
                 $source_versions_payload, $content_fingerprint, $presentation_fingerprint,
                 $presentation_version);
            """;
        await using SqliteCommand insertCommand = new(insertSql, connection, transaction);
        insertCommand.Parameters.AddWithValue("$version", version);
        insertCommand.Parameters.AddWithValue("$created_at_utc", Format(now));
        insertCommand.Parameters.AddWithValue("$data_as_of_utc", Format(dataAsOfUtc));
        insertCommand.Parameters.AddWithValue("$state", state);
        insertCommand.Parameters.AddWithValue("$server_links_payload", JsonSerializer.Serialize(serverLinks, JsonOptions));
        insertCommand.Parameters.AddWithValue("$source_versions_payload", JsonSerializer.Serialize(sourceVersions, JsonOptions));
        insertCommand.Parameters.AddWithValue("$content_fingerprint", contentFingerprint);
        insertCommand.Parameters.AddWithValue("$presentation_fingerprint", presentationFingerprint);
        insertCommand.Parameters.AddWithValue(
            "$presentation_version",
            CatalogPresentation.CurrentVersion);
        await insertCommand.ExecuteNonQueryAsync(cancellationToken);
        await AddAuditAsync(
            connection,
            transaction,
            now,
            "snapshot.published",
            null,
            null,
            version,
            null,
            cancellationToken);
        return version;
    }

    private static async Task<PublishedSnapshot?> GetLatestPublishedSnapshotAsync(
        SqliteConnection connection,
        CancellationToken cancellationToken)
    {
        const string sql = """
            SELECT version, created_at_utc, data_as_of_utc, server_links_payload, state,
                   source_versions_payload, content_fingerprint, presentation_fingerprint,
                   presentation_version
            FROM published_snapshots
            ORDER BY version DESC
            LIMIT 1;
            """;
        await using SqliteCommand command = new(sql, connection);
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);

        if (!await reader.ReadAsync(cancellationToken))
        {
            return null;
        }

        return ReadPublishedSnapshot(reader);
    }
}

public sealed class AesGcmEndpointProtector : IEndpointProtector
{
    private readonly string _currentKeyId;
    private readonly IReadOnlyDictionary<string, byte[]> _keys;

    public AesGcmEndpointProtector(
        IOptions<VpnMediatorOptions> options,
        IWebHostEnvironment environment)
    {
        VpnMediatorOptions value = options.Value;
        _currentKeyId = TextSanitizer.NullIfWhiteSpace(value.SourceEndpointProtectionKeyId)
            ?? "v1";
        Dictionary<string, byte[]> keys = new(StringComparer.Ordinal);
        string? configuredKey = TextSanitizer.NullIfWhiteSpace(value.SourceEndpointProtectionKey);
        if (configuredKey is null)
        {
            if (!environment.IsDevelopment())
            {
                throw new InvalidOperationException(
                    "VpnMediator:SourceEndpointProtectionKey is required outside development.");
            }
            keys[_currentKeyId] = SHA256.HashData(
                Encoding.UTF8.GetBytes(value.LinkSigningSecret));
        }
        else
        {
            keys[_currentKeyId] = DecodeKey(configuredKey, "SourceEndpointProtectionKey");
        }

        string? previousId = TextSanitizer.NullIfWhiteSpace(
            value.PreviousSourceEndpointProtectionKeyId);
        string? previousKey = TextSanitizer.NullIfWhiteSpace(
            value.PreviousSourceEndpointProtectionKey);
        if (previousId is not null && previousKey is not null)
        {
            keys[previousId] = DecodeKey(
                previousKey,
                "PreviousSourceEndpointProtectionKey");
        }
        _keys = keys;
    }

    public string Protect(string endpoint)
    {
        byte[] nonce = RandomNumberGenerator.GetBytes(12);
        byte[] plaintext = Encoding.UTF8.GetBytes(endpoint);
        byte[] ciphertext = new byte[plaintext.Length];
        byte[] tag = new byte[16];
        using AesGcm aes = new(_keys[_currentKeyId], 16);
        aes.Encrypt(nonce, plaintext, ciphertext, tag);

        byte[] payload = new byte[nonce.Length + tag.Length + ciphertext.Length];
        nonce.CopyTo(payload, 0);
        tag.CopyTo(payload, nonce.Length);
        ciphertext.CopyTo(payload, nonce.Length + tag.Length);
        return $"v1:{_currentKeyId}:{Convert.ToBase64String(payload)}";
    }

    public string Unprotect(string protectedEndpoint)
    {
        (string? keyId, string payloadText) = ParsePayload(protectedEndpoint);
        byte[] payload = Convert.FromBase64String(payloadText);
        if (payload.Length < 29)
        {
            throw new InvalidOperationException("Protected endpoint payload is invalid.");
        }

        IEnumerable<byte[]> candidateKeys = keyId is not null
            ? [_keys.TryGetValue(keyId, out byte[]? resolvedKey)
                ? resolvedKey
                : throw new InvalidOperationException("Protected endpoint key id is unknown.")]
            : _keys.Values;
        foreach (byte[] candidateKey in candidateKeys)
        {
            try
            {
                ReadOnlySpan<byte> nonce = payload.AsSpan(0, 12);
                ReadOnlySpan<byte> tag = payload.AsSpan(12, 16);
                ReadOnlySpan<byte> ciphertext = payload.AsSpan(28);
                byte[] plaintext = new byte[ciphertext.Length];
                using AesGcm aes = new(candidateKey, 16);
                aes.Decrypt(nonce, ciphertext, tag, plaintext);
                return Encoding.UTF8.GetString(plaintext);
            }
            catch (CryptographicException) when (keyId is null)
            {
            }
        }

        throw new CryptographicException("Protected endpoint could not be decrypted.");
    }

    public bool NeedsReencryption(string protectedEndpoint)
    {
        (string? keyId, _) = ParsePayload(protectedEndpoint);
        return !string.Equals(keyId, _currentKeyId, StringComparison.Ordinal);
    }

    private static (string? KeyId, string Payload) ParsePayload(string value)
    {
        string[] parts = value.Split(':', 3);
        return parts.Length == 3 && parts[0] == "v1"
            ? (parts[1], parts[2])
            : (null, value);
    }

    private static byte[] DecodeKey(string value, string name)
    {
        byte[] key = Convert.FromBase64String(value);
        if (key.Length != 32)
        {
            throw new InvalidOperationException(
                $"VpnMediator:{name} must be a base64 encoded 32-byte key.");
        }
        return key;
    }
}


public sealed class SsrfSafeHttpFetcher : ISsrfSafeHttpFetcher
{
    private readonly VpnMediatorOptions _options;
    private readonly IWebHostEnvironment _environment;
    private readonly IHostAddressResolver _resolver;

    public SsrfSafeHttpFetcher(
        IOptions<VpnMediatorOptions> options,
        IWebHostEnvironment environment,
        IHostAddressResolver resolver)
    {
        _options = options.Value;
        _environment = environment;
        _resolver = resolver;
    }

    public async Task<FetchResult> FetchStringAsync(
        Uri endpoint,
        CancellationToken cancellationToken)
    {
        Uri current = endpoint;

        for (int redirect = 0; redirect <= _options.ServerSourceMaxRedirects; redirect++)
        {
            EndpointValidationResult validation = await ResolveAndValidateEndpointAsync(current, cancellationToken);

            if (!validation.IsAllowed)
            {
                return FetchResult.Failed(validation.ErrorCode ?? "uri_blocked");
            }

            using CancellationTokenSource timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            timeoutCts.CancelAfter(TimeSpan.FromSeconds(_options.ServerSourceTimeoutSeconds));

            using SocketsHttpHandler handler = CreateHandler(validation.Address!);
            using HttpClient client = new(handler)
            {
                Timeout = TimeSpan.FromSeconds(_options.ServerSourceTimeoutSeconds)
            };

            HttpResponseMessage response;

            try
            {
                using HttpRequestMessage request = new(HttpMethod.Get, current);
                response = await client.SendAsync(
                    request,
                    HttpCompletionOption.ResponseHeadersRead,
                    timeoutCts.Token);
            }
            catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
            {
                return FetchResult.Failed("timeout");
            }
            catch (HttpRequestException)
            {
                return FetchResult.Failed("connect_failed");
            }

            using (response)
            {
                if (IsRedirect(response.StatusCode))
                {
                    if (redirect == _options.ServerSourceMaxRedirects)
                    {
                        return FetchResult.Failed("redirect_limit");
                    }

                    Uri? next = response.Headers.Location;

                    if (next is null)
                    {
                        return FetchResult.Failed("redirect_missing_location");
                    }

                    current = next.IsAbsoluteUri ? next : new Uri(current, next);
                    continue;
                }

                if (!response.IsSuccessStatusCode)
                {
                    return FetchResult.Failed("http_error");
                }

                try
                {
                    await using Stream stream = await response.Content.ReadAsStreamAsync(timeoutCts.Token);
                    string body = await ReadLimitedStringAsync(
                        stream,
                        _options.ServerCatalogMaxResponseBytes,
                        timeoutCts.Token);

                    return FetchResult.Succeeded(body);
                }
                catch (InvalidOperationException)
                {
                    return FetchResult.Failed("response_too_large");
                }
                catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
                {
                    return FetchResult.Failed("timeout");
                }
            }
        }

        return FetchResult.Failed("redirect_limit");
    }

    public async Task<UriValidationResult> ValidateUriAsync(Uri endpoint, CancellationToken cancellationToken)
    {
        EndpointValidationResult result = await ResolveAndValidateEndpointAsync(endpoint, cancellationToken);
        return result.IsAllowed
            ? UriValidationResult.Allowed()
            : UriValidationResult.Blocked(result.ErrorCode ?? "uri_blocked");
    }

    private async Task<EndpointValidationResult> ResolveAndValidateEndpointAsync(
        Uri endpoint,
        CancellationToken cancellationToken)
    {
        if (!endpoint.IsAbsoluteUri)
        {
            return EndpointValidationResult.Blocked("not_absolute");
        }

        if (endpoint.AbsoluteUri.Length > _options.ServerSourceMaxUriLength)
        {
            return EndpointValidationResult.Blocked("uri_too_long");
        }

        if (endpoint.Scheme != Uri.UriSchemeHttps)
        {
            if (!(endpoint.Scheme == Uri.UriSchemeHttp && _environment.IsDevelopment() && _options.AllowDevelopmentHttpSources))
            {
                return EndpointValidationResult.Blocked("scheme_blocked");
            }
        }

        if (string.Equals(endpoint.Host, "169.254.169.254", StringComparison.Ordinal))
        {
            return EndpointValidationResult.Blocked("address_blocked");
        }

        IPAddress[] addresses;

        try
        {
            addresses = await _resolver.GetHostAddressesAsync(endpoint.Host, cancellationToken);
        }
        catch (SocketException)
        {
            return EndpointValidationResult.Blocked("dns_failed");
        }

        if (addresses.Length == 0)
        {
            return EndpointValidationResult.Blocked("dns_empty");
        }

        foreach (IPAddress address in addresses)
        {
            if (!NetworkAddressClassifier.IsPublicRoutable(address))
            {
                return EndpointValidationResult.Blocked("address_blocked");
            }
        }

        return EndpointValidationResult.Allowed(addresses[0]);
    }

    private SocketsHttpHandler CreateHandler(IPAddress pinnedAddress)
    {
        return new SocketsHttpHandler
        {
            AllowAutoRedirect = false,
            AutomaticDecompression = System.Net.DecompressionMethods.None,
            ConnectTimeout = TimeSpan.FromSeconds(_options.ServerSourceConnectTimeoutSeconds),
            UseProxy = false,
            MaxResponseHeadersLength = 64,
            ConnectCallback = async (context, cancellationToken) =>
            {
                Socket socket = new(pinnedAddress.AddressFamily, SocketType.Stream, ProtocolType.Tcp);

                try
                {
                    using CancellationTokenSource connectTimeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
                    connectTimeout.CancelAfter(TimeSpan.FromSeconds(_options.ServerSourceConnectTimeoutSeconds));
                    await socket.ConnectAsync(new IPEndPoint(pinnedAddress, context.DnsEndPoint.Port), connectTimeout.Token);
                    return new NetworkStream(socket, ownsSocket: true);
                }
                catch
                {
                    socket.Dispose();
                    throw;
                }
            }
        };
    }

    private static bool IsRedirect(HttpStatusCode statusCode)
    {
        int code = (int)statusCode;
        return code is 301 or 302 or 303 or 307 or 308;
    }

    private static async Task<string> ReadLimitedStringAsync(
        Stream stream,
        int maxBytes,
        CancellationToken cancellationToken)
    {
        using MemoryStream memory = new();
        byte[] buffer = ArrayPool<byte>.Shared.Rent(8192);

        try
        {
            while (true)
            {
                int read = await stream.ReadAsync(buffer.AsMemory(0, buffer.Length), cancellationToken);

                if (read == 0)
                {
                    break;
                }

                if (memory.Length + read > maxBytes)
                {
                    throw new InvalidOperationException("Response size limit exceeded.");
                }

                memory.Write(buffer, 0, read);
            }
        }
        finally
        {
            ArrayPool<byte>.Shared.Return(buffer);
        }

        return Encoding.UTF8.GetString(memory.ToArray());
    }
}

public sealed record NetworkAddressClassification(bool Allowed, string ReasonCode);

public static class NetworkAddressClassifier
{
    private static readonly IpNetworkRange[] BlockedIpv4Ranges =
    [
        IpNetworkRange.Parse("0.0.0.0/8", "unspecified_or_this_network"),
        IpNetworkRange.Parse("10.0.0.0/8", "private"),
        IpNetworkRange.Parse("100.64.0.0/10", "shared_address_space"),
        IpNetworkRange.Parse("127.0.0.0/8", "loopback"),
        IpNetworkRange.Parse("169.254.0.0/16", "link_local"),
        IpNetworkRange.Parse("172.16.0.0/12", "private"),
        IpNetworkRange.Parse("192.0.0.0/24", "special_purpose"),
        IpNetworkRange.Parse("192.0.2.0/24", "documentation"),
        IpNetworkRange.Parse("192.88.99.0/24", "deprecated_transition"),
        IpNetworkRange.Parse("192.168.0.0/16", "private"),
        IpNetworkRange.Parse("198.18.0.0/15", "benchmark"),
        IpNetworkRange.Parse("198.51.100.0/24", "documentation"),
        IpNetworkRange.Parse("203.0.113.0/24", "documentation"),
        IpNetworkRange.Parse("224.0.0.0/4", "multicast"),
        IpNetworkRange.Parse("240.0.0.0/4", "reserved")
    ];

    private static readonly IpNetworkRange[] BlockedIpv6Ranges =
    [
        IpNetworkRange.Parse("::/128", "unspecified"),
        IpNetworkRange.Parse("::1/128", "loopback"),
        IpNetworkRange.Parse("64:ff9b::/96", "nat64_well_known_prefix"),
        IpNetworkRange.Parse("64:ff9b:1::/48", "nat64_local_use"),
        IpNetworkRange.Parse("100::/64", "discard_only"),
        IpNetworkRange.Parse("2001::/23", "ietf_protocol_assignment"),
        IpNetworkRange.Parse("2001:db8::/32", "documentation"),
        IpNetworkRange.Parse("2002::/16", "deprecated_transition"),
        IpNetworkRange.Parse("3fff::/20", "documentation"),
        IpNetworkRange.Parse("5f00::/16", "segment_routing_sids"),
        IpNetworkRange.Parse("fc00::/7", "unique_local"),
        IpNetworkRange.Parse("fe80::/10", "link_local"),
        IpNetworkRange.Parse("ff00::/8", "multicast")
    ];

    public static bool IsPublicRoutable(IPAddress address)
    {
        return Classify(address).Allowed;
    }

    public static NetworkAddressClassification Classify(IPAddress address)
    {
        ArgumentNullException.ThrowIfNull(address);
        IPAddress normalized = address.IsIPv4MappedToIPv6
            ? address.MapToIPv4()
            : address;

        IpNetworkRange[] ranges = normalized.AddressFamily switch
        {
            AddressFamily.InterNetwork => BlockedIpv4Ranges,
            AddressFamily.InterNetworkV6 => BlockedIpv6Ranges,
            _ => []
        };
        if (ranges.Length == 0)
        {
            return new NetworkAddressClassification(false, "unsupported_address_family");
        }

        foreach (IpNetworkRange range in ranges)
        {
            if (range.Contains(normalized))
            {
                return new NetworkAddressClassification(false, range.ReasonCode);
            }
        }

        return new NetworkAddressClassification(true, "public_routable");
    }

    private sealed record IpNetworkRange(
        byte[] NetworkBytes,
        int PrefixLength,
        AddressFamily AddressFamily,
        string ReasonCode)
    {
        public static IpNetworkRange Parse(string value, string reasonCode)
        {
            string[] parts = value.Split('/', 2, StringSplitOptions.TrimEntries);
            if (parts.Length != 2
                || !IPAddress.TryParse(parts[0], out IPAddress? address)
                || !int.TryParse(parts[1], out int prefixLength))
            {
                throw new InvalidOperationException($"Invalid built-in network range: {value}");
            }

            int maximumPrefixLength = address.AddressFamily == AddressFamily.InterNetwork ? 32 : 128;
            if (prefixLength < 0 || prefixLength > maximumPrefixLength)
            {
                throw new InvalidOperationException($"Invalid prefix length in built-in network range: {value}");
            }

            return new IpNetworkRange(
                address.GetAddressBytes(),
                prefixLength,
                address.AddressFamily,
                reasonCode);
        }

        public bool Contains(IPAddress address)
        {
            if (address.AddressFamily != AddressFamily)
            {
                return false;
            }

            byte[] addressBytes = address.GetAddressBytes();
            int wholeBytes = PrefixLength / 8;
            int remainingBits = PrefixLength % 8;
            for (int index = 0; index < wholeBytes; index++)
            {
                if (addressBytes[index] != NetworkBytes[index])
                {
                    return false;
                }
            }

            if (remainingBits == 0)
            {
                return true;
            }

            int mask = 0xFF << (8 - remainingBits);
            return (addressBytes[wholeBytes] & mask) == (NetworkBytes[wholeBytes] & mask);
        }
    }
}

public sealed class SubscriptionUrlSourceReader : IUpstreamSourceReader
{
    private static readonly HashSet<string> AllowedServerSchemes = new(StringComparer.OrdinalIgnoreCase)
    {
        "vless",
        "vmess",
        "trojan",
        "ss"
    };

    private readonly ISsrfSafeHttpFetcher _fetcher;
    private readonly VpnMediatorOptions _options;

    public SubscriptionUrlSourceReader(
        ISsrfSafeHttpFetcher fetcher,
        IOptions<VpnMediatorOptions> options)
    {
        _fetcher = fetcher;
        _options = options.Value;
    }

    public string Kind => SourceKinds.SubscriptionUrl;

    public async Task<SourceReadResult> ReadAsync(
        UpstreamSource source,
        CancellationToken cancellationToken)
    {
        if (!Uri.TryCreate(source.Endpoint, UriKind.Absolute, out Uri? endpoint))
        {
            return SourceReadResult.Failed("endpoint_invalid");
        }

        Stopwatch stopwatch = Stopwatch.StartNew();
        FetchResult fetchResult;

        try
        {
            fetchResult = await _fetcher.FetchStringAsync(endpoint, cancellationToken);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception)
        {
            return SourceReadResult.Failed("fetch_failed");
        }
        finally
        {
            stopwatch.Stop();
        }

        if (!fetchResult.Success || fetchResult.Body is null)
        {
            return SourceReadResult.Failed(fetchResult.ErrorCode ?? "fetch_failed", stopwatch.ElapsedMilliseconds);
        }

        IReadOnlyList<string> decoded;

        try
        {
            decoded = SubscriptionCodec.DecodeServerLinks(fetchResult.Body);
        }
        catch (FormatException)
        {
            return SourceReadResult.Failed("base64_invalid", stopwatch.ElapsedMilliseconds);
        }

        List<string> accepted = [];
        List<string> errors = [];
        int filtered = 0;

        foreach (string link in decoded)
        {
            if (accepted.Count >= _options.ServerCatalogMaxLinksPerSourceRead)
            {
                filtered++;
                continue;
            }

            if (link.Length > 4096)
            {
                filtered++;
                errors.Add("uri_too_long");
                continue;
            }

            if (!Uri.TryCreate(link, UriKind.Absolute, out Uri? uri) || !AllowedServerSchemes.Contains(uri.Scheme))
            {
                filtered++;
                errors.Add("scheme_unsupported");
                continue;
            }

            if (string.IsNullOrWhiteSpace(uri.Host)
                || uri.Port is <= 0 or > 65535
                || string.IsNullOrWhiteSpace(uri.UserInfo))
            {
                filtered++;
                errors.Add("server_fields_invalid");
                continue;
            }

            if (IPAddress.TryParse(uri.Host, out IPAddress? address)
                && !NetworkAddressClassifier.IsPublicRoutable(address))
            {
                filtered++;
                errors.Add("server_destination_forbidden");
                continue;
            }

            accepted.Add(link);
        }

        if (accepted.Count == 0)
        {
            return SourceReadResult.Failed("servers_empty", stopwatch.ElapsedMilliseconds, filtered, errors.Distinct(StringComparer.Ordinal).ToArray());
        }

        return SourceReadResult.Successful(
            accepted,
            stopwatch.ElapsedMilliseconds,
            accepted.Select(x => new Uri(x).Scheme).Distinct(StringComparer.OrdinalIgnoreCase).OrderBy(x => x, StringComparer.Ordinal).ToArray(),
            filtered,
            errors.Distinct(StringComparer.Ordinal).ToArray());
    }
}

public static class CatalogFingerprint
{
    public static string ComputeContent(IEnumerable<string> serverLinks)
    {
        IEnumerable<string> normalized = serverLinks
            .Select(NormalizeTechnicalIdentity)
            .Distinct(StringComparer.Ordinal)
            .OrderBy(value => value, StringComparer.Ordinal);
        return Hash(normalized);
    }

    public static string ComputePresentation(IEnumerable<string> serverLinks)
    {
        return Hash(serverLinks);
    }

    public static string Compute(string serverLink)
    {
        return Hash([NormalizeTechnicalIdentity(serverLink)]);
    }

    public static string NormalizeTechnicalIdentity(string link)
    {
        int fragmentIndex = link.IndexOf('#', StringComparison.Ordinal);
        string withoutFragment = fragmentIndex >= 0 ? link[..fragmentIndex] : link;
        if (!Uri.TryCreate(withoutFragment, UriKind.Absolute, out Uri? uri))
        {
            return withoutFragment.Trim();
        }

        UriBuilder builder = new(uri)
        {
            Scheme = uri.Scheme.ToLowerInvariant(),
            Host = uri.Host.ToLowerInvariant(),
            Fragment = string.Empty
        };
        return builder.Uri.AbsoluteUri;
    }

    private static string Hash(IEnumerable<string> values)
    {
        string payload = string.Join("\n", values);
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(payload)))
            .ToLowerInvariant();
    }
}

public readonly record struct ServerPresentationDescriptor(
    string Country,
    bool HasWifiLabel,
    bool HasMobileInternetLabel);

public static class ServerPresentationClassifier
{
    private static readonly Regex WifiPattern = new(
        @"(?<![\p{L}\p{N}])(?:wi(?:[\s-]*fi)|вай(?:[\s-]*фай))(?![\p{L}\p{N}])",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant | RegexOptions.Compiled);

    private static readonly Regex MobileInternetPattern = new(
        @"(?<![\p{L}\p{N}])(?:(?:обход|от)\s+глушилок|глушилки|мобильный\s+интернет)(?![\p{L}\p{N}])",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant | RegexOptions.Compiled);

    public static (bool HasWifiLabel, bool HasMobileInternetLabel) Classify(
        string rawDisplayName)
    {
        string normalized = rawDisplayName.Normalize();
        return (
            WifiPattern.IsMatch(normalized),
            MobileInternetPattern.IsMatch(normalized));
    }

    public static int ConnectionTypePriority(string rawDisplayName)
    {
        (bool hasWifiLabel, bool hasMobileInternetLabel) = Classify(rawDisplayName);
        if (hasMobileInternetLabel)
        {
            return 0;
        }

        return hasWifiLabel ? 2 : 1;
    }
}

public static class CatalogPresentation
{
    public const int CurrentVersion = 5;

    private const string UnknownCountryLabel = "Неизвестно";
    private const string UnknownCountryIcon = "🌐";

    private static readonly IReadOnlyDictionary<string, string> BuiltInCountryAliases =
        new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["de"] = "Германия",
            ["germany"] = "Германия",
            ["германия"] = "Германия",
            ["🇩🇪"] = "Германия",
            ["pl"] = "Польша",
            ["poland"] = "Польша",
            ["польша"] = "Польша",
            ["🇵🇱"] = "Польша",
            ["nl"] = "Нидерланды",
            ["netherlands"] = "Нидерланды",
            ["нидерланды"] = "Нидерланды",
            ["🇳🇱"] = "Нидерланды",
            ["fi"] = "Финляндия",
            ["finland"] = "Финляндия",
            ["финляндия"] = "Финляндия",
            ["🇫🇮"] = "Финляндия",
            ["it"] = "Италия",
            ["italy"] = "Италия",
            ["италия"] = "Италия",
            ["🇮🇹"] = "Италия",
            ["ru"] = "Россия",
            ["russia"] = "Россия",
            ["россия"] = "Россия",
            ["🇷🇺"] = "Россия"
        };

    private static readonly IReadOnlyDictionary<string, string> CountryFlagByName =
        new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["Германия"] = "🇩🇪",
            ["Польша"] = "🇵🇱",
            ["Нидерланды"] = "🇳🇱",
            ["Финляндия"] = "🇫🇮",
            ["Италия"] = "🇮🇹",
            ["Россия"] = "🇷🇺"
        };

    public static IReadOnlyList<string> Deduplicate(IEnumerable<string> serverLinks)
    {
        return serverLinks
            .GroupBy(CatalogFingerprint.NormalizeTechnicalIdentity, StringComparer.Ordinal)
            .Select(group => group.OrderBy(value => value, StringComparer.Ordinal).First())
            .ToArray();
    }

    public static IReadOnlyList<string> Build(IEnumerable<string> serverLinks, int maxServers)
    {
        return Deduplicate(serverLinks)
            .OrderBy(link => ServerPresentationClassifier.ConnectionTypePriority(
                GetDisplayName(link)))
            .ThenBy(GetDisplayName, NaturalStringComparer.Instance)
            .ThenBy(GetProtocol, StringComparer.Ordinal)
            .ThenBy(CatalogFingerprint.NormalizeTechnicalIdentity, StringComparer.Ordinal)
            .Take(maxServers)
            .ToArray();
    }

    public static IReadOnlyList<string> RenumberInPublicationOrder(
        IEnumerable<string> serverLinks,
        IReadOnlyDictionary<string, string>? countryBySourceNumber = null)
    {
        return serverLinks
            .Select((link, index) => WithPublicationNumber(
                link,
                index + 1,
                countryBySourceNumber))
            .ToArray();
    }

    public static string GetDisplayName(string link)
    {
        int fragmentIndex = link.IndexOf('#', StringComparison.Ordinal);
        return fragmentIndex < 0
            ? string.Empty
            : Uri.UnescapeDataString(link[(fragmentIndex + 1)..]).Normalize();
    }

    private static string WithPublicationNumber(
        string link,
        int publicationNumber,
        IReadOnlyDictionary<string, string>? countryBySourceNumber)
    {
        int fragmentIndex = link.IndexOf('#', StringComparison.Ordinal);
        string technicalLink = fragmentIndex < 0 ? link : link[..fragmentIndex];
        string currentDisplayName = GetDisplayName(link).Trim();
        string country = ResolveCountry(currentDisplayName, countryBySourceNumber);
        (bool hasWifiLabel, bool hasMobileInternetLabel) =
            ServerPresentationClassifier.Classify(currentDisplayName);
        ServerPresentationDescriptor descriptor = new(
            country,
            hasWifiLabel,
            hasMobileInternetLabel);
        string displayName = Render(publicationNumber, descriptor);

        return $"{technicalLink}#{Uri.EscapeDataString(displayName)}";
    }


    private static string Render(
        int publicationNumber,
        ServerPresentationDescriptor descriptor)
    {
        StringBuilder builder = new();
        builder.Append(publicationNumber.ToString(CultureInfo.InvariantCulture));
        builder.Append(" | ");
        builder.Append(FormatCountry(descriptor.Country));
        if (descriptor.HasMobileInternetLabel)
        {
            builder.Append(" мобильный интернет");
        }
        if (descriptor.HasWifiLabel)
        {
            builder.Append(" вайфай");
        }
        return builder.ToString();
    }

    private static string ResolveCountry(
        string currentDisplayName,
        IReadOnlyDictionary<string, string>? countryBySourceNumber)
    {
        Match canonicalName = Regex.Match(
            currentDisplayName,
            @"^\s*\d+\s*\|\s*(?<country>[^|]+?)\s*$",
            RegexOptions.CultureInvariant);
        if (canonicalName.Success)
        {
            string candidate = canonicalName.Groups["country"].Value.Trim();
            if (TryNormalizeCountry(candidate, countryBySourceNumber, out string country))
            {
                return country;
            }
        }

        Match sourceNumberMatch = Regex.Match(
            currentDisplayName,
            @"^\s*(?<number>\d+)\s*\|",
            RegexOptions.CultureInvariant);
        if (sourceNumberMatch.Success
            && TryGetConfiguredCountry(
                sourceNumberMatch.Groups["number"].Value,
                countryBySourceNumber,
                out string configuredCountry))
        {
            return configuredCountry;
        }

        foreach ((string alias, string normalizedCountry) in BuiltInCountryAliases)
        {
            if (alias.Length <= 2 && alias.All(char.IsAsciiLetter))
            {
                continue;
            }
            if (currentDisplayName.Contains(alias, StringComparison.OrdinalIgnoreCase))
            {
                return normalizedCountry;
            }
        }

        return UnknownCountryLabel;
    }

    private static bool TryNormalizeCountry(
        string candidate,
        IReadOnlyDictionary<string, string>? countryBySourceNumber,
        out string country)
    {
        if (BuiltInCountryAliases.TryGetValue(candidate, out string? builtInCountry))
        {
            country = builtInCountry;
            return true;
        }

        foreach ((string alias, string normalizedCountry) in BuiltInCountryAliases)
        {
            if (alias.Length <= 2 && alias.All(char.IsAsciiLetter))
            {
                continue;
            }
            if (candidate.Contains(alias, StringComparison.OrdinalIgnoreCase))
            {
                country = normalizedCountry;
                return true;
            }
        }

        if (countryBySourceNumber is not null)
        {
            string? configuredCountry = countryBySourceNumber.Values
                .Where(value => !string.IsNullOrWhiteSpace(value))
                .Select(value => value.Trim())
                .FirstOrDefault(value => string.Equals(
                    value,
                    candidate,
                    StringComparison.OrdinalIgnoreCase));
            if (configuredCountry is not null)
            {
                country = configuredCountry;
                return true;
            }
        }

        country = string.Empty;
        return false;
    }

    private static string FormatCountry(string country)
    {
        if (CountryFlagByName.TryGetValue(country, out string? flag))
        {
            return string.Concat(flag, " ", country);
        }

        return string.Equals(country, UnknownCountryLabel, StringComparison.Ordinal)
            ? string.Concat(UnknownCountryIcon, " ", UnknownCountryLabel)
            : country;
    }

    private static bool TryGetConfiguredCountry(
        string sourceNumber,
        IReadOnlyDictionary<string, string>? countryBySourceNumber,
        out string country)
    {
        if (countryBySourceNumber is not null
            && int.TryParse(
                sourceNumber,
                NumberStyles.None,
                CultureInfo.InvariantCulture,
                out int parsedSourceNumber))
        {
            string normalizedSourceNumber = parsedSourceNumber.ToString(
                CultureInfo.InvariantCulture);
            if (countryBySourceNumber.TryGetValue(
                    normalizedSourceNumber,
                    out string? configuredCountry)
                && !string.IsNullOrWhiteSpace(configuredCountry))
            {
                country = configuredCountry.Trim();
                return true;
            }
        }

        country = string.Empty;
        return false;
    }

    private static string GetProtocol(string link)
    {
        return Uri.TryCreate(link, UriKind.Absolute, out Uri? uri)
            ? uri.Scheme.ToLowerInvariant()
            : string.Empty;
    }
}

public sealed class NaturalStringComparer : IComparer<string>
{
    public static NaturalStringComparer Instance { get; } = new();

    public int Compare(string? left, string? right)
    {
        if (ReferenceEquals(left, right))
        {
            return 0;
        }
        if (left is null)
        {
            return -1;
        }
        if (right is null)
        {
            return 1;
        }

        MatchCollection leftParts = Regex.Matches(left.Normalize(), @"\d+|\D+");
        MatchCollection rightParts = Regex.Matches(right.Normalize(), @"\d+|\D+");
        int count = Math.Min(leftParts.Count, rightParts.Count);
        for (int index = 0; index < count; index++)
        {
            string leftPart = leftParts[index].Value;
            string rightPart = rightParts[index].Value;
            int comparison;
            if (long.TryParse(leftPart, NumberStyles.None, CultureInfo.InvariantCulture, out long leftNumber)
                && long.TryParse(rightPart, NumberStyles.None, CultureInfo.InvariantCulture, out long rightNumber))
            {
                comparison = leftNumber.CompareTo(rightNumber);
            }
            else
            {
                comparison = StringComparer.InvariantCultureIgnoreCase.Compare(leftPart, rightPart);
            }
            if (comparison != 0)
            {
                return comparison;
            }
        }

        int lengthComparison = leftParts.Count.CompareTo(rightParts.Count);
        return lengthComparison != 0
            ? lengthComparison
            : StringComparer.Ordinal.Compare(left, right);
    }
}

public static class CatalogAnomalyGuard
{
    public static bool ShouldReject(
        IReadOnlyList<string>? previous,
        IReadOnlyList<string> candidate)
    {
        if (previous is null || previous.Count < 10 || candidate.Count == 0)
        {
            return false;
        }

        HashSet<string> previousIds = previous
            .Select(CatalogFingerprint.NormalizeTechnicalIdentity)
            .ToHashSet(StringComparer.Ordinal);
        int retained = candidate
            .Select(CatalogFingerprint.NormalizeTechnicalIdentity)
            .Count(previousIds.Contains);
        return retained < Math.Ceiling(previousIds.Count * 0.2);
    }
}

public sealed class UpstreamSourceReaderRegistry : IUpstreamSourceReaderRegistry
{
    private readonly Dictionary<string, IUpstreamSourceReader> _readers;

    public UpstreamSourceReaderRegistry(IEnumerable<IUpstreamSourceReader> readers)
    {
        _readers = readers.ToDictionary(x => x.Kind, StringComparer.Ordinal);
    }

    public bool TryGet(string kind, out IUpstreamSourceReader? reader)
    {
        return _readers.TryGetValue(kind, out reader);
    }
}

public sealed class CatalogRefreshWorker : BackgroundService
{
    private readonly SqliteMediatorRepository _repository;
    private readonly IUpstreamSourceReaderRegistry _readerRegistry;
    private readonly VpnMediatorOptions _options;
    private readonly ILogger<CatalogRefreshWorker> _logger;

    public CatalogRefreshWorker(
        SqliteMediatorRepository repository,
        IUpstreamSourceReaderRegistry readerRegistry,
        IOptions<VpnMediatorOptions> options,
        ILogger<CatalogRefreshWorker> logger)
    {
        _repository = repository;
        _readerRegistry = readerRegistry;
        _options = options.Value;
        _logger = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        int consecutiveFailures = 0;

        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                CatalogRefreshResult result = await _repository.RefreshCatalogAsync(
                    _readerRegistry,
                    DateTimeOffset.UtcNow,
                    stoppingToken);

                consecutiveFailures = 0;
                _logger.LogInformation(
                    "Catalog refresh completed with state {State} and {ServerCount} servers.",
                    result.State,
                    result.ServerCount);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                consecutiveFailures++;
                _logger.LogError(
                    "Catalog refresh failed with {ExceptionType}; consecutive failures: {FailureCount}.",
                    exception.GetType().FullName,
                    consecutiveFailures);

                if (consecutiveFailures >= _options.CriticalWorkerConsecutiveFailureLimit)
                {
                    throw new InvalidOperationException(
                        "Catalog refresh worker exceeded its consecutive failure limit.");
                }
            }

            await Task.Delay(
                TimeSpan.FromSeconds(_options.ServerCatalogRefreshIntervalSeconds),
                stoppingToken);
        }
    }
}

public sealed record ProtectedDeviceCredential(
    string KeyId,
    string Nonce,
    string Ciphertext,
    string Tag);

public interface IDeviceCredentialProtector
{
    ProtectedDeviceCredential Protect(string rawCredential);

    string Unprotect(ProtectedDeviceCredential protectedCredential);

    bool NeedsReencryption(ProtectedDeviceCredential protectedCredential);
}

public sealed class DeviceCredentialKeyUnavailableException : CryptographicException
{
    public DeviceCredentialKeyUnavailableException(string keyId)
        : base($"Device credential key id '{keyId}' is unavailable.")
    {
    }
}

public sealed class AesGcmDeviceCredentialProtector : IDeviceCredentialProtector
{
    private readonly string _currentKeyId;
    private readonly IReadOnlyDictionary<string, byte[]> _keys;

    public AesGcmDeviceCredentialProtector(
        IOptions<VpnMediatorOptions> options,
        IWebHostEnvironment environment)
    {
        VpnMediatorOptions value = options.Value;
        _currentKeyId = TextSanitizer.NullIfWhiteSpace(value.DeviceCredentialProtectionKeyId)
            ?? "v1";
        Dictionary<string, byte[]> keys = new(StringComparer.Ordinal);
        string? configuredCurrent = TextSanitizer.NullIfWhiteSpace(
            value.DeviceCredentialProtectionKey);

        if (configuredCurrent is null)
        {
            if (!environment.IsDevelopment())
            {
                throw new InvalidOperationException(
                    "VpnMediator:DeviceCredentialProtectionKey is required outside development.");
            }

            keys[_currentKeyId] = SHA256.HashData(
                Encoding.UTF8.GetBytes($"{value.DeviceTokenHashKey}:device-credential"));
        }
        else
        {
            keys[_currentKeyId] = DecodeKey(
                configuredCurrent,
                "DeviceCredentialProtectionKey");
        }

        string? previousId = TextSanitizer.NullIfWhiteSpace(
            value.PreviousDeviceCredentialProtectionKeyId);
        string? previousKey = TextSanitizer.NullIfWhiteSpace(
            value.PreviousDeviceCredentialProtectionKey);

        if (previousId is not null && previousKey is not null)
        {
            keys[previousId] = DecodeKey(
                previousKey,
                "PreviousDeviceCredentialProtectionKey");
        }

        _keys = keys;
    }

    public ProtectedDeviceCredential Protect(string rawCredential)
    {
        byte[] nonce = RandomNumberGenerator.GetBytes(12);
        byte[] plaintext = Encoding.UTF8.GetBytes(rawCredential);
        byte[] ciphertext = new byte[plaintext.Length];
        byte[] tag = new byte[16];
        byte[] associatedData = Encoding.UTF8.GetBytes(_currentKeyId);

        using AesGcm aes = new(_keys[_currentKeyId], 16);
        aes.Encrypt(nonce, plaintext, ciphertext, tag, associatedData);

        return new ProtectedDeviceCredential(
            _currentKeyId,
            Convert.ToBase64String(nonce),
            Convert.ToBase64String(ciphertext),
            Convert.ToBase64String(tag));
    }

    public string Unprotect(ProtectedDeviceCredential protectedCredential)
    {
        if (!_keys.TryGetValue(protectedCredential.KeyId, out byte[]? key))
        {
            throw new DeviceCredentialKeyUnavailableException(protectedCredential.KeyId);
        }

        byte[] nonce = Convert.FromBase64String(protectedCredential.Nonce);
        byte[] ciphertext = Convert.FromBase64String(protectedCredential.Ciphertext);
        byte[] tag = Convert.FromBase64String(protectedCredential.Tag);
        byte[] plaintext = new byte[ciphertext.Length];
        byte[] associatedData = Encoding.UTF8.GetBytes(protectedCredential.KeyId);

        using AesGcm aes = new(key, 16);
        aes.Decrypt(nonce, ciphertext, tag, plaintext, associatedData);
        return Encoding.UTF8.GetString(plaintext);
    }

    public bool NeedsReencryption(ProtectedDeviceCredential protectedCredential)
    {
        return !string.Equals(
            protectedCredential.KeyId,
            _currentKeyId,
            StringComparison.Ordinal);
    }

    private static byte[] DecodeKey(string value, string name)
    {
        byte[] key = Convert.FromBase64String(value);

        if (key.Length != 32)
        {
            throw new InvalidOperationException(
                $"VpnMediator:{name} must be a base64 encoded 32-byte key.");
        }

        return key;
    }
}

public interface IEndpointProtector
{
    string Protect(string endpoint);

    string Unprotect(string protectedEndpoint);

    bool NeedsReencryption(string protectedEndpoint);
}

public interface ISsrfSafeHttpFetcher
{
    Task<FetchResult> FetchStringAsync(Uri endpoint, CancellationToken cancellationToken);

    Task<UriValidationResult> ValidateUriAsync(Uri endpoint, CancellationToken cancellationToken);
}

public interface IHostAddressResolver
{
    Task<IPAddress[]> GetHostAddressesAsync(string host, CancellationToken cancellationToken);
}

public sealed class DnsHostAddressResolver : IHostAddressResolver
{
    public Task<IPAddress[]> GetHostAddressesAsync(string host, CancellationToken cancellationToken)
    {
        return Dns.GetHostAddressesAsync(host, cancellationToken);
    }
}

public interface IUpstreamSourceReader
{
    string Kind { get; }

    Task<SourceReadResult> ReadAsync(UpstreamSource source, CancellationToken cancellationToken);
}

public interface IUpstreamSourceReaderRegistry
{
    bool TryGet(string kind, out IUpstreamSourceReader? reader);
}

public sealed record CreateSubscriptionCommand(
    string? ExternalRequestId,
    string? CustomerReference,
    string? Note,
    Guid? PublicGuid,
    EntitlementUpdateRequest Entitlement);

public sealed record CreateSubscriptionResult(Guid PublicGuid, bool AlreadyExisted);

public sealed record EntitlementOperationRequest(
    string OperationId,
    string OperationType,
    long ExpectedVersion,
    string Status,
    DateTimeOffset? ValidUntilUtc,
    int MaxDeviceTokens);

public sealed record EntitlementOperationResult(
    EntitlementOperationStatus Status,
    string OperationId,
    string RequestFingerprint,
    Guid? PublicGuid,
    string? OperationType,
    long? ExpectedVersion,
    long? ResultVersion,
    string? ResultStatus,
    DateTimeOffset? ResultValidUntilUtc,
    int? ResultMaxDeviceTokens,
    DateTimeOffset? AppliedAtUtc,
    int ActiveDeviceTokens = 0)
{
    public static EntitlementOperationResult NotFound(string operationId, string fingerprint) =>
        new(EntitlementOperationStatus.SubscriptionNotFound, operationId, fingerprint, null, null,
            null, null, null, null, null, null);

    public static EntitlementOperationResult VersionConflict(
        string operationId,
        string fingerprint,
        long? currentVersion) =>
        new(EntitlementOperationStatus.VersionConflict, operationId, fingerprint, null, null,
            currentVersion, null, null, null, null, null);

    public static EntitlementOperationResult DeviceLimitDecrease(
        string operationId,
        string fingerprint,
        long currentVersion) =>
        new(EntitlementOperationStatus.DeviceLimitDecreaseNotAllowed, operationId, fingerprint,
            null, null, currentVersion, null, null, null, null, null);

    public static EntitlementOperationResult ActiveDevicesConflict(
        string operationId,
        string fingerprint,
        long currentVersion,
        int activeDeviceTokens) =>
        new(EntitlementOperationStatus.ActiveDevicesExceedNewLimit, operationId, fingerprint,
            null, null, currentVersion, null, null, null, null, null, activeDeviceTokens);
}

public enum EntitlementOperationStatus
{
    Applied,
    AlreadyApplied,
    IdempotencyConflict,
    VersionConflict,
    SubscriptionNotFound,
    DeviceLimitDecreaseNotAllowed,
    ActiveDevicesExceedNewLimit
}

public sealed record EntitlementUpdateRequest(
    long Version,
    string Status,
    DateTimeOffset? ValidUntilUtc,
    int MaxDeviceTokens);

public sealed record EntitlementDetails(
    Guid PublicGuid,
    long Version,
    string Status,
    DateTimeOffset? ValidUntilUtc,
    int MaxDeviceTokens,
    DateTimeOffset UpdatedAtUtc);

public sealed record EntitlementUpdateResult(
    EntitlementUpdateStatus Status,
    long? CurrentVersion,
    int ActiveDeviceTokens = 0);

public enum EntitlementUpdateStatus
{
    Applied,
    AlreadyApplied,
    StaleVersionRejected,
    SubscriptionNotFound,
    InvalidState,
    DeviceLimitDecreaseNotAllowed,
    ActiveDevicesExceedNewLimit
}

public sealed record CreateDeviceTokenRequest(string? DisplayName, string? RequestedPlatform = null);

public sealed record TransferDeviceTokenRequest(string OperationId, string RequestedPlatform);

public sealed record DeviceCredentialResult(
    string Status,
    string? PublicId,
    string? DisplayName,
    string? ConnectionUrl,
    string? ErrorCode)
{
    public static DeviceCredentialResult Available(
        string publicId,
        string displayName,
        string connectionUrl)
    {
        return new DeviceCredentialResult(
            "available",
            publicId,
            displayName,
            connectionUrl,
            null);
    }

    public static DeviceCredentialResult NotFound()
    {
        return new DeviceCredentialResult(
            "not_found",
            null,
            null,
            null,
            "device_token_not_found");
    }

    public static DeviceCredentialResult Invalid(string errorCode)
    {
        return new DeviceCredentialResult("invalid", null, null, null, errorCode);
    }
}

public sealed record MediatorCapacitySnapshot(
    int ActiveSubscriptions,
    int ActiveDevices,
    DateTimeOffset CapturedAtUtc);

public sealed record DeviceTokenCreateResult(
    string Status,
    string? PublicId,
    string? DisplayName,
    string? ConnectionUrl,
    int ActiveDeviceTokens,
    int PendingDeviceTokens,
    int OccupiedSlots,
    int MaxDeviceTokens,
    DateTimeOffset? PendingExpiresAtUtc,
    bool PreviousPendingReplaced,
    string? ErrorCode)
{
    public static DeviceTokenCreateResult Created(
        string publicId,
        string displayName,
        string connectionUrl,
        int activeDeviceTokens,
        int pendingDeviceTokens,
        int occupiedSlots,
        int maxDeviceTokens,
        DateTimeOffset pendingExpiresAtUtc,
        bool previousPendingReplaced)
    {
        return new DeviceTokenCreateResult(
            "created",
            publicId,
            displayName,
            connectionUrl,
            activeDeviceTokens,
            pendingDeviceTokens,
            occupiedSlots,
            maxDeviceTokens,
            pendingExpiresAtUtc,
            previousPendingReplaced,
            null);
    }

    public static DeviceTokenCreateResult Existing(
        string publicId,
        string displayName,
        string connectionUrl,
        int activeDeviceTokens,
        int pendingDeviceTokens,
        int occupiedSlots,
        int maxDeviceTokens,
        DateTimeOffset? pendingExpiresAtUtc)
    {
        return new DeviceTokenCreateResult(
            "existing",
            publicId,
            displayName,
            connectionUrl,
            activeDeviceTokens,
            pendingDeviceTokens,
            occupiedSlots,
            maxDeviceTokens,
            pendingExpiresAtUtc,
            false,
            null);
    }

    public static DeviceTokenCreateResult NotFound()
    {
        return new DeviceTokenCreateResult("not_found", null, null, null, 0, 0, 0, 0, null, false, "subscription_not_found");
    }

    public static DeviceTokenCreateResult LimitReached(
        int activeDeviceTokens,
        int pendingDeviceTokens,
        int occupiedSlots,
        int maxDeviceTokens)
    {
        return new DeviceTokenCreateResult(
            "limit_reached",
            null,
            null,
            null,
            activeDeviceTokens,
            pendingDeviceTokens,
            occupiedSlots,
            maxDeviceTokens,
            null,
            false,
            "device_limit_reached");
    }

    public static DeviceTokenCreateResult Invalid(string errorCode)
    {
        return new DeviceTokenCreateResult("invalid", null, null, null, 0, 0, 0, 0, null, false, errorCode);
    }
}

public sealed record MigrationState(
    int CurrentBinaryVersion,
    int DatabaseMaxVersion,
    IReadOnlyList<int> MissingRequiredVersions,
    IReadOnlyList<int> UnknownVersions,
    bool IsAhead,
    bool IsCurrent)
{
    public static MigrationState Empty(
        int currentBinaryVersion,
        IReadOnlyList<int> requiredVersions)
    {
        return new MigrationState(
            currentBinaryVersion,
            0,
            requiredVersions,
            [],
            false,
            false);
    }
}

public sealed record DeviceTokenListItem(
    string PublicId,
    string DisplayName,
    DateTimeOffset CreatedAtUtc,
    DateTimeOffset? ActivatedAtUtc,
    DateTimeOffset? LastUsedAtUtc,
    DateTimeOffset? RevokedAtUtc,
    DateTimeOffset? PendingExpiresAtUtc,
    string? RevocationReason,
    string? DeviceType,
    string? Platform,
    string? DetectedModel,
    string? DetectionSource,
    DateTimeOffset? FirstFetchedAtUtc,
    string State,
    int FeedPolicyVersion = DeviceFeedPolicyVersions.PlatformHeuristic,
    string FeedPolicyMode = DeviceFeedPolicyModes.Legacy,
    string BindingState = DeviceFeedBindingStates.Grandfathered,
    string? BoundPlatform = null,
    string? BoundClientFamily = null,
    DateTimeOffset? BoundAtUtc = null,
    bool IdentityBound = false,
    string? IdentitySource = null,
    DateTimeOffset? LastIdentitySeenAtUtc = null,
    DateTimeOffset? LastIdentityMismatchAtUtc = null,
    DateTimeOffset? LastTransferAtUtc = null,
    int TransferCount = 0,
    int RiskScore = 0,
    string AccessChannel = "device_link",
    string? DeviceState = null);

public sealed record DeviceTokenRevokeAllResult(bool SubscriptionFound, int RevokedCount);

public sealed record TokenSubscriptionAccessResult(
    bool SubscriptionFound,
    bool Allowed,
    UserFacingStatus Status,
    int ActiveDeviceTokens,
    int MaxDeviceTokens,
    string? PolicyReasonCode = null,
    string? ExpectedPlatform = null,
    DateTimeOffset? ValidUntilUtc = null)
{
    public static TokenSubscriptionAccessResult NotFound()
    {
        return new TokenSubscriptionAccessResult(false, false, UserFacingStatus.SubscriptionNotFound, 0, 0);
    }

    public static TokenSubscriptionAccessResult Forbidden(UserFacingStatus status)
    {
        return new TokenSubscriptionAccessResult(true, false, status, 0, 0);
    }

    public static TokenSubscriptionAccessResult Forbidden(
        UserFacingStatus status,
        int activeDeviceTokens,
        int maxDeviceTokens,
        string? policyReasonCode = null,
        string? expectedPlatform = null)
    {
        return new TokenSubscriptionAccessResult(
            true,
            false,
            status,
            activeDeviceTokens,
            maxDeviceTokens,
            policyReasonCode,
            expectedPlatform);
    }

    public static TokenSubscriptionAccessResult Permit(
        int activeDeviceTokens,
        int maxDeviceTokens,
        DateTimeOffset? validUntilUtc = null)
    {
        return new TokenSubscriptionAccessResult(
            true,
            true,
            UserFacingStatus.Allowed,
            activeDeviceTokens,
            maxDeviceTokens,
            ValidUntilUtc: validUntilUtc);
    }
}

public enum UserFacingStatus
{
    Allowed,
    SubscriptionNotFound,
    SubscriptionDisabled,
    SubscriptionExpired,
    DeviceTokenInvalid,
    DeviceTokenRevoked,
    DeviceTokenExpired,
    DeviceLimitReached,
    DeviceIdentityRequired,
    DeviceTransferRequired,
    LegacyLinkDisabled,
    ServersUnavailable,
    DeviceProvisioningUnavailable
}

public static class DeviceTokenStates
{
    public const string Pending = "pending";
    public const string Active = "active";
    public const string ExpiredPending = "expired_pending";
    public const string Revoked = "revoked";
}

public static class DeviceTokenState
{
    public static string FromColumns(
        DateTimeOffset? activatedAtUtc,
        DateTimeOffset? revokedAtUtc,
        DateTimeOffset? pendingExpiresAtUtc,
        DateTimeOffset now)
    {
        if (revokedAtUtc is not null)
        {
            return DeviceTokenStates.Revoked;
        }

        if (activatedAtUtc is not null)
        {
            return DeviceTokenStates.Active;
        }

        if (pendingExpiresAtUtc is not null && pendingExpiresAtUtc <= now)
        {
            return DeviceTokenStates.ExpiredPending;
        }

        return DeviceTokenStates.Pending;
    }
}

public sealed record CreateSourceRequest(string Name, string Kind, string Endpoint);

public sealed record SourceDetails(
    long Id,
    string Name,
    string Kind,
    string State,
    int SortOrder,
    DateTimeOffset? LastTestedAtUtc,
    DateTimeOffset? LastSuccessfulRefreshAtUtc,
    DateTimeOffset? LastFailedRefreshAtUtc,
    string? LastErrorCode);

public sealed record UpstreamSource(
    long Id,
    string Name,
    string Kind,
    string Endpoint,
    string State,
    int SortOrder);

public sealed record SourceReadResult(
    bool Success,
    IReadOnlyList<string> ServerLinks,
    long ResponseTimeMs,
    IReadOnlyList<string> AcceptedSchemes,
    int FilteredCount,
    IReadOnlyList<string> ValidationErrors,
    string? ErrorCode)
{
    public static SourceReadResult Successful(
        IReadOnlyList<string> serverLinks,
        long responseTimeMs,
        IReadOnlyList<string> acceptedSchemes,
        int filteredCount,
        IReadOnlyList<string> validationErrors)
    {
        return new SourceReadResult(true, serverLinks, responseTimeMs, acceptedSchemes, filteredCount, validationErrors, null);
    }

    public static SourceReadResult Failed(
        string errorCode,
        long responseTimeMs = 0,
        int filteredCount = 0,
        IReadOnlyList<string>? validationErrors = null)
    {
        return new SourceReadResult(false, [], responseTimeMs, [], filteredCount, validationErrors ?? [], errorCode);
    }
}

public sealed record SourceTestResult(
    long SourceId,
    bool Success,
    long ResponseTimeMs,
    int ServerCount,
    IReadOnlyList<string> AcceptedSchemes,
    int FilteredCount,
    IReadOnlyList<string> ValidationErrors,
    string? ErrorCode)
{
    public static SourceTestResult NotFound(long sourceId)
    {
        return new SourceTestResult(sourceId, false, 0, 0, [], 0, [], "source_not_found");
    }
}

public sealed record CatalogRefreshResult(
    string State,
    int ServerCount,
    IReadOnlyList<CatalogSourceRefreshResult> Sources);

public sealed record CatalogSourceRefreshResult(
    long SourceId,
    string Status,
    int ServerCount,
    long? SnapshotVersion,
    string? ErrorCode)
{
    public static CatalogSourceRefreshResult Succeeded(long sourceId, int serverCount)
    {
        return new CatalogSourceRefreshResult(sourceId, "refreshed", serverCount, null, null);
    }

    public static CatalogSourceRefreshResult UsedCached(long sourceId, long version, string errorCode)
    {
        return new CatalogSourceRefreshResult(sourceId, "cached", 0, version, errorCode);
    }

    public static CatalogSourceRefreshResult Failed(long sourceId, string errorCode)
    {
        return new CatalogSourceRefreshResult(sourceId, "failed", 0, null, errorCode);
    }
}

public sealed record CatalogStatus(
    long? PublishedVersion,
    DateTimeOffset? PublishedAtUtc,
    DateTimeOffset? DataAsOfUtc,
    string State,
    int ServerCount,
    string? ContentFingerprint,
    string? PresentationFingerprint,
    int? PresentationVersion,
    IReadOnlyList<SourceDetails> Sources);

public sealed record ReadinessStatus(
    string Status,
    string? Reason,
    string CatalogState,
    int ServerCount,
    DateTimeOffset? DataAsOfUtc,
    int HttpStatusCode)
{
    public static ReadinessStatus Ready(
        int serverCount,
        DateTimeOffset dataAsOfUtc)
    {
        return new ReadinessStatus(
            "ready",
            null,
            PublishedSnapshotStates.Fresh,
            serverCount,
            dataAsOfUtc,
            StatusCodes.Status200OK);
    }

    public static ReadinessStatus Degraded(
        string reason,
        int serverCount,
        DateTimeOffset dataAsOfUtc)
    {
        return new ReadinessStatus(
            "degraded",
            reason,
            PublishedSnapshotStates.Stale,
            serverCount,
            dataAsOfUtc,
            StatusCodes.Status200OK);
    }

    public static ReadinessStatus NotReady(
        string reason,
        string catalogState,
        int serverCount,
        DateTimeOffset? dataAsOfUtc)
    {
        return new ReadinessStatus(
            "not_ready",
            reason,
            catalogState,
            serverCount,
            dataAsOfUtc,
            StatusCodes.Status503ServiceUnavailable);
    }
}

public sealed record PublishedSnapshot(
    long Version,
    DateTimeOffset CreatedAtUtc,
    DateTimeOffset DataAsOfUtc,
    string State,
    IReadOnlyList<string> ServerLinks,
    string SourceVersionsPayload,
    string? ContentFingerprint,
    string? PresentationFingerprint,
    int PresentationVersion);

public sealed record FetchResult(bool Success, string? Body, string? ErrorCode)
{
    public static FetchResult Succeeded(string body)
    {
        return new FetchResult(true, body, null);
    }

    public static FetchResult Failed(string errorCode)
    {
        return new FetchResult(false, null, errorCode);
    }
}

public sealed record UriValidationResult(bool IsAllowed, string? ErrorCode)
{
    public static UriValidationResult Allowed()
    {
        return new UriValidationResult(true, null);
    }

    public static UriValidationResult Blocked(string errorCode)
    {
        return new UriValidationResult(false, errorCode);
    }
}

internal sealed record EndpointValidationResult(
    bool IsAllowed,
    IPAddress? Address,
    string? ErrorCode)
{
    public static EndpointValidationResult Allowed(IPAddress address)
    {
        return new EndpointValidationResult(true, address, null);
    }

    public static EndpointValidationResult Blocked(string errorCode)
    {
        return new EndpointValidationResult(false, null, errorCode);
    }
}

public static class EntitlementStatuses
{
    public const string Active = "active";
    public const string Disabled = "disabled";
    public const string Expired = "expired";

    public static readonly string[] All = [Active, Disabled, Expired];
}

public static class SourceStates
{
    public const string Draft = "draft";
    public const string Tested = "tested";
    public const string Enabled = "enabled";
    public const string Degraded = "degraded";
    public const string Disabled = "disabled";
    public const string Revoked = "revoked";

    public static readonly string[] All = [Draft, Tested, Enabled, Degraded, Disabled, Revoked];
}

public static class SourceKinds
{
    public const string SubscriptionUrl = "subscription_url";
    public const string StaticServers = "static_servers";
    public const string ProviderApi = "provider_api";
    public const string OwnNodes = "own_nodes";
    public const string ConfigFile = "config_file";
    public const string AnotherMediator = "another_mediator";
}

public static class PublishedSnapshotStates
{
    public const string Fresh = "fresh";
    public const string Stale = "stale";
    public const string Unavailable = "unavailable";
}

public static class SourceValidator
{
    public static void ValidateCreate(CreateSourceRequest request)
    {
        if (string.IsNullOrWhiteSpace(request.Name))
        {
            throw new InvalidOperationException("Source name is required.");
        }

        if (request.Kind != SourceKinds.SubscriptionUrl)
        {
            throw new InvalidOperationException("Only subscription_url sources are implemented in this release.");
        }

        if (!Uri.TryCreate(request.Endpoint, UriKind.Absolute, out _))
        {
            throw new InvalidOperationException("Source endpoint must be an absolute URL.");
        }
    }
}

public static class DeviceIssuanceKeyValidator
{
    private const string Prefix = "device-issuance:";

    public static bool TryNormalize(string? value, out string? normalized)
    {
        if (value is null)
        {
            normalized = null;
            return true;
        }

        string candidate = value.Trim();

        if (!candidate.StartsWith(Prefix, StringComparison.Ordinal)
            || candidate.Length is < 24 or > 128)
        {
            normalized = null;
            return false;
        }

        ReadOnlySpan<char> suffix = candidate.AsSpan(Prefix.Length);

        if (suffix.Length is < 8 or > 96)
        {
            normalized = null;
            return false;
        }

        foreach (char character in suffix)
        {
            if (!char.IsAsciiLetterOrDigit(character) && character is not '-' and not '_')
            {
                normalized = null;
                return false;
            }
        }

        normalized = candidate;
        return true;
    }
}

public static class TokenSecretGenerator
{
    public static string CreateSecret()
    {
        return Base64Url.Encode(RandomNumberGenerator.GetBytes(32));
    }
}

public static class DeviceTokenHasher
{
    public static string Hash(string rawSecret, string signingSecret)
    {
        using HMACSHA256 hmac = new(Encoding.UTF8.GetBytes(signingSecret));
        return Base64Url.Encode(hmac.ComputeHash(Encoding.UTF8.GetBytes(rawSecret)));
    }

    public static bool Verify(string rawSecret, string expectedHash, string signingSecret)
    {
        string actual = Hash(rawSecret, signingSecret);
        byte[] actualBytes = Encoding.UTF8.GetBytes(actual);
        byte[] expectedBytes = Encoding.UTF8.GetBytes(expectedHash);

        return actualBytes.Length == expectedBytes.Length
            && CryptographicOperations.FixedTimeEquals(actualBytes, expectedBytes);
    }
}

internal sealed record DeviceFeedPolicySeed(
    int PolicyVersion,
    string PolicyMode,
    string BindingState,
    string? BoundPlatform,
    string? BoundClientFamily,
    DateTimeOffset? BoundAtUtc,
    string? BoundIdentityHash,
    string? BoundIdentityKeyId,
    string? BoundIdentitySource,
    DateTimeOffset? LastIdentitySeenAtUtc,
    DateTimeOffset? LastIdentityMismatchAtUtc,
    DateTimeOffset? LastTransferAtUtc,
    int TransferCount);

internal sealed record ExistingDeviceCredential(
    long Id,
    string PublicId,
    string DisplayName,
    DateTimeOffset? PendingExpiresAtUtc,
    DateTimeOffset? RevokedAtUtc,
    string? IssuanceKey,
    string? IssuanceRequestHash,
    string? RequestedPlatform,
    ProtectedDeviceCredential? ProtectedCredential);

internal sealed record SubscriptionIdentity(long Id, Guid PublicGuid);

internal sealed record EntitlementMirror(
    long Version,
    string Status,
    DateTimeOffset? ValidUntilUtc,
    int MaxDeviceTokens);

internal sealed record SubscriptionRuntimeState(
    long Version,
    string Status,
    DateTimeOffset? ValidUntilUtc,
    int MaxDeviceTokens);

internal sealed record SourceSnapshot(
    long Version,
    DateTimeOffset CreatedAtUtc,
    IReadOnlyList<string> ServerLinks);
