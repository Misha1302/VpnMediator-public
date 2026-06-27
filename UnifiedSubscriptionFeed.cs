using System.Security.Cryptography;
using Microsoft.Data.Sqlite;
using Microsoft.Extensions.Options;

public static class SubscriptionFeedCredentialStatuses
{
    public const string Created = "created";
    public const string Existing = "existing";
    public const string NotFound = "not_found";
    public const string Invalid = "invalid";
}

public static class UnifiedDeviceStates
{
    public const string Provisioning = "provisioning";
    public const string Active = "active";
    public const string ProvisioningFailed = "provisioning_failed";
    public const string Disabled = "disabled";
}

public sealed record SubscriptionFeedCredentialResult(
    string Status,
    string? ConnectionUrl,
    bool Created,
    string? ErrorCode = null);

public sealed record UnifiedFeedDeviceResolution(
    TokenSubscriptionAccessResult Access,
    string? DevicePublicId,
    bool Created,
    bool RequiresProvisioning)
{
    public static UnifiedFeedDeviceResolution Forbidden(
        TokenSubscriptionAccessResult access)
    {
        return new UnifiedFeedDeviceResolution(access, null, false, false);
    }
}

public sealed record UnifiedDeviceEnableResult(
    string Status,
    string? ErrorCode,
    int OccupiedSlots,
    int MaxDeviceTokens);

public interface ISubscriptionFeedLinkFactory
{
    string CreateSubscriptionFeedLink(
        HttpContext httpContext,
        Guid publicGuid,
        string secret);
}

public sealed class SubscriptionFeedLinkFactory : ISubscriptionFeedLinkFactory
{
    private readonly VpnMediatorOptions _options;

    public SubscriptionFeedLinkFactory(IOptions<VpnMediatorOptions> options)
    {
        _options = options.Value;
    }

    public string CreateSubscriptionFeedLink(
        HttpContext httpContext,
        Guid publicGuid,
        string secret)
    {
        string publicBaseUrl = ProgramUrlHelpers.GetPublicBaseUrl(httpContext, _options);
        return $"{publicBaseUrl}/sub/{publicGuid:D}/feed?token={Uri.EscapeDataString(secret)}";
    }
}

public sealed partial class SqliteMediatorRepository
{
    public async Task<SubscriptionFeedCredentialResult> EnsureSubscriptionFeedCredentialAsync(
        Guid publicGuid,
        HttpContext httpContext,
        ISubscriptionFeedLinkFactory linkFactory,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await using SqliteTransaction transaction =
                connection.BeginTransaction(deferred: false);
            SubscriptionIdentity? subscription = await FindByPublicGuidAsync(
                connection,
                transaction,
                publicGuid,
                cancellationToken);

            if (subscription is null)
            {
                await transaction.CommitAsync(cancellationToken);
                return new SubscriptionFeedCredentialResult(
                    SubscriptionFeedCredentialStatuses.NotFound,
                    null,
                    false,
                    "subscription_not_found");
            }

            const string selectSql = """
                SELECT id, credential_key_id, credential_nonce,
                       credential_ciphertext, credential_tag
                FROM subscription_feed_credentials
                WHERE subscription_id = $subscription_id
                  AND revoked_at_utc IS NULL
                LIMIT 1;
                """;
            await using SqliteCommand select = new(selectSql, connection, transaction);
            select.Parameters.AddWithValue("$subscription_id", subscription.Id);
            await using SqliteDataReader reader = await select.ExecuteReaderAsync(cancellationToken);

            if (await reader.ReadAsync(cancellationToken))
            {
                long credentialId = reader.GetInt64(0);
                ProtectedDeviceCredential protectedCredential = new(
                    reader.GetString(1),
                    reader.GetString(2),
                    reader.GetString(3),
                    reader.GetString(4));
                await reader.DisposeAsync();

                string rawSecret;
                try
                {
                    rawSecret = _deviceCredentialProtector.Unprotect(protectedCredential);
                }
                catch (CryptographicException)
                {
                    await transaction.CommitAsync(cancellationToken);
                    return new SubscriptionFeedCredentialResult(
                        SubscriptionFeedCredentialStatuses.Invalid,
                        null,
                        false,
                        "feed_credential_unavailable");
                }

                if (_deviceCredentialProtector.NeedsReencryption(protectedCredential))
                {
                    ProtectedDeviceCredential replacement =
                        _deviceCredentialProtector.Protect(rawSecret);
                    const string updateSql = """
                        UPDATE subscription_feed_credentials
                        SET credential_key_id = $credential_key_id,
                            credential_nonce = $credential_nonce,
                            credential_ciphertext = $credential_ciphertext,
                            credential_tag = $credential_tag
                        WHERE id = $id;
                        """;
                    await using SqliteCommand update = new(updateSql, connection, transaction);
                    update.Parameters.AddWithValue("$credential_key_id", replacement.KeyId);
                    update.Parameters.AddWithValue("$credential_nonce", replacement.Nonce);
                    update.Parameters.AddWithValue("$credential_ciphertext", replacement.Ciphertext);
                    update.Parameters.AddWithValue("$credential_tag", replacement.Tag);
                    update.Parameters.AddWithValue("$id", credentialId);
                    await update.ExecuteNonQueryAsync(cancellationToken);
                }

                await transaction.CommitAsync(cancellationToken);
                return new SubscriptionFeedCredentialResult(
                    SubscriptionFeedCredentialStatuses.Existing,
                    linkFactory.CreateSubscriptionFeedLink(httpContext, publicGuid, rawSecret),
                    false);
            }

            await reader.DisposeAsync();
            string secret = TokenSecretGenerator.CreateSecret();
            string secretHash = DeviceTokenHasher.Hash(secret, GetDeviceTokenHashKey());
            ProtectedDeviceCredential protectedSecret =
                _deviceCredentialProtector.Protect(secret);
            const string insertSql = """
                INSERT INTO subscription_feed_credentials
                    (subscription_id, secret_hash, credential_key_id, credential_nonce,
                     credential_ciphertext, credential_tag, created_at_utc)
                VALUES
                    ($subscription_id, $secret_hash, $credential_key_id, $credential_nonce,
                     $credential_ciphertext, $credential_tag, $created_at_utc);
                """;
            await using SqliteCommand insert = new(insertSql, connection, transaction);
            insert.Parameters.AddWithValue("$subscription_id", subscription.Id);
            insert.Parameters.AddWithValue("$secret_hash", secretHash);
            insert.Parameters.AddWithValue("$credential_key_id", protectedSecret.KeyId);
            insert.Parameters.AddWithValue("$credential_nonce", protectedSecret.Nonce);
            insert.Parameters.AddWithValue("$credential_ciphertext", protectedSecret.Ciphertext);
            insert.Parameters.AddWithValue("$credential_tag", protectedSecret.Tag);
            insert.Parameters.AddWithValue("$created_at_utc", Format(now));
            await insert.ExecuteNonQueryAsync(cancellationToken);
            await AddAuditAsync(
                connection,
                transaction,
                now,
                "subscription_feed.credential_created",
                publicGuid,
                null,
                null,
                null,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);

            return new SubscriptionFeedCredentialResult(
                SubscriptionFeedCredentialStatuses.Created,
                linkFactory.CreateSubscriptionFeedLink(httpContext, publicGuid, secret),
                true);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task<UnifiedFeedDeviceResolution> ResolveUnifiedFeedDeviceAsync(
        Guid publicGuid,
        string? rawToken,
        DeviceAccessRequestContext requestContext,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        if (!requestContext.Identity.IsValid
            || requestContext.Identity.CurrentHash is null
            || requestContext.Identity.CurrentKeyId is null)
        {
            return UnifiedFeedDeviceResolution.Forbidden(
                TokenSubscriptionAccessResult.Forbidden(UserFacingStatus.DeviceIdentityRequired));
        }

        string? normalizedToken = TextSanitizer.NullIfWhiteSpace(rawToken);
        if (normalizedToken is null)
        {
            return UnifiedFeedDeviceResolution.Forbidden(
                TokenSubscriptionAccessResult.Forbidden(UserFacingStatus.DeviceTokenInvalid));
        }

        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await using SqliteTransaction transaction =
                connection.BeginTransaction(deferred: false);

            const string subscriptionSql = """
                SELECT s.id, e.status, e.valid_until_utc, e.max_device_tokens,
                       c.secret_hash
                FROM mediated_subscriptions s
                LEFT JOIN entitlement_mirrors e ON e.subscription_id = s.id
                LEFT JOIN subscription_feed_credentials c
                  ON c.subscription_id = s.id
                 AND c.revoked_at_utc IS NULL
                WHERE s.public_guid = $public_guid
                LIMIT 1;
                """;
            await using SqliteCommand subscriptionCommand = new(
                subscriptionSql,
                connection,
                transaction);
            subscriptionCommand.Parameters.AddWithValue(
                "$public_guid",
                publicGuid.ToString("D"));
            await using SqliteDataReader subscriptionReader =
                await subscriptionCommand.ExecuteReaderAsync(cancellationToken);

            if (!await subscriptionReader.ReadAsync(cancellationToken))
            {
                await transaction.CommitAsync(cancellationToken);
                return UnifiedFeedDeviceResolution.Forbidden(
                    TokenSubscriptionAccessResult.Forbidden(
                        UserFacingStatus.SubscriptionNotFound));
            }

            long subscriptionId = subscriptionReader.GetInt64(0);
            string? entitlementStatus = ReadString(subscriptionReader, 1);
            DateTimeOffset? validUntilUtc = ReadDate(subscriptionReader, 2);
            int maxDeviceTokens = subscriptionReader.IsDBNull(3)
                ? 0
                : subscriptionReader.GetInt32(3);
            string? expectedSecretHash = ReadString(subscriptionReader, 4);
            await subscriptionReader.DisposeAsync();

            if (expectedSecretHash is null
                || !DeviceTokenHasher.Verify(
                    normalizedToken,
                    expectedSecretHash,
                    GetDeviceTokenHashKey()))
            {
                await transaction.CommitAsync(cancellationToken);
                return UnifiedFeedDeviceResolution.Forbidden(
                    TokenSubscriptionAccessResult.Forbidden(
                        UserFacingStatus.DeviceTokenInvalid));
            }

            if (entitlementStatus is null)
            {
                await transaction.CommitAsync(cancellationToken);
                return UnifiedFeedDeviceResolution.Forbidden(
                    TokenSubscriptionAccessResult.Forbidden(
                        UserFacingStatus.SubscriptionDisabled));
            }

            if (string.Equals(
                entitlementStatus,
                EntitlementStatuses.Expired,
                StringComparison.Ordinal)
                || (validUntilUtc is not null && validUntilUtc <= now))
            {
                await transaction.CommitAsync(cancellationToken);
                return UnifiedFeedDeviceResolution.Forbidden(
                    TokenSubscriptionAccessResult.Forbidden(
                        UserFacingStatus.SubscriptionExpired));
            }

            if (!string.Equals(
                entitlementStatus,
                EntitlementStatuses.Active,
                StringComparison.Ordinal))
            {
                await transaction.CommitAsync(cancellationToken);
                return UnifiedFeedDeviceResolution.Forbidden(
                    TokenSubscriptionAccessResult.Forbidden(
                        UserFacingStatus.SubscriptionDisabled));
            }

            UnifiedDeviceRow? existing = await FindUnifiedDeviceAsync(
                connection,
                transaction,
                subscriptionId,
                requestContext.Identity,
                cancellationToken);

            if (existing is not null)
            {
                if (existing.RevokedAtUtc is not null
                    || string.Equals(
                        existing.DeviceState,
                        UnifiedDeviceStates.Disabled,
                        StringComparison.Ordinal))
                {
                    int occupiedForDisabledDevice = await CountOccupiedDeviceSlotsAsync(
                        connection,
                        transaction,
                        subscriptionId,
                        now,
                        cancellationToken);
                    await transaction.CommitAsync(cancellationToken);
                    return UnifiedFeedDeviceResolution.Forbidden(
                        TokenSubscriptionAccessResult.Forbidden(
                            UserFacingStatus.DeviceTokenRevoked,
                            occupiedForDisabledDevice,
                            maxDeviceTokens));
                }

                bool reservationExpired = existing.ActivatedAtUtc is null
                    && existing.ProvisioningExpiresAtUtc is not null
                    && existing.ProvisioningExpiresAtUtc <= now;
                if (reservationExpired)
                {
                    int occupiedBeforeReservation = await CountOccupiedDeviceSlotsAsync(
                        connection,
                        transaction,
                        subscriptionId,
                        now,
                        cancellationToken);
                    if (occupiedBeforeReservation >= maxDeviceTokens)
                    {
                        await transaction.CommitAsync(cancellationToken);
                        return UnifiedFeedDeviceResolution.Forbidden(
                            TokenSubscriptionAccessResult.Forbidden(
                                UserFacingStatus.DeviceLimitReached,
                                occupiedBeforeReservation,
                                maxDeviceTokens));
                    }

                    await ResetUnifiedDeviceReservationAsync(
                        connection,
                        transaction,
                        existing.Id,
                        now,
                        cancellationToken);
                }

                await UpdateUnifiedDeviceObservationAsync(
                    connection,
                    transaction,
                    existing.Id,
                    requestContext,
                    now,
                    cancellationToken);
                int occupied = await CountOccupiedDeviceSlotsAsync(
                    connection,
                    transaction,
                    subscriptionId,
                    now,
                    cancellationToken);
                await transaction.CommitAsync(cancellationToken);
                return new UnifiedFeedDeviceResolution(
                    TokenSubscriptionAccessResult.Permit(
                        occupied,
                        maxDeviceTokens,
                        validUntilUtc),
                    existing.PublicId,
                    false,
                    existing.ActivatedAtUtc is null);
            }

            int occupiedSlots = await CountOccupiedDeviceSlotsAsync(
                connection,
                transaction,
                subscriptionId,
                now,
                cancellationToken);
            if (occupiedSlots >= maxDeviceTokens)
            {
                await transaction.CommitAsync(cancellationToken);
                return UnifiedFeedDeviceResolution.Forbidden(
                    TokenSubscriptionAccessResult.Forbidden(
                        UserFacingStatus.DeviceLimitReached,
                        occupiedSlots,
                        maxDeviceTokens));
            }

            string devicePublicId = Guid.NewGuid().ToString("N");
            string placeholderSecretHash = DeviceTokenHasher.Hash(
                TokenSecretGenerator.CreateSecret(),
                GetDeviceTokenHashKey());
            string displayName = BuildUnifiedDeviceDisplayName(requestContext.Metadata);
            DateTimeOffset reservationExpiresAt = now.AddMinutes(
                _options.UnifiedDeviceReservationMinutes);
            const string insertDeviceSql = """
                INSERT INTO device_access_tokens
                    (subscription_id, public_id, secret_hash, display_name, created_at_utc,
                     pending_expires_at_utc, requested_platform,
                     feed_policy_version, feed_policy_mode, binding_state,
                     bound_platform, bound_client_family, bound_at_utc,
                     bound_identity_hash, bound_identity_key_id, bound_identity_source,
                     last_identity_seen_at_utc, access_channel, device_state,
                     provisioning_expires_at_utc, device_type, platform,
                     detected_model, detection_source)
                VALUES
                    ($subscription_id, $public_id, $secret_hash, $display_name, $created_at_utc,
                     $pending_expires_at_utc, $requested_platform,
                     $feed_policy_version, $feed_policy_mode, $binding_state,
                     $bound_platform, $bound_client_family, $bound_at_utc,
                     $bound_identity_hash, $bound_identity_key_id, $bound_identity_source,
                     $last_identity_seen_at_utc, 'unified_feed', $device_state,
                     $provisioning_expires_at_utc, $device_type, $platform,
                     $detected_model, $detection_source);
                """;
            await using SqliteCommand insertDevice = new(
                insertDeviceSql,
                connection,
                transaction);
            insertDevice.Parameters.AddWithValue("$subscription_id", subscriptionId);
            insertDevice.Parameters.AddWithValue("$public_id", devicePublicId);
            insertDevice.Parameters.AddWithValue("$secret_hash", placeholderSecretHash);
            insertDevice.Parameters.AddWithValue("$display_name", displayName);
            insertDevice.Parameters.AddWithValue("$created_at_utc", Format(now));
            insertDevice.Parameters.AddWithValue(
                "$pending_expires_at_utc",
                Format(reservationExpiresAt));
            insertDevice.Parameters.AddWithValue(
                "$requested_platform",
                DbValue(requestContext.Metadata.Platform));
            insertDevice.Parameters.AddWithValue(
                "$feed_policy_version",
                DeviceFeedPolicyVersions.HwidIdentity);
            insertDevice.Parameters.AddWithValue(
                "$feed_policy_mode",
                DeviceFeedPolicyModes.Enforce);
            insertDevice.Parameters.AddWithValue(
                "$binding_state",
                DeviceFeedBindingStates.Bound);
            insertDevice.Parameters.AddWithValue(
                "$bound_platform",
                DbValue(requestContext.Metadata.Platform));
            insertDevice.Parameters.AddWithValue(
                "$bound_client_family",
                DbValue(requestContext.ClientFamily));
            insertDevice.Parameters.AddWithValue("$bound_at_utc", Format(now));
            insertDevice.Parameters.AddWithValue(
                "$bound_identity_hash",
                requestContext.Identity.CurrentHash);
            insertDevice.Parameters.AddWithValue(
                "$bound_identity_key_id",
                requestContext.Identity.CurrentKeyId);
            insertDevice.Parameters.AddWithValue(
                "$bound_identity_source",
                requestContext.Identity.Source ?? DeviceIdentitySources.HappHwid);
            insertDevice.Parameters.AddWithValue("$last_identity_seen_at_utc", Format(now));
            insertDevice.Parameters.AddWithValue(
                "$device_state",
                UnifiedDeviceStates.Provisioning);
            insertDevice.Parameters.AddWithValue(
                "$provisioning_expires_at_utc",
                Format(reservationExpiresAt));
            insertDevice.Parameters.AddWithValue(
                "$device_type",
                DbValue(requestContext.Metadata.DeviceType));
            insertDevice.Parameters.AddWithValue(
                "$platform",
                DbValue(requestContext.Metadata.Platform));
            insertDevice.Parameters.AddWithValue(
                "$detected_model",
                DbValue(requestContext.Metadata.DetectedModel));
            insertDevice.Parameters.AddWithValue(
                "$detection_source",
                DbValue(requestContext.Metadata.DetectionSource));
            await insertDevice.ExecuteNonQueryAsync(cancellationToken);
            await AddAuditAsync(
                connection,
                transaction,
                now,
                "unified_device.reserved",
                publicGuid,
                null,
                null,
                null,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);

            return new UnifiedFeedDeviceResolution(
                TokenSubscriptionAccessResult.Permit(
                    occupiedSlots + 1,
                    maxDeviceTokens,
                    validUntilUtc),
                devicePublicId,
                true,
                true);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task MarkUnifiedDeviceActivatedAsync(
        Guid publicGuid,
        string devicePublicId,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            const string sql = """
                UPDATE device_access_tokens
                SET activated_at_utc = COALESCE(activated_at_utc, $now),
                    first_fetched_at_utc = COALESCE(first_fetched_at_utc, $now),
                    last_used_at_utc = $now,
                    pending_expires_at_utc = NULL,
                    provisioning_expires_at_utc = NULL,
                    device_state = $device_state
                WHERE public_id = $public_id
                  AND access_channel = 'unified_feed'
                  AND revoked_at_utc IS NULL
                  AND subscription_id = (
                      SELECT id FROM mediated_subscriptions WHERE public_guid = $public_guid
                  );
                """;
            await using SqliteCommand command = new(sql, connection);
            command.Parameters.AddWithValue("$now", Format(now));
            command.Parameters.AddWithValue("$device_state", UnifiedDeviceStates.Active);
            command.Parameters.AddWithValue("$public_id", devicePublicId);
            command.Parameters.AddWithValue("$public_guid", publicGuid.ToString("D"));
            await command.ExecuteNonQueryAsync(cancellationToken);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task MarkUnifiedDeviceProvisioningFailedAsync(
        Guid publicGuid,
        string devicePublicId,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            const string sql = """
                UPDATE device_access_tokens
                SET device_state = $device_state,
                    provisioning_expires_at_utc = COALESCE(
                        provisioning_expires_at_utc,
                        $provisioning_expires_at_utc),
                    pending_expires_at_utc = COALESCE(
                        pending_expires_at_utc,
                        $provisioning_expires_at_utc)
                WHERE public_id = $public_id
                  AND access_channel = 'unified_feed'
                  AND revoked_at_utc IS NULL
                  AND subscription_id = (
                      SELECT id FROM mediated_subscriptions WHERE public_guid = $public_guid
                  );
                """;
            await using SqliteCommand command = new(sql, connection);
            command.Parameters.AddWithValue(
                "$device_state",
                UnifiedDeviceStates.ProvisioningFailed);
            command.Parameters.AddWithValue(
                "$provisioning_expires_at_utc",
                Format(now.AddMinutes(_options.UnifiedDeviceReservationMinutes)));
            command.Parameters.AddWithValue("$public_id", devicePublicId);
            command.Parameters.AddWithValue("$public_guid", publicGuid.ToString("D"));
            await command.ExecuteNonQueryAsync(cancellationToken);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task<UnifiedDeviceEnableResult> EnableUnifiedDeviceAsync(
        Guid publicGuid,
        string devicePublicId,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await _writeLock.WaitAsync(cancellationToken);

        try
        {
            await using SqliteConnection connection = await OpenConnectionAsync(cancellationToken);
            await using SqliteTransaction transaction =
                connection.BeginTransaction(deferred: false);
            const string selectSql = """
                SELECT d.id, d.revoked_at_utc, e.max_device_tokens, e.status, e.valid_until_utc
                FROM device_access_tokens d
                JOIN mediated_subscriptions s ON s.id = d.subscription_id
                LEFT JOIN entitlement_mirrors e ON e.subscription_id = s.id
                WHERE s.public_guid = $public_guid
                  AND d.public_id = $public_id
                  AND d.access_channel = 'unified_feed'
                LIMIT 1;
                """;
            await using SqliteCommand select = new(selectSql, connection, transaction);
            select.Parameters.AddWithValue("$public_guid", publicGuid.ToString("D"));
            select.Parameters.AddWithValue("$public_id", devicePublicId);
            await using SqliteDataReader reader = await select.ExecuteReaderAsync(cancellationToken);
            if (!await reader.ReadAsync(cancellationToken))
            {
                await transaction.CommitAsync(cancellationToken);
                return new UnifiedDeviceEnableResult("not_found", "device_not_found", 0, 0);
            }

            long deviceId = reader.GetInt64(0);
            DateTimeOffset? revokedAtUtc = ReadDate(reader, 1);
            int maxDeviceTokens = reader.IsDBNull(2) ? 0 : reader.GetInt32(2);
            string? status = ReadString(reader, 3);
            DateTimeOffset? validUntilUtc = ReadDate(reader, 4);
            await reader.DisposeAsync();

            if (!string.Equals(status, EntitlementStatuses.Active, StringComparison.Ordinal)
                || (validUntilUtc is not null && validUntilUtc <= now))
            {
                await transaction.CommitAsync(cancellationToken);
                return new UnifiedDeviceEnableResult(
                    "invalid",
                    "subscription_not_active",
                    0,
                    maxDeviceTokens);
            }

            long subscriptionId = await GetSubscriptionIdAsync(
                connection,
                transaction,
                publicGuid,
                cancellationToken);
            int occupiedSlots = await CountOccupiedDeviceSlotsAsync(
                connection,
                transaction,
                subscriptionId,
                now,
                cancellationToken);

            if (revokedAtUtc is null)
            {
                await transaction.CommitAsync(cancellationToken);
                return new UnifiedDeviceEnableResult(
                    "existing",
                    null,
                    occupiedSlots,
                    maxDeviceTokens);
            }

            if (occupiedSlots >= maxDeviceTokens)
            {
                await transaction.CommitAsync(cancellationToken);
                return new UnifiedDeviceEnableResult(
                    "limit_reached",
                    "device_limit_reached",
                    occupiedSlots,
                    maxDeviceTokens);
            }

            DateTimeOffset reservationExpiresAt = now.AddMinutes(
                _options.UnifiedDeviceReservationMinutes);
            const string updateSql = """
                UPDATE device_access_tokens
                SET revoked_at_utc = NULL,
                    revocation_reason = NULL,
                    activated_at_utc = NULL,
                    pending_expires_at_utc = $pending_expires_at_utc,
                    provisioning_expires_at_utc = $pending_expires_at_utc,
                    device_state = $device_state
                WHERE id = $id;
                """;
            await using SqliteCommand update = new(updateSql, connection, transaction);
            update.Parameters.AddWithValue(
                "$pending_expires_at_utc",
                Format(reservationExpiresAt));
            update.Parameters.AddWithValue(
                "$device_state",
                UnifiedDeviceStates.Provisioning);
            update.Parameters.AddWithValue("$id", deviceId);
            await update.ExecuteNonQueryAsync(cancellationToken);
            await AddAuditAsync(
                connection,
                transaction,
                now,
                "unified_device.enabled",
                publicGuid,
                null,
                null,
                null,
                cancellationToken);
            await transaction.CommitAsync(cancellationToken);
            return new UnifiedDeviceEnableResult(
                "enabled",
                null,
                occupiedSlots + 1,
                maxDeviceTokens);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    private static async Task<UnifiedDeviceRow?> FindUnifiedDeviceAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long subscriptionId,
        DeviceIdentityEvidence identity,
        CancellationToken cancellationToken)
    {
        const string sql = """
            SELECT id, public_id, activated_at_utc, revoked_at_utc,
                   pending_expires_at_utc, provisioning_expires_at_utc,
                   device_state, bound_identity_hash
            FROM device_access_tokens
            WHERE subscription_id = $subscription_id
              AND access_channel = 'unified_feed'
              AND (
                  bound_identity_hash = $current_hash
                  OR ($previous_hash IS NOT NULL AND bound_identity_hash = $previous_hash)
              )
            ORDER BY CASE WHEN bound_identity_hash = $current_hash THEN 0 ELSE 1 END
            LIMIT 1;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$subscription_id", subscriptionId);
        command.Parameters.AddWithValue("$current_hash", identity.CurrentHash!);
        command.Parameters.AddWithValue("$previous_hash", DbValue(identity.PreviousHash));
        await using SqliteDataReader reader = await command.ExecuteReaderAsync(cancellationToken);
        if (!await reader.ReadAsync(cancellationToken))
        {
            return null;
        }

        return new UnifiedDeviceRow(
            reader.GetInt64(0),
            reader.GetString(1),
            ReadDate(reader, 2),
            ReadDate(reader, 3),
            ReadDate(reader, 4),
            ReadDate(reader, 5),
            reader.GetString(6),
            reader.GetString(7));
    }

    private static async Task<int> CountOccupiedDeviceSlotsAsync(
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
              AND (
                  activated_at_utc IS NOT NULL
                  OR (
                      activated_at_utc IS NULL
                      AND (pending_expires_at_utc IS NULL OR pending_expires_at_utc > $now)
                  )
              );
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$subscription_id", subscriptionId);
        command.Parameters.AddWithValue("$now", Format(now));
        return Convert.ToInt32(await command.ExecuteScalarAsync(cancellationToken));
    }

    private async Task ResetUnifiedDeviceReservationAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long deviceId,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        DateTimeOffset reservationExpiresAt = now.AddMinutes(
            _options.UnifiedDeviceReservationMinutes);
        const string sql = """
            UPDATE device_access_tokens
            SET pending_expires_at_utc = $pending_expires_at_utc,
                provisioning_expires_at_utc = $pending_expires_at_utc,
                device_state = $device_state
            WHERE id = $id;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue(
            "$pending_expires_at_utc",
            Format(reservationExpiresAt));
        command.Parameters.AddWithValue("$device_state", UnifiedDeviceStates.Provisioning);
        command.Parameters.AddWithValue("$id", deviceId);
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private static async Task UpdateUnifiedDeviceObservationAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        long deviceId,
        DeviceAccessRequestContext context,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        const string sql = """
            UPDATE device_access_tokens
            SET display_name = $display_name,
                device_type = COALESCE($device_type, device_type),
                platform = COALESCE($platform, platform),
                detected_model = COALESCE($detected_model, detected_model),
                detection_source = COALESCE($detection_source, detection_source),
                bound_platform = COALESCE($platform, bound_platform),
                bound_client_family = COALESCE($client_family, bound_client_family),
                bound_identity_hash = $bound_identity_hash,
                bound_identity_key_id = $bound_identity_key_id,
                bound_identity_source = $bound_identity_source,
                last_identity_seen_at_utc = $now,
                last_used_at_utc = CASE
                    WHEN activated_at_utc IS NULL THEN last_used_at_utc
                    ELSE $now
                END
            WHERE id = $id;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue(
            "$display_name",
            BuildUnifiedDeviceDisplayName(context.Metadata));
        command.Parameters.AddWithValue("$device_type", DbValue(context.Metadata.DeviceType));
        command.Parameters.AddWithValue("$platform", DbValue(context.Metadata.Platform));
        command.Parameters.AddWithValue(
            "$detected_model",
            DbValue(context.Metadata.DetectedModel));
        command.Parameters.AddWithValue(
            "$detection_source",
            DbValue(context.Metadata.DetectionSource));
        command.Parameters.AddWithValue("$client_family", DbValue(context.ClientFamily));
        command.Parameters.AddWithValue(
            "$bound_identity_hash",
            context.Identity.CurrentHash!);
        command.Parameters.AddWithValue(
            "$bound_identity_key_id",
            context.Identity.CurrentKeyId!);
        command.Parameters.AddWithValue(
            "$bound_identity_source",
            context.Identity.Source ?? DeviceIdentitySources.HappHwid);
        command.Parameters.AddWithValue("$now", Format(now));
        command.Parameters.AddWithValue("$id", deviceId);
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    private static string BuildUnifiedDeviceDisplayName(DeviceMetadata metadata)
    {
        string? model = TextSanitizer.NullIfWhiteSpace(metadata.DetectedModel);
        if (model is not null)
        {
            return model.Length <= 96 ? model : model[..96];
        }

        return metadata.Platform switch
        {
            "android" => "Android device",
            "ios" => "iPhone or iPad",
            "windows" => "Windows device",
            "macos" => "macOS device",
            "linux" => "Linux device",
            _ => "Happ device"
        };
    }

    private static async Task<long> GetSubscriptionIdAsync(
        SqliteConnection connection,
        SqliteTransaction transaction,
        Guid publicGuid,
        CancellationToken cancellationToken)
    {
        const string sql = """
            SELECT id
            FROM mediated_subscriptions
            WHERE public_guid = $public_guid;
            """;
        await using SqliteCommand command = new(sql, connection, transaction);
        command.Parameters.AddWithValue("$public_guid", publicGuid.ToString("D"));
        object? value = await command.ExecuteScalarAsync(cancellationToken);
        return Convert.ToInt64(value);
    }

    private sealed record UnifiedDeviceRow(
        long Id,
        string PublicId,
        DateTimeOffset? ActivatedAtUtc,
        DateTimeOffset? RevokedAtUtc,
        DateTimeOffset? PendingExpiresAtUtc,
        DateTimeOffset? ProvisioningExpiresAtUtc,
        string DeviceState,
        string BoundIdentityHash);
}

public static class UnifiedDeviceStateMapper
{
    public static string ToPublicState(
        string? deviceState,
        DateTimeOffset? revokedAtUtc,
        DateTimeOffset? pendingExpiresAtUtc,
        DateTimeOffset now)
    {
        if (revokedAtUtc is not null
            || string.Equals(deviceState, UnifiedDeviceStates.Disabled, StringComparison.Ordinal))
        {
            return DeviceTokenStates.Revoked;
        }

        if (string.Equals(deviceState, UnifiedDeviceStates.Active, StringComparison.Ordinal))
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
