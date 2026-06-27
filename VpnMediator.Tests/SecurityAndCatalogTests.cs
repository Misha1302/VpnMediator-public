using System.Net;
using System.Security.Cryptography;
using System.Text.Json;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Data.Sqlite;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.FileProviders;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Options;
using Xunit;

public sealed class SecurityAndCatalogTests
{
    [Fact]
    public void ContentFingerprintIgnoresPresentationChanges()
    {
        string[] first =
        [
            "vless://00000000-0000-0000-0000-000000000001@example.com:443#Server%202",
            "trojan://secret@example.net:443#Amsterdam"
        ];
        string[] second =
        [
            "trojan://secret@example.net:443#Амстердам",
            "vless://00000000-0000-0000-0000-000000000001@example.com:443#Server%2010"
        ];

        Assert.Equal(
            CatalogFingerprint.ComputeContent(first),
            CatalogFingerprint.ComputeContent(second));
        Assert.NotEqual(
            CatalogFingerprint.ComputePresentation(first),
            CatalogFingerprint.ComputePresentation(second));
    }

    [Fact]
    public void CatalogPresentationUsesNaturalStableOrdering()
    {
        string[] shuffled =
        [
            "vless://00000000-0000-0000-0000-000000000010@example.com:443#Server%2010",
            "vless://00000000-0000-0000-0000-000000000002@example.com:443#Server%202",
            "vless://00000000-0000-0000-0000-000000000001@example.com:443#Server%201"
        ];

        IReadOnlyList<string> sorted = CatalogPresentation.Build(shuffled, 10);

        Assert.Contains("Server%201", sorted[0], StringComparison.Ordinal);
        Assert.Contains("Server%202", sorted[1], StringComparison.Ordinal);
        Assert.Contains("Server%2010", sorted[2], StringComparison.Ordinal);
        Assert.Equal(sorted, CatalogPresentation.Build(shuffled.Reverse(), 10));
    }

    [Fact]
    public void CatalogPresentationUsesPublicationNumberAndConfiguredCountry()
    {
        string[] ranked =
        [
            "vless://00000000-0000-0000-0000-000000000002@fast.example:443#4%20%7C%20Wi-Fi%20%D1%81%D0%B5%D1%80%D0%B2%D0%B5%D1%80%20%239",
            "trojan://secret@slow.example:443#3%20%7C%20LTE%20%D0%9E%D0%B1%D1%85%D0%BE%D0%B4%20%D0%B3%D0%BB%D1%83%D1%88%D0%B8%D0%BB%D0%BE%D0%BA%202",
            "ss://encoded@example.net:443"
        ];
        Dictionary<string, string> countries = new(StringComparer.Ordinal)
        {
            ["4"] = "Германия",
            ["3"] = "Польша"
        };

        IReadOnlyList<string> published =
            CatalogPresentation.RenumberInPublicationOrder(ranked, countries);

        Assert.Equal("1 | 🇩🇪 Германия вайфай", CatalogPresentation.GetDisplayName(published[0]));
        Assert.Equal(
            "2 | 🇵🇱 Польша мобильный интернет",
            CatalogPresentation.GetDisplayName(published[1]));
        Assert.Equal("3 | 🌐 Неизвестно", CatalogPresentation.GetDisplayName(published[2]));
        Assert.Equal(
            ranked.Select(CatalogFingerprint.NormalizeTechnicalIdentity),
            published.Select(CatalogFingerprint.NormalizeTechnicalIdentity));
    }

    [Fact]
    public void CatalogPresentationRenumberingIsStableAcrossRepeatedPublication()
    {
        string[] firstPublication =
        [
            "vless://00000000-0000-0000-0000-000000000001@example.com:443#4%20%7C%20Wi-Fi%20%D1%81%D0%B5%D1%80%D0%B2%D0%B5%D1%80%20%239",
            "vless://00000000-0000-0000-0000-000000000002@example.net:443#3%20%7C%20LTE%20%D0%BE%D1%82%20%D0%B3%D0%BB%D1%83%D1%88%D0%B8%D0%BB%D0%BE%D0%BA%202"
        ];
        Dictionary<string, string> countries = new(StringComparer.Ordinal)
        {
            ["4"] = "Германия",
            ["3"] = "Польша"
        };

        IReadOnlyList<string> first =
            CatalogPresentation.RenumberInPublicationOrder(firstPublication, countries);
        IReadOnlyList<string> second =
            CatalogPresentation.RenumberInPublicationOrder(first, countries);

        Assert.Equal(first, second);
        Assert.Equal("1 | 🇩🇪 Германия вайфай", CatalogPresentation.GetDisplayName(second[0]));
        Assert.Equal(
            "2 | 🇵🇱 Польша мобильный интернет",
            CatalogPresentation.GetDisplayName(second[1]));
    }

    [Theory]
    [InlineData("WiFi", "1 | 🌐 Неизвестно вайфай")]
    [InlineData("Wi-Fi сервер", "1 | 🌐 Неизвестно вайфай")]
    [InlineData("вай-фай", "1 | 🌐 Неизвестно вайфай")]
    [InlineData("LTE обход глушилок", "1 | 🌐 Неизвестно мобильный интернет")]
    [InlineData("глушилки", "1 | 🌐 Неизвестно мобильный интернет")]
    [InlineData("мобильный интернет", "1 | 🌐 Неизвестно мобильный интернет")]
    [InlineData(
        "Italy WIFI от глушилок",
        "1 | 🇮🇹 Италия мобильный интернет вайфай")]
    [InlineData("swiftification", "1 | 🌐 Неизвестно")]
    public void CatalogPresentationPreservesOnlyAllowlistedSemanticPostfixes(
        string sourceName,
        string expectedName)
    {
        string source =
            $"vless://00000000-0000-0000-0000-000000000001@example.com:443#{Uri.EscapeDataString(sourceName)}";

        string first = Assert.Single(CatalogPresentation.RenumberInPublicationOrder([source]));
        string second = Assert.Single(CatalogPresentation.RenumberInPublicationOrder([first]));

        Assert.Equal(expectedName, CatalogPresentation.GetDisplayName(first));
        Assert.Equal(first, second);
        Assert.Equal(
            CatalogFingerprint.ComputeContent([source]),
            CatalogFingerprint.ComputeContent([first]));
        Assert.NotEqual(
            CatalogFingerprint.ComputePresentation([source]),
            CatalogFingerprint.ComputePresentation([first]));
    }

    [Fact]
    public void CatalogPresentationNormalizesCountryAlreadyPresentInSourceName()
    {
        const string source =
            "vless://00000000-0000-0000-0000-000000000001@example.com:443#Italy";

        string published = Assert.Single(
            CatalogPresentation.RenumberInPublicationOrder([source]));

        Assert.Equal("1 | 🇮🇹 Италия", CatalogPresentation.GetDisplayName(published));
    }

    [Fact]
    public void CatalogPresentationOrdersMobileInternetFirstAndWifiLastByDefault()
    {
        string[] shuffled =
        [
            "vless://00000000-0000-0000-0000-000000000001@wifi.example:443#Germany%20WiFi",
            "vless://00000000-0000-0000-0000-000000000002@neutral.example:443#Finland",
            "vless://00000000-0000-0000-0000-000000000003@mobile.example:443#Italy%20%D0%BE%D1%82%20%D0%B3%D0%BB%D1%83%D1%88%D0%B8%D0%BB%D0%BE%D0%BA"
        ];

        IReadOnlyList<string> sorted = CatalogPresentation.Build(shuffled, 10);

        Assert.Contains("mobile.example", sorted[0], StringComparison.Ordinal);
        Assert.Contains("neutral.example", sorted[1], StringComparison.Ordinal);
        Assert.Contains("wifi.example", sorted[2], StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("Germany", "1 | 🇩🇪 Германия")]
    [InlineData("🇵🇱 Poland", "1 | 🇵🇱 Польша")]
    [InlineData("Нидерланды", "1 | 🇳🇱 Нидерланды")]
    [InlineData("Finland", "1 | 🇫🇮 Финляндия")]
    [InlineData("Russia", "1 | 🇷🇺 Россия")]
    public void CatalogPresentationAddsExactlyOneCountryFlag(
        string sourceName,
        string expectedName)
    {
        string source =
            $"vless://00000000-0000-0000-0000-000000000001@example.com:443#{Uri.EscapeDataString(sourceName)}";

        string first = Assert.Single(
            CatalogPresentation.RenumberInPublicationOrder([source]));
        string second = Assert.Single(
            CatalogPresentation.RenumberInPublicationOrder([first]));

        Assert.Equal(expectedName, CatalogPresentation.GetDisplayName(first));
        Assert.Equal(first, second);
    }

    [Fact]
    public void OptionsRejectInvalidServerPresentationCountryMapping()
    {
        VpnMediatorOptions options = CreateValidProductionOptions();
        options.ServerPresentationCountryBySourceNumber["not-a-number"] = "Германия";
        options.ServerPresentationCountryBySourceNumber["2"] = "Польша | резерв";
        VpnMediatorOptionsValidator validator = new(
            new TestEnvironment(Environments.Production));

        ValidateOptionsResult result = validator.Validate(null, options);

        Assert.True(result.Failed);
        Assert.Contains(
            result.Failures,
            failure => failure.Contains(
                "ServerPresentationCountryBySourceNumber",
                StringComparison.Ordinal));
    }

    [Fact]
    public void CatalogAnomalyGuardRejectsNearTotalReplacement()
    {
        string[] previous = Enumerable.Range(1, 20)
            .Select(index => $"vless://00000000-0000-0000-0000-{index:000000000000}@old.example:443#Server{index}")
            .ToArray();
        string[] candidate = Enumerable.Range(1, 20)
            .Select(index => $"vless://10000000-0000-0000-0000-{index:000000000000}@new.example:443#Server{index}")
            .ToArray();

        Assert.True(CatalogAnomalyGuard.ShouldReject(previous, candidate));
        Assert.False(CatalogAnomalyGuard.ShouldReject(previous, previous.Reverse().ToArray()));
    }

    [Fact]
    public async Task CatalogRefreshPublishesEveryValidatedServerWithoutHealthFiltering()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        DateTimeOffset now = DateTimeOffset.UtcNow;
        string first =
            "vless://00000000-0000-0000-0000-000000000001@example.com:443#1";
        string second = "trojan://secret@example.net:443#2";
        IReadOnlyList<string> links = [first, second];
        SourceDetails source = await fixture.Repository.CreateSourceAsync(
            new CreateSourceRequest(
                "primary",
                SourceKinds.SubscriptionUrl,
                "https://catalog.example/subscription"),
            now,
            CancellationToken.None);
        SourceReadResult testResult = SourceReadResult.Successful(
            links,
            1,
            ["vless", "trojan"],
            0,
            []);
        await fixture.Repository.SaveSourceTestResultAsync(
            source.Id,
            testResult,
            now,
            CancellationToken.None);
        Assert.True(await fixture.Repository.SetSourceStateAsync(
            source.Id,
            SourceStates.Enabled,
            now,
            CancellationToken.None));

        BlockingSourceReader reader = new(links);
        Task<CatalogRefreshResult> refresh = fixture.Repository.RefreshCatalogAsync(
            new BlockingSourceReaderRegistry(reader),
            now.AddSeconds(1),
            CancellationToken.None);
        await reader.Entered.Task.WaitAsync(TimeSpan.FromSeconds(2));
        reader.Release.SetResult();
        CatalogRefreshResult result = await refresh;
        PublishedSnapshot snapshot = Assert.IsType<PublishedSnapshot>(
            await fixture.Repository.GetLatestPublishedSnapshotAsync(CancellationToken.None));

        Assert.Equal(PublishedSnapshotStates.Fresh, result.State);
        Assert.Equal(2, result.ServerCount);
        Assert.Equal(2, snapshot.ServerLinks.Count);
        Assert.Contains(snapshot.ServerLinks, link => link.StartsWith("vless://", StringComparison.Ordinal));
        Assert.Contains(snapshot.ServerLinks, link => link.StartsWith("trojan://", StringComparison.Ordinal));
    }

    [Fact]
    public async Task CatalogRefreshRejectsNearTotalReplacementOfPublishedSourceSnapshot()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        DateTimeOffset now = DateTimeOffset.UtcNow;
        IReadOnlyList<string> original = Enumerable.Range(1, 20)
            .Select(index =>
                $"vless://00000000-0000-0000-0000-{index:000000000000}@old.example:443#Server{index}")
            .ToArray();
        IReadOnlyList<string> replacement = Enumerable.Range(1, 20)
            .Select(index =>
                $"vless://10000000-0000-0000-0000-{index:000000000000}@new.example:443#Server{index}")
            .ToArray();
        SourceDetails source = await fixture.Repository.CreateSourceAsync(
            new CreateSourceRequest(
                "primary",
                SourceKinds.SubscriptionUrl,
                "https://catalog.example/subscription"),
            now,
            CancellationToken.None);
        SourceReadResult testResult = SourceReadResult.Successful(
            original,
            1,
            ["vless"],
            0,
            []);
        await fixture.Repository.SaveSourceTestResultAsync(
            source.Id,
            testResult,
            now,
            CancellationToken.None);
        Assert.True(await fixture.Repository.SetSourceStateAsync(
            source.Id,
            SourceStates.Enabled,
            now,
            CancellationToken.None));

        CatalogRefreshResult first = await fixture.Repository.RefreshCatalogAsync(
            new BlockingSourceReaderRegistry(new ImmediateSourceReader(original)),
            now.AddSeconds(1),
            CancellationToken.None);
        CatalogRefreshResult second = await fixture.Repository.RefreshCatalogAsync(
            new BlockingSourceReaderRegistry(new ImmediateSourceReader(replacement)),
            now.AddSeconds(2),
            CancellationToken.None);
        PublishedSnapshot snapshot = Assert.IsType<PublishedSnapshot>(
            await fixture.Repository.GetLatestPublishedSnapshotAsync(CancellationToken.None));

        Assert.Equal(PublishedSnapshotStates.Fresh, first.State);
        Assert.Equal("anomaly_rejected", second.State);
        Assert.Equal(original.Count, snapshot.ServerLinks.Count);
        Assert.All(
            snapshot.ServerLinks,
            link => Assert.Contains("@old.example:443", link, StringComparison.Ordinal));
    }

    [Fact]
    public void PreviousAdminTokenRequiresUnexpiredWindow()
    {
        DefaultHttpContext context = new();
        context.Request.Headers["X-Admin-Token"] = "previous-secret-value-with-sufficient-length";

        Assert.False(AdminGuard.IsAllowed(
            context,
            "current-secret-value-with-sufficient-length",
            "previous-secret-value-with-sufficient-length",
            DateTimeOffset.UtcNow.AddMinutes(-1)));
        Assert.True(AdminGuard.IsAllowed(
            context,
            "current-secret-value-with-sufficient-length",
            "previous-secret-value-with-sufficient-length",
            DateTimeOffset.UtcNow.AddMinutes(1)));
    }

    [Theory]
    [InlineData("/admin", true)]
    [InlineData("/admin/subscriptions", true)]
    [InlineData("/administer", false)]
    [InlineData("/subscription", false)]
    public void AdminPathAuthorizationProtectsWholeAdminNamespace(
        string path,
        bool expected)
    {
        Assert.Equal(
            expected,
            AdminPathAuthorization.IsProtectedPath(new PathString(path)));
    }

    [Fact]
    public void AdminPathAuthorizationUsesConfiguredTokenRotationWindow()
    {
        VpnMediatorOptions options = new()
        {
            AdminToken = "current-secret-value-with-sufficient-length",
            PreviousAdminToken = "previous-secret-value-with-sufficient-length",
            PreviousAdminTokenValidUntilUtc = DateTimeOffset.UtcNow.AddMinutes(5)
        };
        DefaultHttpContext current = new();
        current.Request.Headers["X-Admin-Token"] = options.AdminToken;
        DefaultHttpContext previous = new();
        previous.Request.Headers["X-Admin-Token"] = options.PreviousAdminToken;
        DefaultHttpContext rejected = new();
        rejected.Request.Headers["X-Admin-Token"] = "attacker-token-with-sufficient-length";

        Assert.True(AdminPathAuthorization.IsAllowed(current, options));
        Assert.True(AdminPathAuthorization.IsAllowed(previous, options));
        Assert.False(AdminPathAuthorization.IsAllowed(rejected, options));
    }

    [Theory]
    [InlineData("valid-id_123", true)]
    [InlineData("../etc/passwd", false)]
    [InlineData("with space", false)]
    [InlineData("", false)]
    public void RouteIdentifiersAreBoundedAndSafe(string value, bool expected)
    {
        Assert.Equal(expected, RouteIdentifierValidator.IsSafe(value));
    }

    [Fact]
    public void UnsignedRandomResourceIdentifiersCannotCreateNewRateLimitPartitions()
    {
        DefaultHttpContext first = new();
        first.Connection.RemoteIpAddress = IPAddress.Parse("8.8.8.8");
        first.Request.RouteValues["publicGuid"] = "resource-a";
        DefaultHttpContext second = new();
        second.Connection.RemoteIpAddress = IPAddress.Parse("8.8.8.8");
        second.Request.RouteValues["publicGuid"] = "resource-b";

        Assert.Equal(
            RateLimitPartitionKey.ForSubscription(first),
            RateLimitPartitionKey.ForSubscription(second));
    }

    [Fact]
    public void SignedSubscriptionPartitionIsStableAcrossSharedIpAddresses()
    {
        DefaultHttpContext first = SubscriptionContext(
            "00000000-0000-0000-0000-000000000001",
            "device-a",
            "token-secret-a",
            "100.64.0.10");
        DefaultHttpContext second = SubscriptionContext(
            "00000000-0000-0000-0000-000000000001",
            "device-a",
            "token-secret-a",
            "100.64.0.11");

        string firstKey = RateLimitPartitionKey.ForSubscription(first);
        string secondKey = RateLimitPartitionKey.ForSubscription(second);

        Assert.Equal(firstKey, secondKey);
        Assert.DoesNotContain("token-secret-a", firstKey, StringComparison.Ordinal);
    }

    [Fact]
    public void DifferentSignedSubscriptionsUseDifferentRateLimitPartitions()
    {
        DefaultHttpContext first = SubscriptionContext(
            "00000000-0000-0000-0000-000000000001",
            "device-a",
            "token-secret-a",
            "100.64.0.10");
        DefaultHttpContext second = SubscriptionContext(
            "00000000-0000-0000-0000-000000000001",
            "device-a",
            "token-secret-b",
            "100.64.0.10");

        Assert.NotEqual(
            RateLimitPartitionKey.ForSubscription(first),
            RateLimitPartitionKey.ForSubscription(second));
    }

    [Fact]
    public async Task ReadinessDeadlineReturnsStableFailClosedSnapshot()
    {
        MediatorReadinessSnapshot snapshot = await
            MediatorReadinessSnapshotBuilder.ExecuteWithDeadlineAsync(
                async cancellationToken =>
                {
                    await Task.Delay(Timeout.InfiniteTimeSpan, cancellationToken);
                    return new MediatorReadinessSnapshot(true, new Dictionary<string, object?>());
                },
                TimeSpan.FromMilliseconds(50),
                CancellationToken.None);

        Assert.False(snapshot.Ready);
        Assert.Equal("not_ready", snapshot.Body["status"]);
        Assert.Equal("readiness_deadline_exceeded", snapshot.Body["reason"]);
    }

    [Fact]
    public async Task ReadinessDeadlineDoesNotSwallowRequestCancellation()
    {
        using CancellationTokenSource cancellation = new();
        cancellation.Cancel();

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() =>
            MediatorReadinessSnapshotBuilder.ExecuteWithDeadlineAsync(
                async token =>
                {
                    await Task.Delay(Timeout.InfiniteTimeSpan, token);
                    return new MediatorReadinessSnapshot(true, new Dictionary<string, object?>());
                },
                TimeSpan.FromSeconds(5),
                cancellation.Token));
    }

    [Theory]
    [InlineData(true, "ready")]
    [InlineData(false, "not_ready")]
    public void PublicReadinessResponseContainsOnlyCoarseStatus(bool ready, string expectedStatus)
    {
        MediatorReadinessSnapshot snapshot = new(
            ready,
            new Dictionary<string, object?>
            {
                ["status"] = expectedStatus,
                ["reason"] = "internal-reason",
                ["deviceIdentityHashKeyId"] = "sensitive-key-id"
            });

        PublicReadinessResponse response = MediatorReadinessResponseFactory.CreatePublic(snapshot);

        Assert.Equal(expectedStatus, response.Status);
        Assert.Single(typeof(PublicReadinessResponse).GetProperties());
    }

    [Theory]
    [InlineData("127.0.0.1")]
    [InlineData("10.1.2.3")]
    [InlineData("172.16.1.1")]
    [InlineData("192.168.1.1")]
    [InlineData("169.254.169.254")]
    [InlineData("192.0.2.10")]
    [InlineData("198.51.100.10")]
    [InlineData("203.0.113.10")]
    [InlineData("224.0.0.1")]
    [InlineData("64:ff9b::1")]
    [InlineData("64:ff9b:1::1")]
    [InlineData("100::1")]
    [InlineData("2001::1")]
    [InlineData("2001:2::1")]
    [InlineData("2001:db8::1")]
    [InlineData("2002::1")]
    [InlineData("fc00::1")]
    [InlineData("fe80::1")]
    [InlineData("ff02::1")]
    [InlineData("::ffff:127.0.0.1")]
    public void PrivateAndReservedAddressesAreBlocked(string addressText)
    {
        Assert.False(NetworkAddressClassifier.IsPublicRoutable(IPAddress.Parse(addressText)));
    }

    [Theory]
    [InlineData("8.8.8.8")]
    [InlineData("192.0.1.1")]
    [InlineData("192.0.10.1")]
    [InlineData("2606:4700:4700::1111")]
    public void PublicAddressIsAllowed(string addressText)
    {
        Assert.True(NetworkAddressClassifier.IsPublicRoutable(IPAddress.Parse(addressText)));
    }

    [Fact]
    public void ProductionPublicBaseUrlOverridesHostHeader()
    {
        DefaultHttpContext context = new();
        context.Request.Scheme = "https";
        context.Request.Host = new HostString("attacker.example");

        string url = ProgramUrlHelpers.GetPublicBaseUrl(
            context,
            new VpnMediatorOptions
            {
                PublicBaseUrl = "https://vpn.example"
            });

        Assert.Equal("https://vpn.example", url);
    }

    [Fact]
    public void ProductionOptionsRejectPlaceholders()
    {
        VpnMediatorOptionsValidator validator = new(new TestEnvironment(Environments.Production));

        ValidateOptionsResult result = validator.Validate(
            null,
            new VpnMediatorOptions
            {
                PublicBaseUrl = "http://vpn.example",
                AdminToken = "change-me-local-admin-token",
                DeviceTokenHashKey = "change-me-local-device-token-key",
                SourceEndpointProtectionKey = Convert.ToBase64String(new byte[16]),
                AllowDevelopmentHttpSources = true
            });

        Assert.True(result.Failed);
        Assert.Contains(result.Failures, item => item.Contains("PublicBaseUrl", StringComparison.Ordinal));
        Assert.Contains(result.Failures, item => item.Contains("AdminToken", StringComparison.Ordinal));
        Assert.Contains(result.Failures, item => item.Contains("DeviceTokenHashKey", StringComparison.Ordinal));
    }

    [Fact]
    public void OptionsRejectInvalidProductName()
    {
        VpnMediatorOptionsValidator validator = new(new TestEnvironment(Environments.Development));

        ValidateOptionsResult result = validator.Validate(
            null,
            new VpnMediatorOptions
            {
                ProductName = new string('x', 33)
            });

        Assert.True(result.Failed);
        Assert.Contains(result.Failures, item => item.Contains("ProductName", StringComparison.Ordinal));
    }

    [Fact]
    public void DeviceTokenHashValidatesOnlyOriginalSecret()
    {
        const string signingSecret = "test-signing-secret-with-at-least-32-characters";
        string secret = TokenSecretGenerator.CreateSecret();
        string hash = DeviceTokenHasher.Hash(secret, signingSecret);

        Assert.True(DeviceTokenHasher.Verify(secret, hash, signingSecret));
        Assert.False(DeviceTokenHasher.Verify($"{secret}x", hash, signingSecret));
    }

    [Fact]
    public void SubscriptionCodecRoundTripsServerLinks()
    {
        string[] links =
        [
            "vless://00000000-0000-0000-0000-000000000001@example.com:443#one",
            "trojan://secret@example.com:443#two"
        ];

        string encoded = SubscriptionCodec.EncodeServerLinks(links);
        IReadOnlyList<string> decoded = SubscriptionCodec.DecodeServerLinks(encoded);

        Assert.Equal(links, decoded);
    }

    [Fact]
    public void InvalidBase64IsRejected()
    {
        Assert.Throws<FormatException>(() => SubscriptionCodec.DecodeServerLinks("***"));
    }

    [Fact]
    public void HealthySubscriptionContainsOnlyPublishedServerLinks()
    {
        SubscriptionResponseBuilder builder = new();
        TokenSubscriptionAccessResult access = TokenSubscriptionAccessResult.Permit(1, 3);
        string[] publishedLinks =
        [
            "vless://00000000-0000-0000-0000-000000000010@example.com:443#one",
            "trojan://secret@example.com:443#two"
        ];

        IReadOnlyList<string> links = builder.BuildSnapshotServerLinks(publishedLinks, access);

        Assert.Equal(publishedLinks, links);
        Assert.DoesNotContain(links, link => link.Contains("127.0.0.1", StringComparison.Ordinal));
        Assert.DoesNotContain(links, link => link.Contains("VPN РАБОТАЕТ", StringComparison.Ordinal));
        Assert.DoesNotContain(
            links,
            link => link.Contains("Свободно подключений", StringComparison.Ordinal));
    }

    [Fact]
    public void HappAnnouncementContainsDeviceUsageExpirationAndTelegramContact()
    {
        DateTimeOffset now = new(2026, 6, 14, 12, 0, 0, TimeSpan.Zero);
        TokenSubscriptionAccessResult access = TokenSubscriptionAccessResult.Permit(
            3,
            12,
            now.AddDays(30));

        string header = HappSubscriptionMetadata.BuildAnnouncementHeader(
            access,
            " @RazaltushVpnBot ",
            now);

        Assert.StartsWith("base64:", header, StringComparison.Ordinal);
        byte[] encoded = Convert.FromBase64String(header["base64:".Length..]);
        string announcement = System.Text.Encoding.UTF8.GetString(encoded);

        Assert.Equal(
            "📱 Подключено 3 из 12 устройств\n" +
            "⏳ Подписка закончится через 30 дней\n" +
            "💬 Telegram: @RazaltushVpnBot",
            announcement);
    }

    [Fact]
    public void HappAnnouncementAlwaysUsesCanonicalPublicBotUsername()
    {
        TokenSubscriptionAccessResult access = TokenSubscriptionAccessResult.Permit(1, 3);

        string header = HappSubscriptionMetadata.BuildAnnouncementHeader(
            access,
            "@RazakovVpnBot");
        byte[] encoded = Convert.FromBase64String(header["base64:".Length..]);
        string announcement = System.Text.Encoding.UTF8.GetString(encoded);

        Assert.Contains("Telegram: @RazaltushVpnBot", announcement, StringComparison.Ordinal);
        Assert.DoesNotContain("RazakovVpnBot", announcement, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData(1, "1 день")]
    [InlineData(2, "2 дня")]
    [InlineData(5, "5 дней")]
    [InlineData(11, "11 дней")]
    [InlineData(21, "21 день")]
    [InlineData(22, "22 дня")]
    [InlineData(25, "25 дней")]
    public void HappAnnouncementUsesCorrectRussianDayForm(
        int remainingDays,
        string expected)
    {
        DateTimeOffset now = new(2026, 6, 14, 12, 0, 0, TimeSpan.Zero);
        TokenSubscriptionAccessResult access = TokenSubscriptionAccessResult.Permit(
            1,
            3,
            now.AddDays(remainingDays));

        string header = HappSubscriptionMetadata.BuildAnnouncementHeader(
            access,
            "@RazaltushVpnBot",
            now);
        byte[] encoded = Convert.FromBase64String(header["base64:".Length..]);
        string announcement = System.Text.Encoding.UTF8.GetString(encoded);

        Assert.Contains($"через {expected}", announcement, StringComparison.Ordinal);
    }

    [Fact]
    public void HappAnnouncementRoundsRemainingPartialDayUp()
    {
        DateTimeOffset now = new(2026, 6, 14, 12, 0, 0, TimeSpan.Zero);
        TokenSubscriptionAccessResult access = TokenSubscriptionAccessResult.Permit(
            1,
            3,
            now.AddHours(25));

        string header = HappSubscriptionMetadata.BuildAnnouncementHeader(
            access,
            "@RazaltushVpnBot",
            now);
        byte[] encoded = Convert.FromBase64String(header["base64:".Length..]);
        string announcement = System.Text.Encoding.UTF8.GetString(encoded);

        Assert.Contains("через 2 дня", announcement, StringComparison.Ordinal);
    }

    [Fact]
    public void HappAnnouncementOmitsExpirationForUnlimitedSubscription()
    {
        TokenSubscriptionAccessResult access = TokenSubscriptionAccessResult.Permit(3, 12);

        string header = HappSubscriptionMetadata.BuildAnnouncementHeader(
            access,
            "@RazaltushVpnBot");
        byte[] encoded = Convert.FromBase64String(header["base64:".Length..]);
        string announcement = System.Text.Encoding.UTF8.GetString(encoded);

        Assert.DoesNotContain("Подписка закончится", announcement, StringComparison.Ordinal);
        Assert.DoesNotContain("Подписка заканчивается", announcement, StringComparison.Ordinal);
    }

    [Fact]
    public void HappAnnouncementIsNotAddedForBlockedSubscription()
    {
        DefaultHttpContext context = new();
        TokenSubscriptionAccessResult access = TokenSubscriptionAccessResult.Forbidden(
            UserFacingStatus.SubscriptionExpired,
            1,
            3);

        HappSubscriptionMetadata.Apply(
            context.Response,
            access,
            "@RazaltushVpnBot");

        Assert.False(context.Response.Headers.ContainsKey(
            HappSubscriptionMetadata.AnnouncementHeaderName));
    }

    [Fact]
    public void BlockingStatusProducesSingleCompatibilityEntry()
    {
        SubscriptionResponseBuilder builder = new();
        TokenSubscriptionAccessResult access = TokenSubscriptionAccessResult.Permit(3, 3);

        IReadOnlyList<string> links = builder.BuildStatusServerLinks(
            UserFacingStatus.ServersUnavailable,
            access);
        string decoded = Uri.UnescapeDataString(Assert.Single(links));

        Assert.Contains("откройте Telegram-бот", decoded, StringComparison.Ordinal);
        Assert.DoesNotContain("НЕ ПОДКЛЮЧАТЬ", decoded, StringComparison.Ordinal);
        Assert.DoesNotContain("VPN РАБОТАЕТ", decoded, StringComparison.Ordinal);
    }

    [Fact]
    public void SubscriptionResponsesAreProtectedFromCaching()
    {
        DefaultHttpContext context = new();

        Assert.True(SubscriptionResponseSecurity.IsProtectedPath("/sub"));
        Assert.True(SubscriptionResponseSecurity.IsProtectedPath("/sub/example/servers.txt"));
        Assert.False(SubscriptionResponseSecurity.IsProtectedPath("/subscription"));

        SubscriptionResponseSecurity.Apply(context.Response);

        Assert.Equal(
            "private, no-store, max-age=0, must-revalidate",
            context.Response.Headers.CacheControl.ToString());
        Assert.Equal("no-cache", context.Response.Headers.Pragma.ToString());
        Assert.Equal("0", context.Response.Headers.Expires.ToString());
        Assert.Equal("no-referrer", context.Response.Headers["Referrer-Policy"].ToString());
        Assert.Equal("nosniff", context.Response.Headers["X-Content-Type-Options"].ToString());
        Assert.Equal(
            "noindex, nofollow, noarchive",
            context.Response.Headers["X-Robots-Tag"].ToString());
    }

    [Fact]
    public void LegacyHandoffTombstoneCannotRedeemOrExposeCredentials()
    {
        DefaultHttpContext context = new();
        LegacyHandoffResponseSecurity.Apply(context.Response);
        string page = LegacyHandoffTombstoneRenderer.Render(
            "Razaltush VPN",
            "@RazaltushVpnBot");

        Assert.Contains("Ссылка устарела", page, StringComparison.Ordinal);
        Assert.Contains("Откройте подключение через Telegram", page, StringComparison.Ordinal);
        Assert.Contains("Открыть в Happ", page, StringComparison.Ordinal);
        Assert.Contains("https://t.me/RazaltushVpnBot", page, StringComparison.Ordinal);
        Assert.Contains("Повторно оплачивать", page, StringComparison.Ordinal);
        Assert.DoesNotContain("Получить ссылку", page, StringComparison.Ordinal);
        Assert.DoesNotContain("<script", page, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("/redeem", page, StringComparison.Ordinal);
        Assert.DoesNotContain("token=", page, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("Добавить Razaltush VPN в Happ", page, StringComparison.Ordinal);
        Assert.Equal(
            "private, no-store, max-age=0, must-revalidate",
            context.Response.Headers.CacheControl.ToString());
        Assert.Equal("DENY", context.Response.Headers["X-Frame-Options"].ToString());
        Assert.Equal("ru", context.Response.Headers["Content-Language"].ToString());
        Assert.Equal(
            "same-origin",
            context.Response.Headers["Cross-Origin-Opener-Policy"].ToString());
        Assert.Equal(
            "same-origin",
            context.Response.Headers["Cross-Origin-Resource-Policy"].ToString());
        Assert.Equal(
            "camera=(), microphone=(), geolocation=()",
            context.Response.Headers["Permissions-Policy"].ToString());
        Assert.Contains(
            "default-src 'none'",
            context.Response.Headers["Content-Security-Policy"].ToString(),
            StringComparison.Ordinal);
    }

    [Fact]
    public void LegacyHandoffTombstoneEncodesConfiguredTextAndRejectsUnsafeBotUsername()
    {
        string page = LegacyHandoffTombstoneRenderer.Render(
            "<img src=x onerror=alert(1)>",
            "https://evil.example/path");

        Assert.DoesNotContain("<img", page, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("&lt;img src=x onerror=alert(1)&gt;", page, StringComparison.Ordinal);
        Assert.Contains("https://t.me/RazaltushVpnBot", page, StringComparison.Ordinal);
        Assert.DoesNotContain("evil.example", page, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task FreshDatabaseReportsCurrentMigrationVersionWithoutLegacyImport()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();

        int migrationVersion = await fixture.Repository.AppliedMigrationCountAsync(
            CancellationToken.None);

        Assert.Equal(SqliteMediatorRepository.CurrentMigrationVersion, migrationVersion);
    }

    [Fact]
    public async Task MigrationSeventeenAddsResultVersionProvenanceIndexWithoutDataLoss()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 2);
        EntitlementOperationRequest request = new(
            "operation-migration-17",
            "admin_revoke",
            1,
            EntitlementStatuses.Disabled,
            DateTimeOffset.UtcNow.AddDays(30),
            2);
        EntitlementOperationResult applied = await fixture.Repository.ApplyEntitlementOperationAsync(
            publicGuid,
            request,
            DateTimeOffset.UtcNow,
            CancellationToken.None);

        await using (SqliteConnection connection = new($"Data Source={fixture.DatabasePath}"))
        {
            await connection.OpenAsync();
            await using SqliteCommand downgrade = connection.CreateCommand();
            downgrade.CommandText = """
                DROP INDEX ux_entitlement_operations_subscription_result_version;
                DELETE FROM mediator_migrations WHERE version = 17;
                """;
            await downgrade.ExecuteNonQueryAsync();
        }

        SqliteMediatorRepository migratedRepository = fixture.CreateRepository();
        await migratedRepository.InitializeAsync(CancellationToken.None);

        EntitlementOperationResult? queried =
            await migratedRepository.GetEntitlementOperationByResultVersionAsync(
                publicGuid,
                applied.ResultVersion!.Value,
                CancellationToken.None);
        Assert.NotNull(queried);
        Assert.Equal(request.OperationId, queried.OperationId);

        await using SqliteConnection verification = new($"Data Source={fixture.DatabasePath}");
        await verification.OpenAsync();
        await using SqliteCommand indexQuery = verification.CreateCommand();
        indexQuery.CommandText = """
            SELECT COUNT(*)
            FROM pragma_index_list('entitlement_operations')
            WHERE name = 'ux_entitlement_operations_subscription_result_version'
              AND [unique] = 1;
            """;
        Assert.Equal(1L, Convert.ToInt64(await indexQuery.ExecuteScalarAsync()));
    }

    [Fact]
    public async Task NewSubscriptionPersistsInitialEntitlementProvenance()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 2);

        EntitlementOperationResult? provenance =
            await fixture.Repository.GetEntitlementOperationByResultVersionAsync(
                publicGuid,
                1,
                CancellationToken.None);

        Assert.NotNull(provenance);
        Assert.Equal("subscription_creation", provenance.OperationType);
        Assert.Equal(0, provenance.ExpectedVersion);
        Assert.Equal(1, provenance.ResultVersion);
        Assert.Equal(EntitlementStatuses.Active, provenance.ResultStatus);
        Assert.Equal(2, provenance.ResultMaxDeviceTokens);
    }


    [Fact]
    public async Task MigrationTwentyFourBackfillsMissingCurrentSnapshotProvenance()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 2);

        await using (SqliteConnection connection = new($"Data Source={fixture.DatabasePath}"))
        {
            await connection.OpenAsync();
            await using SqliteCommand downgrade = connection.CreateCommand();
            downgrade.CommandText = """
                DELETE FROM entitlement_operations;
                DELETE FROM mediator_migrations WHERE version = 24;
                """;
            await downgrade.ExecuteNonQueryAsync();
        }

        SqliteMediatorRepository migratedRepository = fixture.CreateRepository();
        await migratedRepository.InitializeAsync(CancellationToken.None);

        EntitlementOperationResult? provenance =
            await migratedRepository.GetEntitlementOperationByResultVersionAsync(
                publicGuid,
                1,
                CancellationToken.None);

        Assert.NotNull(provenance);
        Assert.Equal("legacy_snapshot_import", provenance.OperationType);
        Assert.Equal(0, provenance.ExpectedVersion);
        Assert.Equal(1, provenance.ResultVersion);
        Assert.Equal(EntitlementStatuses.Active, provenance.ResultStatus);
        Assert.Equal(2, provenance.ResultMaxDeviceTokens);
    }


    [Fact]
    public async Task MigrationThirteenAddsIssuanceColumnsWithoutChangingLegacyCredential()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Legacy phone"),
            CreateRequestContext(),
            linkFactory,
            DateTimeOffset.UtcNow,
            CancellationToken.None,
            "device-issuance:migration-source-request");
        string originalHash;

        await using (SqliteConnection connection = new($"Data Source={fixture.DatabasePath}"))
        {
            await connection.OpenAsync();
            await using (SqliteCommand select = connection.CreateCommand())
            {
                select.CommandText = """
                    SELECT secret_hash
                    FROM device_access_tokens
                    WHERE public_id = $public_id;
                    """;
                select.Parameters.AddWithValue("$public_id", created.PublicId!);
                originalHash = Convert.ToString(await select.ExecuteScalarAsync())!;
            }

            await using SqliteCommand downgrade = connection.CreateCommand();
            downgrade.CommandText = """
                DROP INDEX ux_device_tokens_subscription_issuance;
                ALTER TABLE device_access_tokens DROP COLUMN issuance_key;
                ALTER TABLE device_access_tokens DROP COLUMN requested_platform;
                DELETE FROM mediator_migrations WHERE version = 13;
                """;
            await downgrade.ExecuteNonQueryAsync();
        }

        SqliteMediatorRepository migratedRepository = fixture.CreateRepository();
        await migratedRepository.InitializeAsync(CancellationToken.None);

        Assert.Equal(
            SqliteMediatorRepository.CurrentMigrationVersion,
            await migratedRepository.AppliedMigrationCountAsync(CancellationToken.None));

        await using SqliteConnection verification = new($"Data Source={fixture.DatabasePath}");
        await verification.OpenAsync();
        await using SqliteCommand columns = verification.CreateCommand();
        columns.CommandText = "PRAGMA table_info(device_access_tokens);";
        await using SqliteDataReader reader = await columns.ExecuteReaderAsync();
        HashSet<string> columnNames = [];

        while (await reader.ReadAsync())
        {
            columnNames.Add(reader.GetString(1));
        }

        await reader.DisposeAsync();
        await using SqliteCommand selectHash = verification.CreateCommand();
        selectHash.CommandText = """
            SELECT secret_hash
            FROM device_access_tokens
            WHERE public_id = $public_id;
            """;
        selectHash.Parameters.AddWithValue("$public_id", created.PublicId!);

        Assert.Contains("issuance_key", columnNames);
        Assert.Contains("requested_platform", columnNames);
        Assert.Equal(originalHash, Convert.ToString(await selectHash.ExecuteScalarAsync()));
    }

    [Theory]
    [InlineData("device-issuance:valid_request-123", true)]
    [InlineData("legacy-display-name:v1:not-allowed", false)]
    [InlineData("device-issuance:bad value", false)]
    [InlineData("device-issuance:x", false)]
    public void DeviceIssuanceKeysRequireNamespacedBoundedIdentifiers(
        string value,
        bool expected)
    {
        Assert.Equal(expected, DeviceIssuanceKeyValidator.TryNormalize(value, out _));
    }

    [Fact]
    public async Task RepeatedDeviceTokenCreationReturnsSameEncryptedCredentialWithoutRotation()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DefaultHttpContext context = CreateRequestContext();
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));

        DeviceTokenCreateResult first = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Phone"),
            context,
            linkFactory,
            DateTimeOffset.UtcNow,
            CancellationToken.None);
        DeviceTokenCreateResult second = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Phone"),
            context,
            linkFactory,
            DateTimeOffset.UtcNow,
            CancellationToken.None);
        DeviceCredentialResult retrieved = await fixture.Repository.GetDeviceCredentialAsync(
            publicGuid,
            first.PublicId!,
            context,
            linkFactory,
            CancellationToken.None);

        IReadOnlyList<DeviceTokenListItem> tokens = await fixture.Repository.ListDeviceTokensAsync(
            publicGuid,
            CancellationToken.None);

        Assert.Equal("created", first.Status);
        Assert.Equal("existing", second.Status);
        Assert.Equal(first.PublicId, second.PublicId);
        Assert.Equal(first.ConnectionUrl, second.ConnectionUrl);
        Assert.Equal(first.ConnectionUrl, retrieved.ConnectionUrl);
        Assert.False(second.PreviousPendingReplaced);
        Assert.Single(tokens, item => item.State == DeviceTokenStates.Pending);

        string rawSecret = ExtractTokenSecret(first.ConnectionUrl!);
        await using SqliteConnection connection = new($"Data Source={fixture.DatabasePath}");
        await connection.OpenAsync();
        await using SqliteCommand command = connection.CreateCommand();
        command.CommandText = """
            SELECT secret_hash, credential_ciphertext
            FROM device_access_tokens
            WHERE public_id = $public_id;
            """;
        command.Parameters.AddWithValue("$public_id", first.PublicId!);
        await using SqliteDataReader reader = await command.ExecuteReaderAsync();
        Assert.True(await reader.ReadAsync());
        Assert.DoesNotContain(rawSecret, reader.GetString(0), StringComparison.Ordinal);
        Assert.DoesNotContain(rawSecret, reader.GetString(1), StringComparison.Ordinal);
    }

    [Fact]
    public async Task DeviceIssuanceIdentitySeparatesIdenticalPlatformsAndReplaysOneRequest()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 2);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DateTimeOffset now = DateTimeOffset.UtcNow;
        const string firstIssuanceKey = "device-issuance:first-android-request";
        const string secondIssuanceKey = "device-issuance:second-android-request";

        DeviceTokenCreateResult first = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Android", "android"),
            CreateRequestContext(),
            linkFactory,
            now,
            CancellationToken.None,
            firstIssuanceKey);
        DeviceTokenCreateResult replay = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Android", "android"),
            CreateRequestContext(),
            linkFactory,
            now.AddSeconds(1),
            CancellationToken.None,
            firstIssuanceKey);
        DeviceTokenCreateResult second = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Android", "android"),
            CreateRequestContext(),
            linkFactory,
            now.AddSeconds(2),
            CancellationToken.None,
            secondIssuanceKey);

        IReadOnlyList<DeviceTokenListItem> devices = await fixture.Repository.ListDeviceTokensAsync(
            publicGuid,
            CancellationToken.None);

        Assert.Equal("created", first.Status);
        Assert.Equal("existing", replay.Status);
        Assert.Equal(first.PublicId, replay.PublicId);
        Assert.Equal(first.ConnectionUrl, replay.ConnectionUrl);
        Assert.Equal("created", second.Status);
        Assert.NotEqual(first.PublicId, second.PublicId);
        Assert.Equal(2, devices.Count(item => item.State == DeviceTokenStates.Pending));

        await using SqliteConnection connection = new($"Data Source={fixture.DatabasePath}");
        await connection.OpenAsync();
        await using SqliteCommand command = connection.CreateCommand();
        command.CommandText = """
            SELECT issuance_key, requested_platform
            FROM device_access_tokens
            WHERE public_id IN ($first_public_id, $second_public_id)
            ORDER BY issuance_key;
            """;
        command.Parameters.AddWithValue("$first_public_id", first.PublicId!);
        command.Parameters.AddWithValue("$second_public_id", second.PublicId!);
        await using SqliteDataReader reader = await command.ExecuteReaderAsync();
        List<(string IssuanceKey, string RequestedPlatform)> identities = [];

        while (await reader.ReadAsync())
        {
            identities.Add((reader.GetString(0), reader.GetString(1)));
        }

        Assert.Equal(
            [
                (firstIssuanceKey, "android"),
                (secondIssuanceKey, "android")
            ],
            identities);
    }

    [Fact]
    public async Task ConcurrentRepositoriesCreateOnlyOneTokenForOneIssuanceRequest()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 2);
        SqliteMediatorRepository secondRepository = fixture.CreateRepository();
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DateTimeOffset now = DateTimeOffset.UtcNow;
        const string issuanceKey = "device-issuance:concurrent-android-request";

        Task<DeviceTokenCreateResult> firstTask = fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Android", "android"),
            CreateRequestContext(),
            linkFactory,
            now,
            CancellationToken.None,
            issuanceKey);
        Task<DeviceTokenCreateResult> secondTask = secondRepository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Android", "android"),
            CreateRequestContext(),
            linkFactory,
            now,
            CancellationToken.None,
            issuanceKey);

        DeviceTokenCreateResult[] results = await Task.WhenAll(firstTask, secondTask);
        IReadOnlyList<DeviceTokenListItem> devices = await fixture.Repository.ListDeviceTokensAsync(
            publicGuid,
            CancellationToken.None);

        Assert.Equal(["created", "existing"], results.Select(item => item.Status).Order().ToArray());
        Assert.Single(results.Select(item => item.PublicId).Distinct());
        Assert.Single(devices, item => item.State == DeviceTokenStates.Pending);
    }

    [Fact]
    public async Task ValidLegacyFetchBackfillsCredentialWithoutChangingDeviceIdentity()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DateTimeOffset now = DateTimeOffset.UtcNow;
        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Legacy phone"),
            CreateRequestContext(),
            linkFactory,
            now,
            CancellationToken.None,
            "device-issuance:legacy-backfill-request");
        string rawSecret = ExtractTokenSecret(created.ConnectionUrl!);

        await using (SqliteConnection connection = new($"Data Source={fixture.DatabasePath}"))
        {
            await connection.OpenAsync();
            await using SqliteCommand command = connection.CreateCommand();
            command.CommandText = """
                UPDATE device_access_tokens
                SET credential_key_id = NULL,
                    credential_nonce = NULL,
                    credential_ciphertext = NULL,
                    credential_tag = NULL
                WHERE public_id = $public_id;
                """;
            command.Parameters.AddWithValue("$public_id", created.PublicId!);
            Assert.Equal(1, await command.ExecuteNonQueryAsync());
        }

        DeviceCredentialResult beforeFetch = await fixture.Repository.GetDeviceCredentialAsync(
            publicGuid,
            created.PublicId!,
            CreateRequestContext(),
            linkFactory,
            CancellationToken.None);
        TokenSubscriptionAccessResult access = await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            created.PublicId!,
            rawSecret,
            now.AddSeconds(1),
            CancellationToken.None);
        DeviceCredentialResult afterFetch = await fixture.Repository.GetDeviceCredentialAsync(
            publicGuid,
            created.PublicId!,
            CreateRequestContext(),
            linkFactory,
            CancellationToken.None);

        Assert.Equal("invalid", beforeFetch.Status);
        Assert.Equal("credential_reissue_required", beforeFetch.ErrorCode);
        Assert.True(access.Allowed);
        Assert.Equal("available", afterFetch.Status);
        Assert.Equal(created.PublicId, afterFetch.PublicId);
        Assert.Equal(created.ConnectionUrl, afterFetch.ConnectionUrl);
    }

    [Fact]
    public async Task LegacyHashOnlyRetryCreatesAtMostOneRecoverableReplacement()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 2);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DateTimeOffset now = DateTimeOffset.UtcNow;
        DeviceTokenCreateResult legacy = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Android"),
            CreateRequestContext(),
            linkFactory,
            now,
            CancellationToken.None);

        await using (SqliteConnection connection = new($"Data Source={fixture.DatabasePath}"))
        {
            await connection.OpenAsync();
            await using SqliteCommand command = connection.CreateCommand();
            command.CommandText = """
                UPDATE device_access_tokens
                SET issuance_key = NULL,
                    credential_key_id = NULL,
                    credential_nonce = NULL,
                    credential_ciphertext = NULL,
                    credential_tag = NULL
                WHERE public_id = $public_id;
                """;
            command.Parameters.AddWithValue("$public_id", legacy.PublicId!);
            Assert.Equal(1, await command.ExecuteNonQueryAsync());
        }

        DeviceTokenCreateResult replacement = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Android"),
            CreateRequestContext(),
            linkFactory,
            now.AddSeconds(1),
            CancellationToken.None);
        DeviceTokenCreateResult replay = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Android"),
            CreateRequestContext(),
            linkFactory,
            now.AddSeconds(2),
            CancellationToken.None);
        IReadOnlyList<DeviceTokenListItem> devices = await fixture.Repository.ListDeviceTokensAsync(
            publicGuid,
            CancellationToken.None);

        Assert.Equal("created", replacement.Status);
        Assert.Equal("existing", replay.Status);
        Assert.Equal(replacement.PublicId, replay.PublicId);
        Assert.NotEqual(legacy.PublicId, replacement.PublicId);
        Assert.Equal(2, devices.Count(item => item.State == DeviceTokenStates.Pending));
    }

    [Fact]
    public async Task RegenerateDeviceTokenRevokesOldCredentialAndKeepsOtherDevices()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 2);
        DefaultHttpContext context = CreateRequestContext();
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DateTimeOffset now = DateTimeOffset.UtcNow;

        DeviceTokenCreateResult phone = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Phone"),
            context,
            linkFactory,
            now,
            CancellationToken.None);
        DeviceTokenCreateResult tablet = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Tablet"),
            context,
            linkFactory,
            now,
            CancellationToken.None);
        DeviceTokenCreateResult regenerated = await fixture.Repository.RegenerateDeviceTokenAsync(
            publicGuid,
            phone.PublicId!,
            context,
            linkFactory,
            now.AddSeconds(1),
            CancellationToken.None);

        TokenSubscriptionAccessResult oldPhone = await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            phone.PublicId!,
            ExtractTokenSecret(phone.ConnectionUrl!),
            now.AddSeconds(2),
            CancellationToken.None);
        TokenSubscriptionAccessResult newPhone = await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            regenerated.PublicId!,
            ExtractTokenSecret(regenerated.ConnectionUrl!),
            now.AddSeconds(2),
            CancellationToken.None);
        TokenSubscriptionAccessResult tabletAccess = await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            tablet.PublicId!,
            ExtractTokenSecret(tablet.ConnectionUrl!),
            now.AddSeconds(2),
            CancellationToken.None);

        Assert.False(oldPhone.Allowed);
        Assert.Equal(UserFacingStatus.DeviceTokenRevoked, oldPhone.Status);
        Assert.True(newPhone.Allowed);
        Assert.True(tabletAccess.Allowed);
        Assert.NotEqual(phone.PublicId, regenerated.PublicId);
    }

    [Fact]
    public async Task RegenerateTransfersIssuanceIdentityToReplacementCredential()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DateTimeOffset now = DateTimeOffset.UtcNow;
        const string issuanceKey = "device-issuance:regenerated-android-request";
        DeviceTokenCreateResult original = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Android", "android"),
            CreateRequestContext(),
            linkFactory,
            now,
            CancellationToken.None,
            issuanceKey);
        DeviceTokenCreateResult regenerated = await fixture.Repository.RegenerateDeviceTokenAsync(
            publicGuid,
            original.PublicId!,
            CreateRequestContext(),
            linkFactory,
            now.AddSeconds(1),
            CancellationToken.None);
        DeviceTokenCreateResult replay = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Android", "android"),
            CreateRequestContext(),
            linkFactory,
            now.AddSeconds(2),
            CancellationToken.None,
            issuanceKey);

        Assert.Equal("created", regenerated.Status);
        Assert.Equal("existing", replay.Status);
        Assert.Equal(regenerated.PublicId, replay.PublicId);
        Assert.NotEqual(original.PublicId, regenerated.PublicId);

        await using SqliteConnection connection = new($"Data Source={fixture.DatabasePath}");
        await connection.OpenAsync();
        await using SqliteCommand command = connection.CreateCommand();
        command.CommandText = """
            SELECT public_id, revoked_at_utc, issuance_key, issuance_request_hash,
                   requested_platform
            FROM device_access_tokens
            WHERE public_id IN ($old_public_id, $new_public_id)
            ORDER BY public_id;
            """;
        command.Parameters.AddWithValue("$old_public_id", original.PublicId!);
        command.Parameters.AddWithValue("$new_public_id", regenerated.PublicId!);
        await using SqliteDataReader reader = await command.ExecuteReaderAsync();
        Dictionary<string, (
            string? RevokedAtUtc,
            string? IssuanceKey,
            string? IssuanceRequestHash,
            string? Platform)> rows = [];

        while (await reader.ReadAsync())
        {
            rows[reader.GetString(0)] = (
                reader.IsDBNull(1) ? null : reader.GetString(1),
                reader.IsDBNull(2) ? null : reader.GetString(2),
                reader.IsDBNull(3) ? null : reader.GetString(3),
                reader.IsDBNull(4) ? null : reader.GetString(4));
        }

        Assert.NotNull(rows[original.PublicId!].RevokedAtUtc);
        Assert.Null(rows[original.PublicId!].IssuanceKey);
        Assert.Null(rows[original.PublicId!].IssuanceRequestHash);
        Assert.Null(rows[regenerated.PublicId!].RevokedAtUtc);
        Assert.Equal(issuanceKey, rows[regenerated.PublicId!].IssuanceKey);
        Assert.Equal(64, rows[regenerated.PublicId!].IssuanceRequestHash?.Length);
        Assert.Equal("android", rows[regenerated.PublicId!].Platform);
    }

    [Fact]
    public async Task MissingCredentialKeyReturnsTypedRecoveryError()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Phone"),
            CreateRequestContext(),
            linkFactory,
            DateTimeOffset.UtcNow,
            CancellationToken.None,
            "device-issuance:missing-key-request");

        fixture.Options.DeviceCredentialProtectionKeyId = "new-key";
        fixture.Options.DeviceCredentialProtectionKey = Convert.ToBase64String(
            RandomNumberGenerator.GetBytes(32));
        fixture.Options.PreviousDeviceCredentialProtectionKeyId = null;
        fixture.Options.PreviousDeviceCredentialProtectionKey = null;
        SqliteMediatorRepository repositoryWithoutOldKey = fixture.CreateRepository();

        DeviceCredentialResult result = await repositoryWithoutOldKey.GetDeviceCredentialAsync(
            publicGuid,
            created.PublicId!,
            CreateRequestContext(),
            linkFactory,
            CancellationToken.None);

        Assert.Equal("invalid", result.Status);
        Assert.Equal("credential_key_unavailable", result.ErrorCode);
    }

    [Fact]
    public async Task PreviousCredentialKeyIsReadRepairedToCurrentKey()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        string previousKeyId = fixture.Options.DeviceCredentialProtectionKeyId;
        string previousKey = fixture.Options.DeviceCredentialProtectionKey!;
        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Phone"),
            CreateRequestContext(),
            linkFactory,
            DateTimeOffset.UtcNow,
            CancellationToken.None,
            "device-issuance:key-rotation-request");

        fixture.Options.DeviceCredentialProtectionKeyId = "current-v2";
        fixture.Options.DeviceCredentialProtectionKey = Convert.ToBase64String(
            RandomNumberGenerator.GetBytes(32));
        fixture.Options.PreviousDeviceCredentialProtectionKeyId = previousKeyId;
        fixture.Options.PreviousDeviceCredentialProtectionKey = previousKey;
        SqliteMediatorRepository rotatedRepository = fixture.CreateRepository();

        DeviceCredentialResult result = await rotatedRepository.GetDeviceCredentialAsync(
            publicGuid,
            created.PublicId!,
            CreateRequestContext(),
            linkFactory,
            CancellationToken.None);

        Assert.Equal("available", result.Status);
        Assert.Equal(created.ConnectionUrl, result.ConnectionUrl);

        await using SqliteConnection connection = new($"Data Source={fixture.DatabasePath}");
        await connection.OpenAsync();
        await using SqliteCommand command = connection.CreateCommand();
        command.CommandText = """
            SELECT credential_key_id
            FROM device_access_tokens
            WHERE public_id = $public_id;
            """;
        command.Parameters.AddWithValue("$public_id", created.PublicId!);
        Assert.Equal("current-v2", Convert.ToString(await command.ExecuteScalarAsync()));
    }


    [Fact]
    public async Task FirstDeviceTokenAccessActivatesAndLimitBlocksNewPending()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        DateTimeOffset validUntilUtc = DateTimeOffset.UtcNow.AddDays(17);
        Guid publicGuid = await fixture.CreateSubscriptionAsync(
            maxDevices: 1,
            validUntilUtc);
        DefaultHttpContext context = CreateRequestContext();
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));

        DeviceTokenCreateResult token = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Phone"),
            context,
            linkFactory,
            DateTimeOffset.UtcNow,
            CancellationToken.None);

        TokenSubscriptionAccessResult access = await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            token.PublicId!,
            ExtractTokenSecret(token.ConnectionUrl!),
            DateTimeOffset.UtcNow,
            CancellationToken.None);
        DeviceTokenCreateResult blocked = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Tablet"),
            context,
            linkFactory,
            DateTimeOffset.UtcNow,
            CancellationToken.None);

        Assert.True(access.Allowed);
        Assert.Equal(1, access.ActiveDeviceTokens);
        Assert.Equal(
            validUntilUtc.ToUnixTimeMilliseconds(),
            access.ValidUntilUtc?.ToUnixTimeMilliseconds());
        Assert.Equal("limit_reached", blocked.Status);
        Assert.Equal("device_limit_reached", blocked.ErrorCode);
    }

    [Fact]
    public async Task DeviceAccessStoresOnlyNormalizedMetadata()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DefaultHttpContext context = CreateRequestContext();
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));

        DeviceTokenCreateResult token = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest(null),
            context,
            linkFactory,
            DateTimeOffset.UtcNow,
            CancellationToken.None);

        TokenSubscriptionAccessResult access = await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            token.PublicId!,
            ExtractTokenSecret(token.ConnectionUrl!),
            DateTimeOffset.UtcNow,
            CancellationToken.None,
            "Mozilla/5.0 (Linux; Android 14; HONOR 90) AppleWebKit/537.36");
        IReadOnlyList<DeviceTokenListItem> devices = await fixture.Repository.ListDeviceTokensAsync(
            publicGuid,
            CancellationToken.None);

        Assert.True(access.Allowed);
        DeviceTokenListItem device = Assert.Single(devices, item => item.PublicId == token.PublicId);
        Assert.Equal("android", device.Platform);
        Assert.Equal("HONOR 90", device.DetectedModel);
        Assert.Equal("user_agent_normalized", device.DetectionSource);
    }

    [Fact]
    public void DeviceMetadataDetectorRejectsLowConfidenceAndroidBuildStrings()
    {
        DeviceMetadata metadata = DeviceMetadataDetector.Detect(
            "Mozilla/5.0 (Linux; Android 14; Pixel 8 Build/AP1A.240505.005)");

        Assert.Equal("android", metadata.Platform);
        Assert.Equal("Android-устройство", metadata.DetectedModel);
    }

    [Fact]
    public async Task ExplicitExpiredEntitlementReturnsExpiredDeviceError()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DateTimeOffset now = DateTimeOffset.UtcNow;
        EntitlementUpdateResult update = await fixture.Repository.ApplyEntitlementAsync(
            publicGuid,
            new EntitlementUpdateRequest(
                2,
                EntitlementStatuses.Expired,
                now.AddMinutes(-1),
                1),
            now,
            CancellationToken.None);
        DefaultHttpContext context = CreateRequestContext();
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));

        DeviceTokenCreateResult result = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Phone"),
            context,
            linkFactory,
            now,
            CancellationToken.None);

        Assert.Equal(EntitlementUpdateStatus.Applied, update.Status);
        Assert.Equal("subscription_expired", result.ErrorCode);
    }

    [Fact]
    public async Task DisabledEntitlementRemainsDisabledEvenWhenValidityIsPast()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DateTimeOffset now = DateTimeOffset.UtcNow;
        EntitlementUpdateResult update = await fixture.Repository.ApplyEntitlementAsync(
            publicGuid,
            new EntitlementUpdateRequest(
                2,
                EntitlementStatuses.Disabled,
                now.AddMinutes(-1),
                1),
            now,
            CancellationToken.None);
        DefaultHttpContext context = CreateRequestContext();
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));

        DeviceTokenCreateResult result = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Phone"),
            context,
            linkFactory,
            now,
            CancellationToken.None);

        Assert.Equal(EntitlementUpdateStatus.Applied, update.Status);
        Assert.Equal("subscription_disabled", result.ErrorCode);
    }

    [Theory]
    [InlineData(EntitlementStatuses.Expired, UserFacingStatus.SubscriptionExpired)]
    [InlineData(EntitlementStatuses.Disabled, UserFacingStatus.SubscriptionDisabled)]
    public async Task ExistingDevicePreservesExpiredVersusDisabledSemantics(
        string entitlementStatus,
        UserFacingStatus expectedStatus)
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DateTimeOffset now = DateTimeOffset.UtcNow;
        DefaultHttpContext context = CreateRequestContext();
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DeviceTokenCreateResult token = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Phone"),
            context,
            linkFactory,
            now,
            CancellationToken.None);
        EntitlementUpdateResult update = await fixture.Repository.ApplyEntitlementAsync(
            publicGuid,
            new EntitlementUpdateRequest(
                2,
                entitlementStatus,
                now.AddMinutes(-1),
                1),
            now,
            CancellationToken.None);

        TokenSubscriptionAccessResult access =
            await fixture.Repository.ValidateDeviceTokenAccessAsync(
                publicGuid,
                token.PublicId!,
                ExtractTokenSecret(token.ConnectionUrl!),
                now,
                CancellationToken.None,
                "test-agent");

        Assert.Equal(EntitlementUpdateStatus.Applied, update.Status);
        Assert.False(access.Allowed);
        Assert.Equal(expectedStatus, access.Status);
    }

    [Fact]
    public async Task EntitlementCannotReduceLimitBelowActiveDevices()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 2);
        DefaultHttpContext context = CreateRequestContext();
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));

        for (int index = 0; index < 2; index++)
        {
            DeviceTokenCreateResult token = await fixture.Repository.CreateDeviceTokenAsync(
                publicGuid,
                new CreateDeviceTokenRequest($"Device {index}"),
                context,
                linkFactory,
                DateTimeOffset.UtcNow,
                CancellationToken.None);
            _ = await fixture.Repository.ValidateDeviceTokenAccessAsync(
                publicGuid,
                token.PublicId!,
                ExtractTokenSecret(token.ConnectionUrl!),
                DateTimeOffset.UtcNow,
                CancellationToken.None);
        }

        EntitlementUpdateResult result = await fixture.Repository.ApplyEntitlementAsync(
            publicGuid,
            new EntitlementUpdateRequest(2, EntitlementStatuses.Active, DateTimeOffset.UtcNow.AddDays(30), 1),
            DateTimeOffset.UtcNow,
            CancellationToken.None);

        Assert.Equal(EntitlementUpdateStatus.DeviceLimitDecreaseNotAllowed, result.Status);
        Assert.Equal(2, result.ActiveDeviceTokens);
    }

    [Fact]
    public async Task EntitlementCannotDecreaseDeviceLimitEvenWithoutActiveDevices()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 3);

        EntitlementUpdateResult result = await fixture.Repository.ApplyEntitlementAsync(
            publicGuid,
            new EntitlementUpdateRequest(2, EntitlementStatuses.Active, DateTimeOffset.UtcNow.AddDays(30), 2),
            DateTimeOffset.UtcNow,
            CancellationToken.None);

        Assert.Equal(EntitlementUpdateStatus.DeviceLimitDecreaseNotAllowed, result.Status);
        Assert.Equal(0, result.ActiveDeviceTokens);
    }

    [Fact]
    public async Task LegacyAccessIsDisabledByDefault()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);

        TokenSubscriptionAccessResult access = await fixture.Repository.ValidateLegacyAccessAsync(
            publicGuid,
            DateTimeOffset.UtcNow,
            CancellationToken.None);

        Assert.False(access.Allowed);
        Assert.Equal(UserFacingStatus.LegacyLinkDisabled, access.Status);
    }

    [Fact]
    public async Task LegacyAccessIncludesSubscriptionExpiration()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        fixture.Options.AllowLegacySubscriptionLinks = true;
        DateTimeOffset now = DateTimeOffset.UtcNow;
        DateTimeOffset validUntilUtc = now.AddDays(21);
        Guid publicGuid = await fixture.CreateSubscriptionAsync(
            maxDevices: 1,
            validUntilUtc);

        TokenSubscriptionAccessResult access = await fixture.Repository.ValidateLegacyAccessAsync(
            publicGuid,
            now,
            CancellationToken.None);

        Assert.True(access.Allowed);
        Assert.Equal(
            validUntilUtc.ToUnixTimeMilliseconds(),
            access.ValidUntilUtc?.ToUnixTimeMilliseconds());
    }

    [Fact]
    public async Task EntitlementOperationIsDurablyIdempotentAndQueryable()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 2);
        DateTimeOffset validUntil = DateTimeOffset.UtcNow.AddDays(60);
        EntitlementOperationRequest request = new(
            "operation-idempotency-001",
            "paid_renewal",
            1,
            EntitlementStatuses.Active,
            validUntil,
            3);

        EntitlementOperationResult first = await fixture.Repository.ApplyEntitlementOperationAsync(
            publicGuid,
            request,
            DateTimeOffset.UtcNow,
            CancellationToken.None);
        EntitlementOperationResult replay = await fixture.Repository.ApplyEntitlementOperationAsync(
            publicGuid,
            request,
            DateTimeOffset.UtcNow.AddSeconds(1),
            CancellationToken.None);
        EntitlementOperationResult? queried = await fixture.Repository.GetEntitlementOperationAsync(
            request.OperationId,
            CancellationToken.None);

        Assert.Equal(EntitlementOperationStatus.Applied, first.Status);
        Assert.Equal(EntitlementOperationStatus.AlreadyApplied, replay.Status);
        Assert.NotNull(queried);
        Assert.Equal(first.ResultVersion, queried.ResultVersion);
        Assert.Equal(first.ResultValidUntilUtc, queried.ResultValidUntilUtc);
        Assert.Equal(2, queried.ResultVersion);
    }

    [Fact]
    public async Task EntitlementOperationCanBeQueriedBySubscriptionAndResultVersion()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 2);
        EntitlementOperationRequest request = new(
            "operation-provenance-001",
            "admin_revoke",
            1,
            EntitlementStatuses.Disabled,
            DateTimeOffset.UtcNow.AddDays(30),
            2);

        EntitlementOperationResult applied = await fixture.Repository.ApplyEntitlementOperationAsync(
            publicGuid,
            request,
            DateTimeOffset.UtcNow,
            CancellationToken.None);
        EntitlementOperationResult? queried =
            await fixture.Repository.GetEntitlementOperationByResultVersionAsync(
                publicGuid,
                applied.ResultVersion!.Value,
                CancellationToken.None);
        EntitlementOperationResult? wrongVersion =
            await fixture.Repository.GetEntitlementOperationByResultVersionAsync(
                publicGuid,
                applied.ResultVersion.Value + 1,
                CancellationToken.None);
        EntitlementOperationResult? wrongSubscription =
            await fixture.Repository.GetEntitlementOperationByResultVersionAsync(
                Guid.NewGuid(),
                applied.ResultVersion.Value,
                CancellationToken.None);

        Assert.NotNull(queried);
        Assert.Equal(request.OperationId, queried.OperationId);
        Assert.Equal("admin_revoke", queried.OperationType);
        Assert.Equal(EntitlementStatuses.Disabled, queried.ResultStatus);
        Assert.Null(wrongVersion);
        Assert.Null(wrongSubscription);
    }

    [Fact]
    public async Task EntitlementOperationRejectsPayloadReuseAndPreservesVersionValidation()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 2);
        EntitlementOperationRequest request = new(
            "operation-conflict-001",
            "paid_renewal",
            1,
            EntitlementStatuses.Active,
            DateTimeOffset.UtcNow.AddDays(60),
            2);
        _ = await fixture.Repository.ApplyEntitlementOperationAsync(
            publicGuid,
            request,
            DateTimeOffset.UtcNow,
            CancellationToken.None);

        EntitlementOperationResult payloadConflict = await fixture.Repository.ApplyEntitlementOperationAsync(
            publicGuid,
            request with { MaxDeviceTokens = 3 },
            DateTimeOffset.UtcNow.AddSeconds(1),
            CancellationToken.None);
        EntitlementOperationResult versionConflict = await fixture.Repository.ApplyEntitlementOperationAsync(
            publicGuid,
            new EntitlementOperationRequest(
                "operation-stale-version-001",
                "expiration",
                1,
                EntitlementStatuses.Disabled,
                DateTimeOffset.UtcNow,
                2),
            DateTimeOffset.UtcNow.AddSeconds(2),
            CancellationToken.None);

        Assert.Equal(EntitlementOperationStatus.IdempotencyConflict, payloadConflict.Status);
        Assert.Equal(EntitlementOperationStatus.VersionConflict, versionConflict.Status);
        Assert.Equal(2, versionConflict.ExpectedVersion);
    }

    private static DefaultHttpContext CreateRequestContext()
    {
        DefaultHttpContext context = new();
        context.Request.Scheme = "https";
        context.Request.Host = new HostString("vpn.example");
        return context;
    }

    private static string ExtractTokenSecret(string connectionUrl)
    {
        Uri uri = new(connectionUrl);
        string query = uri.Query.TrimStart('?');

        foreach (string part in query.Split('&', StringSplitOptions.RemoveEmptyEntries))
        {
            string[] pieces = part.Split('=', 2);

            if (pieces.Length == 2 && pieces[0] == "token")
            {
                return Uri.UnescapeDataString(pieces[1]);
            }
        }

        throw new InvalidOperationException("Token query parameter was not found.");
    }

    [Fact]
    public async Task MigrationNineteenAddsRequestHashWithoutChangingCredentialOrPolicy()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        fixture.Options.DefaultNewDeviceFeedPolicy = DeviceFeedPolicyModes.Enforce;
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Protected Android", "android"),
            CreateRequestContext(),
            linkFactory,
            DateTimeOffset.UtcNow,
            CancellationToken.None,
            "device-issuance:migration-nineteen-source");
        string originalSecretHash;

        await using (SqliteConnection connection = new($"Data Source={fixture.DatabasePath}"))
        {
            await connection.OpenAsync();
            await using (SqliteCommand initialSelect = connection.CreateCommand())
            {
                initialSelect.CommandText = """
                    SELECT secret_hash
                    FROM device_access_tokens
                    WHERE public_id = $public_id;
                    """;
                initialSelect.Parameters.AddWithValue("$public_id", created.PublicId!);
                originalSecretHash = Convert.ToString(await initialSelect.ExecuteScalarAsync())!;
            }

            await using SqliteCommand downgrade = connection.CreateCommand();
            downgrade.CommandText = """
                ALTER TABLE device_access_tokens DROP COLUMN issuance_request_hash;
                DELETE FROM mediator_migrations WHERE version = 19;
                """;
            await downgrade.ExecuteNonQueryAsync();
        }

        SqliteMediatorRepository migratedRepository = fixture.CreateRepository();
        await migratedRepository.InitializeAsync(CancellationToken.None);

        await using SqliteConnection verification = new($"Data Source={fixture.DatabasePath}");
        await verification.OpenAsync();
        await using SqliteCommand select = verification.CreateCommand();
        select.CommandText = """
            SELECT secret_hash, feed_policy_mode, binding_state, issuance_request_hash
            FROM device_access_tokens
            WHERE public_id = $public_id;
            """;
        select.Parameters.AddWithValue("$public_id", created.PublicId!);
        await using SqliteDataReader reader = await select.ExecuteReaderAsync();

        Assert.True(await reader.ReadAsync());
        Assert.Equal(originalSecretHash, reader.GetString(0));
        Assert.Equal(DeviceFeedPolicyModes.Enforce, reader.GetString(1));
        Assert.Equal(DeviceFeedBindingStates.Unbound, reader.GetString(2));
        Assert.True(reader.IsDBNull(3));
        Assert.Equal(
            SqliteMediatorRepository.CurrentMigrationVersion,
            await migratedRepository.AppliedMigrationCountAsync(CancellationToken.None));
    }

    [Fact]
    public async Task ProtectedIssuancePersistsExplicitPolicyAndRequestFingerprint()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        fixture.Options.DefaultNewDeviceFeedPolicy = DeviceFeedPolicyModes.Enforce;
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));

        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Android", "android"),
            CreateRequestContext(),
            linkFactory,
            DateTimeOffset.UtcNow,
            CancellationToken.None,
            "device-issuance:protected-policy-request");
        DeviceTokenListItem token = Assert.Single(
            await fixture.Repository.ListDeviceTokensAsync(publicGuid, CancellationToken.None));

        await using SqliteConnection connection = new($"Data Source={fixture.DatabasePath}");
        await connection.OpenAsync();
        await using SqliteCommand command = connection.CreateCommand();
        command.CommandText = """
            SELECT feed_policy_version, issuance_request_hash
            FROM device_access_tokens
            WHERE public_id = $public_id;
            """;
        command.Parameters.AddWithValue("$public_id", created.PublicId!);
        await using SqliteDataReader reader = await command.ExecuteReaderAsync();

        Assert.True(await reader.ReadAsync());
        Assert.Equal(1, reader.GetInt32(0));
        Assert.Equal(64, reader.GetString(1).Length);
        Assert.Equal(DeviceFeedPolicyModes.Enforce, token.FeedPolicyMode);
        Assert.Equal(DeviceFeedBindingStates.Unbound, token.BindingState);
    }

    [Fact]
    public async Task ProtectedCompatibilityIssuanceDoesNotReuseLegacyCredential()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 2);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DateTimeOffset now = DateTimeOffset.UtcNow;
        fixture.Options.DefaultNewDeviceFeedPolicy = DeviceFeedPolicyModes.Legacy;
        DeviceTokenCreateResult legacy = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Android", "android"),
            CreateRequestContext(),
            linkFactory,
            now,
            CancellationToken.None);

        fixture.Options.DefaultNewDeviceFeedPolicy = DeviceFeedPolicyModes.Enforce;
        DeviceTokenCreateResult protectedToken = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Android", "android"),
            CreateRequestContext(),
            linkFactory,
            now.AddSeconds(1),
            CancellationToken.None);
        DeviceTokenCreateResult replay = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Android", "android"),
            CreateRequestContext(),
            linkFactory,
            now.AddSeconds(2),
            CancellationToken.None);
        IReadOnlyList<DeviceTokenListItem> devices = await fixture.Repository.ListDeviceTokensAsync(
            publicGuid,
            CancellationToken.None);

        Assert.Equal("created", legacy.Status);
        Assert.Equal("created", protectedToken.Status);
        Assert.Equal("existing", replay.Status);
        Assert.NotEqual(legacy.PublicId, protectedToken.PublicId);
        Assert.Equal(protectedToken.PublicId, replay.PublicId);
        Assert.Contains(
            devices,
            item => item.PublicId == legacy.PublicId
                && item.FeedPolicyMode == DeviceFeedPolicyModes.Legacy
                && item.BindingState == DeviceFeedBindingStates.Grandfathered);
        Assert.Contains(
            devices,
            item => item.PublicId == protectedToken.PublicId
                && item.FeedPolicyMode == DeviceFeedPolicyModes.Enforce
                && item.BindingState == DeviceFeedBindingStates.Unbound);
    }

    [Fact]
    public async Task RequiredIssuanceKeyRejectsCompatibilityRequestWithoutOccupyingSlot()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        fixture.Options.RequireDeviceIssuanceKey = true;
        fixture.Options.DefaultNewDeviceFeedPolicy = DeviceFeedPolicyModes.Enforce;
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));

        DeviceTokenCreateResult rejected = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Android", "android"),
            CreateRequestContext(),
            linkFactory,
            DateTimeOffset.UtcNow,
            CancellationToken.None);
        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Android", "android"),
            CreateRequestContext(),
            linkFactory,
            DateTimeOffset.UtcNow.AddSeconds(1),
            CancellationToken.None,
            "device-issuance:required-key-request");

        Assert.Equal("invalid", rejected.Status);
        Assert.Equal("device_issuance_key_required", rejected.ErrorCode);
        Assert.Equal("created", created.Status);
        Assert.Single(
            await fixture.Repository.ListDeviceTokensAsync(publicGuid, CancellationToken.None));
    }

    [Fact]
    public async Task ReusingIssuanceKeyWithDifferentSemanticRequestIsRejected()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        fixture.Options.DefaultNewDeviceFeedPolicy = DeviceFeedPolicyModes.Enforce;
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 2);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        const string issuanceKey = "device-issuance:semantic-conflict-request";

        DeviceTokenCreateResult first = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Android", "android"),
            CreateRequestContext(),
            linkFactory,
            DateTimeOffset.UtcNow,
            CancellationToken.None,
            issuanceKey);
        DeviceTokenCreateResult conflict = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Linux", "linux"),
            CreateRequestContext(),
            linkFactory,
            DateTimeOffset.UtcNow.AddSeconds(1),
            CancellationToken.None,
            issuanceKey);

        Assert.Equal("created", first.Status);
        Assert.Equal("invalid", conflict.Status);
        Assert.Equal("idempotency_key_reused", conflict.ErrorCode);
        Assert.Single(
            await fixture.Repository.ListDeviceTokensAsync(publicGuid, CancellationToken.None));
    }

    [Fact]
    public async Task LegacyIssuanceFingerprintIsBackfilledOnCompatibleReplay()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        const string issuanceKey = "device-issuance:backfill-fingerprint-request";
        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Android", "android"),
            CreateRequestContext(),
            linkFactory,
            DateTimeOffset.UtcNow,
            CancellationToken.None,
            issuanceKey);

        await using (SqliteConnection connection = new($"Data Source={fixture.DatabasePath}"))
        {
            await connection.OpenAsync();
            await using SqliteCommand clear = connection.CreateCommand();
            clear.CommandText = """
                UPDATE device_access_tokens
                SET issuance_request_hash = NULL
                WHERE public_id = $public_id;
                """;
            clear.Parameters.AddWithValue("$public_id", created.PublicId!);
            Assert.Equal(1, await clear.ExecuteNonQueryAsync());
        }

        DeviceTokenCreateResult replay = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Happ · Android", "android"),
            CreateRequestContext(),
            linkFactory,
            DateTimeOffset.UtcNow.AddSeconds(1),
            CancellationToken.None,
            issuanceKey);

        await using SqliteConnection verification = new($"Data Source={fixture.DatabasePath}");
        await verification.OpenAsync();
        await using SqliteCommand select = verification.CreateCommand();
        select.CommandText = """
            SELECT issuance_request_hash
            FROM device_access_tokens
            WHERE public_id = $public_id;
            """;
        select.Parameters.AddWithValue("$public_id", created.PublicId!);

        Assert.Equal("existing", replay.Status);
        Assert.Equal(created.PublicId, replay.PublicId);
        Assert.Equal(64, Convert.ToString(await select.ExecuteScalarAsync())!.Length);
    }

    [Fact]
    public async Task MigrationEighteenGrandfathersExistingDeviceTokensWithoutChangingCredential()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Legacy phone", "android"),
            CreateRequestContext(),
            linkFactory,
            DateTimeOffset.UtcNow,
            CancellationToken.None,
            "device-issuance:migration-eighteen-source");
        string originalHash;

        await using (SqliteConnection connection = new($"Data Source={fixture.DatabasePath}"))
        {
            await connection.OpenAsync();
            await using (SqliteCommand select = connection.CreateCommand())
            {
                select.CommandText = "SELECT secret_hash FROM device_access_tokens WHERE public_id = $public_id;";
                select.Parameters.AddWithValue("$public_id", created.PublicId!);
                originalHash = Convert.ToString(await select.ExecuteScalarAsync())!;
            }

            await using SqliteCommand downgrade = connection.CreateCommand();
            downgrade.CommandText = """
                ALTER TABLE device_access_tokens DROP COLUMN issuance_request_hash;
                DELETE FROM mediator_migrations WHERE version = 19;
                DROP TABLE device_feed_transfer_operations;
                DROP TABLE device_feed_policy_events;
                DROP TABLE device_access_sightings;
                ALTER TABLE device_access_tokens DROP COLUMN feed_policy_version;
                ALTER TABLE device_access_tokens DROP COLUMN feed_policy_mode;
                ALTER TABLE device_access_tokens DROP COLUMN binding_state;
                ALTER TABLE device_access_tokens DROP COLUMN bound_platform;
                ALTER TABLE device_access_tokens DROP COLUMN bound_client_family;
                ALTER TABLE device_access_tokens DROP COLUMN bound_at_utc;
                ALTER TABLE device_access_tokens DROP COLUMN last_network_fingerprint;
                ALTER TABLE device_access_tokens DROP COLUMN last_network_changed_at_utc;
                ALTER TABLE device_access_tokens DROP COLUMN last_policy_event_at_utc;
                ALTER TABLE device_access_tokens DROP COLUMN last_transfer_at_utc;
                ALTER TABLE device_access_tokens DROP COLUMN transfer_count;
                ALTER TABLE device_access_tokens DROP COLUMN risk_score;
                DELETE FROM mediator_migrations WHERE version = 18;
                """;
            await downgrade.ExecuteNonQueryAsync();
        }

        SqliteMediatorRepository migratedRepository = fixture.CreateRepository();
        await migratedRepository.InitializeAsync(CancellationToken.None);
        IReadOnlyList<DeviceTokenListItem> devices = await migratedRepository.ListDeviceTokensAsync(
            publicGuid,
            CancellationToken.None);

        await using SqliteConnection verification = new($"Data Source={fixture.DatabasePath}");
        await verification.OpenAsync();
        await using SqliteCommand selectHash = verification.CreateCommand();
        selectHash.CommandText = "SELECT secret_hash FROM device_access_tokens WHERE public_id = $public_id;";
        selectHash.Parameters.AddWithValue("$public_id", created.PublicId!);

        Assert.Equal(originalHash, Convert.ToString(await selectHash.ExecuteScalarAsync()));
        DeviceTokenListItem device = Assert.Single(devices);
        Assert.Equal(DeviceFeedPolicyModes.Legacy, device.FeedPolicyMode);
        Assert.Equal(DeviceFeedBindingStates.Grandfathered, device.BindingState);
        Assert.Equal(created.PublicId, device.PublicId);
    }

    [Fact]
    public async Task LegacyDeviceTokenRemainsUsableAcrossPlatformsWhenEnforcementIsEnabled()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        fixture.Options.DeviceFeedBindingMode = DeviceFeedBindingModes.Enforce;
        fixture.Options.DefaultNewDeviceFeedPolicy = DeviceFeedPolicyModes.Legacy;
        fixture.Options.DeviceObservationHashKey = "test-observation-key-with-at-least-32-characters";
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DateTimeOffset now = DateTimeOffset.UtcNow;
        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Legacy phone", "android"),
            CreateRequestContext(),
            linkFactory,
            now,
            CancellationToken.None);
        string token = ExtractTokenSecret(created.ConnectionUrl!);

        TokenSubscriptionAccessResult android = await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            created.PublicId!,
            token,
            now.AddSeconds(1),
            CancellationToken.None,
            "Happ/1.0 Android",
            IPAddress.Parse("203.0.113.10"));
        TokenSubscriptionAccessResult windows = await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            created.PublicId!,
            token,
            now.AddSeconds(2),
            CancellationToken.None,
            "Happ/1.0 Windows",
            IPAddress.Parse("198.51.100.20"));
        DeviceTokenListItem device = Assert.Single(
            await fixture.Repository.ListDeviceTokensAsync(publicGuid, CancellationToken.None));

        Assert.True(android.Allowed);
        Assert.True(windows.Allowed);
        Assert.Equal(DeviceFeedPolicyModes.Legacy, device.FeedPolicyMode);
        Assert.Equal(DeviceFeedBindingStates.Grandfathered, device.BindingState);
        Assert.Null(device.BoundPlatform);
    }

    [Fact]
    public async Task EnforcedDeviceTokenBindsPlatformAllowsNetworkChangesAndRequiresTransferForMismatch()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        fixture.Options.DeviceFeedBindingMode = DeviceFeedBindingModes.Enforce;
        fixture.Options.DefaultNewDeviceFeedPolicy = DeviceFeedPolicyModes.Enforce;
        fixture.Options.DeviceObservationHashKey = "test-observation-key-with-at-least-32-characters";
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DateTimeOffset now = DateTimeOffset.UtcNow;
        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Protected phone", "android"),
            CreateRequestContext(),
            linkFactory,
            now,
            CancellationToken.None);
        string token = ExtractTokenSecret(created.ConnectionUrl!);

        TokenSubscriptionAccessResult first = await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            created.PublicId!,
            token,
            now.AddSeconds(1),
            CancellationToken.None,
            "Happ/1.0 Android",
            IPAddress.Parse("203.0.113.10"));
        TokenSubscriptionAccessResult changedNetwork = await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            created.PublicId!,
            token,
            now.AddSeconds(2),
            CancellationToken.None,
            "Happ/1.0 Android",
            IPAddress.Parse("198.51.100.20"));
        TokenSubscriptionAccessResult mismatch = await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            created.PublicId!,
            token,
            now.AddSeconds(3),
            CancellationToken.None,
            "Happ/1.0 Windows",
            IPAddress.Parse("192.0.2.30"));
        DeviceTokenListItem device = Assert.Single(
            await fixture.Repository.ListDeviceTokensAsync(publicGuid, CancellationToken.None));

        Assert.True(first.Allowed);
        Assert.True(changedNetwork.Allowed);
        Assert.False(mismatch.Allowed);
        Assert.Equal(UserFacingStatus.DeviceTransferRequired, mismatch.Status);
        Assert.Equal("android", mismatch.ExpectedPlatform);
        Assert.Equal(DeviceFeedBindingStates.Bound, device.BindingState);
        Assert.Equal("android", device.BoundPlatform);

        await using SqliteConnection connection = new($"Data Source={fixture.DatabasePath}");
        await connection.OpenAsync();
        await using SqliteCommand command = connection.CreateCommand();
        command.CommandText = "SELECT network_fingerprint FROM device_access_sightings ORDER BY id;";
        await using SqliteDataReader reader = await command.ExecuteReaderAsync();
        List<string> fingerprints = [];
        while (await reader.ReadAsync())
        {
            fingerprints.Add(reader.GetString(0));
        }

        Assert.NotEmpty(fingerprints);
        Assert.All(fingerprints, fingerprint => Assert.Equal(64, fingerprint.Length));
        Assert.DoesNotContain(fingerprints, fingerprint => fingerprint.Contains("203.0.113", StringComparison.Ordinal));
        Assert.DoesNotContain(fingerprints, fingerprint => fingerprint.Contains("198.51.100", StringComparison.Ordinal));
    }

    [Fact]
    public async Task ObserveModeRecordsMismatchWithoutDenyingCatalogAccess()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        fixture.Options.DeviceFeedBindingMode = DeviceFeedBindingModes.Observe;
        fixture.Options.DefaultNewDeviceFeedPolicy = DeviceFeedPolicyModes.Enforce;
        fixture.Options.DeviceObservationHashKey = "test-observation-key-with-at-least-32-characters";
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DateTimeOffset now = DateTimeOffset.UtcNow;
        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Observed phone", "android"),
            CreateRequestContext(),
            linkFactory,
            now,
            CancellationToken.None);
        string token = ExtractTokenSecret(created.ConnectionUrl!);

        TokenSubscriptionAccessResult first = await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            created.PublicId!,
            token,
            now.AddSeconds(1),
            CancellationToken.None,
            "Happ/1.0 Android",
            IPAddress.Parse("203.0.113.10"));
        TokenSubscriptionAccessResult mismatch = await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            created.PublicId!,
            token,
            now.AddSeconds(2),
            CancellationToken.None,
            "Happ/1.0 Windows",
            IPAddress.Parse("198.51.100.20"));

        Assert.True(first.Allowed);
        Assert.True(mismatch.Allowed);

        await using SqliteConnection connection = new($"Data Source={fixture.DatabasePath}");
        await connection.OpenAsync();
        await using SqliteCommand command = connection.CreateCommand();
        command.CommandText = """
            SELECT COUNT(*)
            FROM device_feed_policy_events
            WHERE event_type = 'policy_mismatch_observed';
            """;
        Assert.Equal(1L, Convert.ToInt64(await command.ExecuteScalarAsync()));
    }

    [Fact]
    public async Task GlobalOffIsImmediateRollbackSwitchForAlreadyBoundProfile()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        fixture.Options.DeviceFeedBindingMode = DeviceFeedBindingModes.Enforce;
        fixture.Options.DefaultNewDeviceFeedPolicy = DeviceFeedPolicyModes.Enforce;
        fixture.Options.DeviceObservationHashKey = "test-observation-key-with-at-least-32-characters";
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DateTimeOffset now = DateTimeOffset.UtcNow;
        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Protected phone", "android"),
            CreateRequestContext(),
            linkFactory,
            now,
            CancellationToken.None);
        string token = ExtractTokenSecret(created.ConnectionUrl!);
        _ = await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            created.PublicId!,
            token,
            now.AddSeconds(1),
            CancellationToken.None,
            "Happ/1.0 Android",
            IPAddress.Parse("203.0.113.10"));

        fixture.Options.DeviceFeedBindingMode = DeviceFeedBindingModes.Off;
        TokenSubscriptionAccessResult rollbackAccess =
            await fixture.Repository.ValidateDeviceTokenAccessAsync(
                publicGuid,
                created.PublicId!,
                token,
                now.AddSeconds(2),
                CancellationToken.None,
                "Happ/1.0 Windows",
                IPAddress.Parse("198.51.100.20"));

        Assert.True(rollbackAccess.Allowed);
    }

    [Fact]
    public async Task DeviceTransferIsAtomicIdempotentAndCreatesEnforcedReplacement()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        fixture.Options.DeviceFeedBindingMode = DeviceFeedBindingModes.Enforce;
        fixture.Options.DefaultNewDeviceFeedPolicy = DeviceFeedPolicyModes.Legacy;
        fixture.Options.DeviceObservationHashKey = "test-observation-key-with-at-least-32-characters";
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DefaultHttpContext context = CreateRequestContext();
        DateTimeOffset now = DateTimeOffset.UtcNow;
        DeviceTokenCreateResult source = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Legacy phone", "android"),
            context,
            linkFactory,
            now,
            CancellationToken.None,
            "device-issuance:transfer-source-request");
        string sourceSecret = ExtractTokenSecret(source.ConnectionUrl!);
        TokenSubscriptionAccessResult initialAccess =
            await fixture.Repository.ValidateDeviceTokenAccessAsync(
                publicGuid,
                source.PublicId!,
                sourceSecret,
                now.AddSeconds(1),
                CancellationToken.None,
                "Happ/1.0 Android",
                IPAddress.Parse("203.0.113.10"));
        Assert.True(initialAccess.Allowed);
        TransferDeviceTokenRequest request = new(
            $"device-transfer:{source.PublicId}:windows",
            "windows");

        DeviceTokenCreateResult transferred = await fixture.Repository.TransferDeviceTokenAsync(
            publicGuid,
            source.PublicId!,
            request,
            context,
            linkFactory,
            now.AddSeconds(2),
            CancellationToken.None);
        DeviceTokenCreateResult replay = await fixture.Repository.TransferDeviceTokenAsync(
            publicGuid,
            source.PublicId!,
            request,
            context,
            linkFactory,
            now.AddSeconds(3),
            CancellationToken.None);
        DeviceTokenCreateResult issuanceReplay = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Legacy phone", "android"),
            context,
            linkFactory,
            now.AddSeconds(4),
            CancellationToken.None,
            "device-issuance:transfer-source-request");
        TokenSubscriptionAccessResult oldAccess =
            await fixture.Repository.ValidateDeviceTokenAccessAsync(
                publicGuid,
                source.PublicId!,
                sourceSecret,
                now.AddSeconds(5),
                CancellationToken.None,
                "Happ/1.0 Android",
                IPAddress.Parse("203.0.113.10"));
        IReadOnlyList<DeviceTokenListItem> devices = await fixture.Repository.ListDeviceTokensAsync(
            publicGuid,
            CancellationToken.None);

        Assert.Equal("created", transferred.Status);
        Assert.Equal("existing", replay.Status);
        Assert.Equal(transferred.PublicId, replay.PublicId);
        Assert.Equal(transferred.ConnectionUrl, replay.ConnectionUrl);
        Assert.Equal("existing", issuanceReplay.Status);
        Assert.Equal(transferred.PublicId, issuanceReplay.PublicId);
        Assert.False(oldAccess.Allowed);
        Assert.Equal(UserFacingStatus.DeviceTokenRevoked, oldAccess.Status);
        DeviceTokenListItem replacement = Assert.Single(
            devices,
            item => item.PublicId == transferred.PublicId);
        Assert.Equal(DeviceFeedPolicyModes.Enforce, replacement.FeedPolicyMode);
        Assert.Equal(DeviceFeedBindingStates.Unbound, replacement.BindingState);
        Assert.Equal(1, replacement.TransferCount);
        Assert.NotNull(replacement.LastTransferAtUtc);
        Assert.Single(devices, item => item.PublicId == source.PublicId && item.State == DeviceTokenStates.Revoked);
    }

    [Fact]
    public async Task RepeatedSamePlatformNetworkOverlapRaisesReviewWithoutBlockingAccess()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        fixture.Options.DeviceFeedBindingMode = DeviceFeedBindingModes.Enforce;
        fixture.Options.DefaultNewDeviceFeedPolicy = DeviceFeedPolicyModes.Enforce;
        fixture.Options.DeviceObservationHashKey = "test-observation-key-with-at-least-32-characters";
        fixture.Options.DeviceFeedConcurrentNetworkWindowMinutes = 10;
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DateTimeOffset now = DateTimeOffset.UtcNow;
        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Protected Android", "android"),
            CreateRequestContext(),
            linkFactory,
            now,
            CancellationToken.None);
        string token = ExtractTokenSecret(created.ConnectionUrl!);

        for (int cycle = 0; cycle < 3; cycle++)
        {
            DateTimeOffset cycleStart = now.AddMinutes(cycle * 11).AddSeconds(1);
            TokenSubscriptionAccessResult first =
                await fixture.Repository.ValidateDeviceTokenAccessAsync(
                    publicGuid,
                    created.PublicId!,
                    token,
                    cycleStart,
                    CancellationToken.None,
                    "Happ/1.0 Android",
                    IPAddress.Parse("203.0.113.10"));
            TokenSubscriptionAccessResult second =
                await fixture.Repository.ValidateDeviceTokenAccessAsync(
                    publicGuid,
                    created.PublicId!,
                    token,
                    cycleStart.AddSeconds(1),
                    CancellationToken.None,
                    "Happ/1.0 Android",
                    IPAddress.Parse("198.51.100.20"));
            Assert.True(first.Allowed);
            Assert.True(second.Allowed);
        }

        TokenSubscriptionAccessResult subsequent =
            await fixture.Repository.ValidateDeviceTokenAccessAsync(
                publicGuid,
                created.PublicId!,
                token,
                now.AddMinutes(34),
                CancellationToken.None,
                "Happ/1.0 Android",
                IPAddress.Parse("203.0.113.10"));
        DeviceTokenListItem device = Assert.Single(
            await fixture.Repository.ListDeviceTokensAsync(publicGuid, CancellationToken.None));

        Assert.True(subsequent.Allowed);
        Assert.Equal(DeviceFeedBindingStates.Review, device.BindingState);
        Assert.Equal(3, device.RiskScore);
        Assert.Equal("android", device.BoundPlatform);
    }

    [Fact]
    public async Task HwidPolicyBindsIdentityAndRejectsDifferentDeviceWithoutRelyingOnUserAgent()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        fixture.Options.DeviceFeedBindingMode = DeviceFeedBindingModes.Enforce;
        fixture.Options.DefaultNewDeviceFeedPolicy = DeviceFeedPolicyModes.Enforce;
        fixture.Options.DefaultNewDeviceFeedPolicyVersion = DeviceFeedPolicyVersions.HwidIdentity;
        fixture.Options.DeviceObservationHashKey = "test-observation-key-with-at-least-32-characters";
        fixture.Options.DeviceIdentityHashKeyId = "identity-v1";
        fixture.Options.DeviceIdentityHashKey = "test-device-identity-key-with-at-least-32-characters";
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DateTimeOffset now = DateTimeOffset.UtcNow;
        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Protected Android", "android"),
            CreateRequestContext(),
            linkFactory,
            now,
            CancellationToken.None,
            "device-issuance:hwid-binding-test");
        string token = ExtractTokenSecret(created.ConnectionUrl!);
        const string firstHwid = "4139def9-6877-4771-b313-49e3119ba158";

        TokenSubscriptionAccessResult first = await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            created.PublicId!,
            token,
            now.AddSeconds(1),
            CancellationToken.None,
            null,
            IPAddress.Parse("203.0.113.10"),
            firstHwid,
            "Android",
            "14",
            "Pixel 8");
        TokenSubscriptionAccessResult sameIdentity = await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            created.PublicId!,
            token,
            now.AddSeconds(2),
            CancellationToken.None,
            "unrecognized-client",
            IPAddress.Parse("198.51.100.20"),
            firstHwid);
        TokenSubscriptionAccessResult differentIdentity = await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            created.PublicId!,
            token,
            now.AddSeconds(3),
            CancellationToken.None,
            null,
            IPAddress.Parse("192.0.2.30"),
            "6c322d91-a235-42cb-a78d-498e1aa40e51",
            "Linux");
        DeviceTokenListItem device = Assert.Single(
            await fixture.Repository.ListDeviceTokensAsync(publicGuid, CancellationToken.None));

        Assert.True(first.Allowed);
        Assert.True(sameIdentity.Allowed);
        Assert.False(differentIdentity.Allowed);
        Assert.Equal(UserFacingStatus.DeviceTransferRequired, differentIdentity.Status);
        Assert.Equal("identity_mismatch", differentIdentity.PolicyReasonCode);
        Assert.Equal(DeviceFeedPolicyVersions.HwidIdentity, device.FeedPolicyVersion);
        Assert.True(device.IdentityBound);
        Assert.Equal(DeviceIdentitySources.HappHwid, device.IdentitySource);
        Assert.NotNull(device.LastIdentitySeenAtUtc);
        Assert.NotNull(device.LastIdentityMismatchAtUtc);

        await using SqliteConnection connection = new($"Data Source={fixture.DatabasePath}");
        await connection.OpenAsync();
        await using SqliteCommand command = connection.CreateCommand();
        command.CommandText = """
            SELECT bound_identity_hash,
                   (SELECT GROUP_CONCAT(COALESCE(reason_code, ''), '|') FROM device_feed_policy_events)
            FROM device_access_tokens
            WHERE public_id = $public_id;
            """;
        command.Parameters.AddWithValue("$public_id", created.PublicId!);
        await using SqliteDataReader reader = await command.ExecuteReaderAsync();
        Assert.True(await reader.ReadAsync());
        string storedHash = reader.GetString(0);
        string events = reader.IsDBNull(1) ? string.Empty : reader.GetString(1);
        Assert.Equal(64, storedHash.Length);
        Assert.DoesNotContain(firstHwid, storedHash, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain(firstHwid, events, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task HwidPolicyRequiresIdentityBeforeActivatingPendingToken()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        fixture.Options.DeviceFeedBindingMode = DeviceFeedBindingModes.Enforce;
        fixture.Options.DefaultNewDeviceFeedPolicy = DeviceFeedPolicyModes.Enforce;
        fixture.Options.DefaultNewDeviceFeedPolicyVersion = DeviceFeedPolicyVersions.HwidIdentity;
        fixture.Options.DeviceObservationHashKey = "test-observation-key-with-at-least-32-characters";
        fixture.Options.DeviceIdentityHashKey = "test-device-identity-key-with-at-least-32-characters";
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Protected Android", "android"),
            CreateRequestContext(),
            linkFactory,
            DateTimeOffset.UtcNow,
            CancellationToken.None,
            "device-issuance:hwid-required-test");

        TokenSubscriptionAccessResult result = await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            created.PublicId!,
            ExtractTokenSecret(created.ConnectionUrl!),
            DateTimeOffset.UtcNow.AddSeconds(1),
            CancellationToken.None,
            string.Empty,
            IPAddress.Loopback);
        DeviceTokenListItem device = Assert.Single(
            await fixture.Repository.ListDeviceTokensAsync(publicGuid, CancellationToken.None));

        Assert.False(result.Allowed);
        Assert.Equal(UserFacingStatus.DeviceIdentityRequired, result.Status);
        Assert.Equal("identity_missing", result.PolicyReasonCode);
        Assert.Null(device.ActivatedAtUtc);
        Assert.False(device.IdentityBound);
    }

    [Fact]
    public async Task HwidObserveModeAllowsMissingIdentityAndRecordsObservation()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        fixture.Options.DeviceFeedBindingMode = DeviceFeedBindingModes.Observe;
        fixture.Options.DefaultNewDeviceFeedPolicy = DeviceFeedPolicyModes.Enforce;
        fixture.Options.DefaultNewDeviceFeedPolicyVersion = DeviceFeedPolicyVersions.HwidIdentity;
        fixture.Options.DeviceObservationHashKey = "test-observation-key-with-at-least-32-characters";
        fixture.Options.DeviceIdentityHashKey = "test-device-identity-key-with-at-least-32-characters";
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Observed Android", "android"),
            CreateRequestContext(),
            linkFactory,
            DateTimeOffset.UtcNow,
            CancellationToken.None,
            "device-issuance:hwid-observe-test");

        TokenSubscriptionAccessResult result = await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            created.PublicId!,
            ExtractTokenSecret(created.ConnectionUrl!),
            DateTimeOffset.UtcNow.AddSeconds(1),
            CancellationToken.None,
            null,
            IPAddress.Parse("203.0.113.10"));

        Assert.True(result.Allowed);
        await using SqliteConnection connection = new($"Data Source={fixture.DatabasePath}");
        await connection.OpenAsync();
        await using SqliteCommand command = connection.CreateCommand();
        command.CommandText = """
            SELECT COUNT(*)
            FROM device_feed_policy_events
            WHERE reason_code = 'identity_missing'
              AND identity_present = 0;
            """;
        Assert.Equal(1L, Convert.ToInt64(await command.ExecuteScalarAsync()));
    }

    [Fact]
    public async Task PreviousDeviceIdentityKeyIsAcceptedAndRehashedWithCurrentKey()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        fixture.Options.DeviceFeedBindingMode = DeviceFeedBindingModes.Enforce;
        fixture.Options.DefaultNewDeviceFeedPolicy = DeviceFeedPolicyModes.Enforce;
        fixture.Options.DefaultNewDeviceFeedPolicyVersion = DeviceFeedPolicyVersions.HwidIdentity;
        fixture.Options.DeviceObservationHashKey = "test-observation-key-with-at-least-32-characters";
        fixture.Options.DeviceIdentityHashKeyId = "identity-v1";
        fixture.Options.DeviceIdentityHashKey = "old-device-identity-key-with-at-least-32-characters";
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Protected Android", "android"),
            CreateRequestContext(),
            linkFactory,
            DateTimeOffset.UtcNow,
            CancellationToken.None,
            "device-issuance:hwid-rotation-test");
        string token = ExtractTokenSecret(created.ConnectionUrl!);
        const string hwid = "4139def9-6877-4771-b313-49e3119ba158";
        Assert.True((await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            created.PublicId!,
            token,
            DateTimeOffset.UtcNow.AddSeconds(1),
            CancellationToken.None,
            "Happ Android",
            IPAddress.Loopback,
            hwid,
            "Android")).Allowed);

        fixture.Options.PreviousDeviceIdentityHashKeyId = fixture.Options.DeviceIdentityHashKeyId;
        fixture.Options.PreviousDeviceIdentityHashKey = fixture.Options.DeviceIdentityHashKey;
        fixture.Options.DeviceIdentityHashKeyId = "identity-v2";
        fixture.Options.DeviceIdentityHashKey = "new-device-identity-key-with-at-least-32-characters";
        Assert.True((await fixture.Repository.ValidateDeviceTokenAccessAsync(
            publicGuid,
            created.PublicId!,
            token,
            DateTimeOffset.UtcNow.AddSeconds(2),
            CancellationToken.None,
            null,
            IPAddress.Loopback,
            hwid)).Allowed);

        await using SqliteConnection connection = new($"Data Source={fixture.DatabasePath}");
        await connection.OpenAsync();
        await using SqliteCommand command = connection.CreateCommand();
        command.CommandText = "SELECT bound_identity_key_id FROM device_access_tokens WHERE public_id = $public_id;";
        command.Parameters.AddWithValue("$public_id", created.PublicId!);
        Assert.Equal("identity-v2", Convert.ToString(await command.ExecuteScalarAsync()));
    }

    [Fact]
    public async Task SameHwidAcrossMultipleNetworksDoesNotIncreaseRiskScore()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        fixture.Options.DeviceFeedBindingMode = DeviceFeedBindingModes.Enforce;
        fixture.Options.DefaultNewDeviceFeedPolicy = DeviceFeedPolicyModes.Enforce;
        fixture.Options.DefaultNewDeviceFeedPolicyVersion = DeviceFeedPolicyVersions.HwidIdentity;
        fixture.Options.DeviceObservationHashKey = "test-observation-key-with-at-least-32-characters";
        fixture.Options.DeviceIdentityHashKey = "test-device-identity-key-with-at-least-32-characters";
        fixture.Options.DeviceFeedConcurrentNetworkWindowMinutes = 10;
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DateTimeOffset now = DateTimeOffset.UtcNow;
        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Protected Android", "android"),
            CreateRequestContext(),
            linkFactory,
            now,
            CancellationToken.None,
            "device-issuance:hwid-network-risk-test");
        string token = ExtractTokenSecret(created.ConnectionUrl!);
        const string hwid = "4139def9-6877-4771-b313-49e3119ba158";

        for (int cycle = 0; cycle < 3; cycle++)
        {
            DateTimeOffset cycleStart = now.AddMinutes(cycle * 11).AddSeconds(1);
            Assert.True((await fixture.Repository.ValidateDeviceTokenAccessAsync(
                publicGuid, created.PublicId!, token, cycleStart, CancellationToken.None,
                null, IPAddress.Parse("203.0.113.10"), hwid, "Android")).Allowed);
            Assert.True((await fixture.Repository.ValidateDeviceTokenAccessAsync(
                publicGuid, created.PublicId!, token, cycleStart.AddSeconds(1), CancellationToken.None,
                null, IPAddress.Parse("198.51.100.20"), hwid, "Android")).Allowed);
        }

        DeviceTokenListItem device = Assert.Single(
            await fixture.Repository.ListDeviceTokensAsync(publicGuid, CancellationToken.None));
        Assert.Equal(0, device.RiskScore);
        Assert.Equal(DeviceFeedBindingStates.Bound, device.BindingState);
    }

    [Fact]
    public async Task ConcurrentFirstRequestsWithDifferentHwidsBindExactlyOneIdentity()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        fixture.Options.DeviceFeedBindingMode = DeviceFeedBindingModes.Enforce;
        fixture.Options.DefaultNewDeviceFeedPolicy = DeviceFeedPolicyModes.Enforce;
        fixture.Options.DefaultNewDeviceFeedPolicyVersion = DeviceFeedPolicyVersions.HwidIdentity;
        fixture.Options.DeviceObservationHashKey = "test-observation-key-with-at-least-32-characters";
        fixture.Options.DeviceIdentityHashKey = "test-device-identity-key-with-at-least-32-characters";
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        DeviceSubscriptionLinkFactory linkFactory = new(Options.Create(fixture.Options));
        DeviceTokenCreateResult created = await fixture.Repository.CreateDeviceTokenAsync(
            publicGuid,
            new CreateDeviceTokenRequest("Protected Android", "android"),
            CreateRequestContext(),
            linkFactory,
            DateTimeOffset.UtcNow,
            CancellationToken.None,
            "device-issuance:hwid-concurrency-test");
        string token = ExtractTokenSecret(created.ConnectionUrl!);

        Task<TokenSubscriptionAccessResult>[] requests =
        [
            fixture.Repository.ValidateDeviceTokenAccessAsync(
                publicGuid, created.PublicId!, token, DateTimeOffset.UtcNow.AddSeconds(1),
                CancellationToken.None, null, IPAddress.Loopback,
                "4139def9-6877-4771-b313-49e3119ba158", "Android"),
            fixture.Repository.ValidateDeviceTokenAccessAsync(
                publicGuid, created.PublicId!, token, DateTimeOffset.UtcNow.AddSeconds(1),
                CancellationToken.None, null, IPAddress.Loopback,
                "6c322d91-a235-42cb-a78d-498e1aa40e51", "Android")
        ];
        TokenSubscriptionAccessResult[] results = await Task.WhenAll(requests);

        Assert.Single(results, result => result.Allowed);
        Assert.Single(results, result => !result.Allowed);
    }

    [Fact]
    public async Task MigrationTwentyOneRemainsSupportedAfterUnifiedFeedRollback()
    {
        await using TestRepositoryFixture fixture =
            await TestRepositoryFixture.CreateAsync();

        await using SqliteConnection connection =
            new($"Data Source={fixture.DatabasePath}");
        await connection.OpenAsync();

        await using SqliteCommand migrationCommand = connection.CreateCommand();
        migrationCommand.CommandText =
            "SELECT COUNT(*) FROM mediator_migrations "
            + "WHERE version = 21 "
            + "AND name = 'unified_hwid_subscription_feed';";

        Assert.Equal(
            1L,
            Convert.ToInt64(await migrationCommand.ExecuteScalarAsync()));

        await using SqliteCommand tableCommand = connection.CreateCommand();
        tableCommand.CommandText =
            "SELECT COUNT(*) FROM sqlite_master "
            + "WHERE type = 'table' "
            + "AND name = 'device_enrollment_intents';";

        Assert.Equal(
            1L,
            Convert.ToInt64(await tableCommand.ExecuteScalarAsync()));

        SqliteMediatorRepository restartedRepository =
            fixture.CreateRepository();

        await restartedRepository.InitializeAsync(CancellationToken.None);
    }


    [Fact]
    public async Task MigrationStateAllowsHistoricalOptionalVersionTwoButRejectsFutureSchema()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        await using (SqliteConnection connection = new($"Data Source={fixture.DatabasePath}"))
        {
            await connection.OpenAsync();
            await using SqliteCommand removeOptional = connection.CreateCommand();
            removeOptional.CommandText = "DELETE FROM mediator_migrations WHERE version = 2;";
            await removeOptional.ExecuteNonQueryAsync();
        }

        MigrationState withoutOptional = await fixture.Repository.GetMigrationStateAsync(
            CancellationToken.None);
        Assert.True(withoutOptional.IsCurrent);
        Assert.Empty(withoutOptional.MissingRequiredVersions);

        await using (SqliteConnection connection = new($"Data Source={fixture.DatabasePath}"))
        {
            await connection.OpenAsync();
            await using SqliteCommand addFuture = connection.CreateCommand();
            addFuture.CommandText = "INSERT INTO mediator_migrations(version, name, applied_at_utc) VALUES(999, 'future', '2030-01-01T00:00:00Z');";
            await addFuture.ExecuteNonQueryAsync();
        }

        SqliteMediatorRepository futureRepository = fixture.CreateRepository();
        InvalidOperationException exception = await Assert.ThrowsAsync<InvalidOperationException>(
            () => futureRepository.InitializeAsync(CancellationToken.None));
        Assert.Contains("newer than supported", exception.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void FeedEnforcedPostureRejectsContradictoryConfiguration()
    {
        VpnMediatorOptions options = CreateValidProductionOptions();
        options.RequiredDeviceSecurityPosture = DeviceSecurityPostures.FeedEnforced;
        options.DeviceFeedBindingMode = DeviceFeedBindingModes.Enforce;
        options.DefaultNewDeviceFeedPolicy = DeviceFeedPolicyModes.Legacy;
        options.DefaultNewDeviceFeedPolicyVersion = DeviceFeedPolicyVersions.PlatformHeuristic;
        options.RequireDeviceIssuanceKey = false;
        VpnMediatorOptionsValidator validator = new(new TestEnvironment(Environments.Production));

        ValidateOptionsResult result = validator.Validate(null, options);

        Assert.True(result.Failed);
        Assert.Contains(result.Failures, failure => failure.Contains("DefaultNewDeviceFeedPolicy=enforce", StringComparison.Ordinal));
        Assert.Contains(result.Failures, failure => failure.Contains("DefaultNewDeviceFeedPolicyVersion=2", StringComparison.Ordinal));
        Assert.Contains(result.Failures, failure => failure.Contains("RequireDeviceIssuanceKey=true", StringComparison.Ordinal));
    }

    private sealed class BlockingSourceReaderRegistry : IUpstreamSourceReaderRegistry
    {
        private readonly IUpstreamSourceReader _reader;

        public BlockingSourceReaderRegistry(IUpstreamSourceReader reader)
        {
            _reader = reader;
        }

        public bool TryGet(string kind, out IUpstreamSourceReader? reader)
        {
            reader = string.Equals(kind, _reader.Kind, StringComparison.Ordinal)
                ? _reader
                : null;
            return reader is not null;
        }
    }

    private sealed class BlockingSourceReader : IUpstreamSourceReader
    {
        private readonly IReadOnlyList<string> _links;

        public BlockingSourceReader(IReadOnlyList<string> links)
        {
            _links = links;
        }

        public string Kind => "subscription_url";

        public TaskCompletionSource Entered { get; } = new(
            TaskCreationOptions.RunContinuationsAsynchronously);

        public TaskCompletionSource Release { get; } = new(
            TaskCreationOptions.RunContinuationsAsynchronously);

        public async Task<SourceReadResult> ReadAsync(
            UpstreamSource source,
            CancellationToken cancellationToken)
        {
            _ = source;
            Entered.TrySetResult();
            await Release.Task.WaitAsync(cancellationToken);
            return SourceReadResult.Successful(_links, 1, ["vless", "trojan"], 0, []);
        }
    }

    private sealed class ImmediateSourceReader : IUpstreamSourceReader
    {
        private readonly IReadOnlyList<string> _links;

        public ImmediateSourceReader(IReadOnlyList<string> links)
        {
            _links = links;
        }

        public string Kind => SourceKinds.SubscriptionUrl;

        public Task<SourceReadResult> ReadAsync(
            UpstreamSource source,
            CancellationToken cancellationToken)
        {
            _ = source;
            cancellationToken.ThrowIfCancellationRequested();
            return Task.FromResult(
                SourceReadResult.Successful(_links, 1, ["vless"], 0, []));
        }
    }


    private static DefaultHttpContext SubscriptionContext(
        string publicGuid,
        string devicePublicId,
        string token,
        string remoteAddress)
    {
        DefaultHttpContext context = new();
        context.Connection.RemoteIpAddress = IPAddress.Parse(remoteAddress);
        context.Request.RouteValues["publicGuid"] = publicGuid;
        context.Request.RouteValues["devicePublicId"] = devicePublicId;
        context.Request.QueryString = QueryString.Create("token", token);
        return context;
    }
    [Fact]
    public async Task OneSharedFeedRegistersThreeDistinctDeviceIdentities()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        ConfigureUnifiedFeed(fixture.Options);
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 3);
        string token = await CreateUnifiedFeedTokenAsync(fixture, publicGuid);
        DateTimeOffset now = DateTimeOffset.UtcNow;

        UnifiedFeedDeviceResolution[] resolutions = await Task.WhenAll(
            new[] { "device-a", "device-b", "device-c" }
                .Select((hwid, index) => fixture.Repository.ResolveUnifiedFeedDeviceAsync(
                    publicGuid,
                    token,
                    CreateUnifiedContext(fixture.Options, hwid),
                    now.AddSeconds(index),
                    CancellationToken.None)));
        IReadOnlyList<DeviceTokenListItem> devices =
            await fixture.Repository.ListDeviceTokensAsync(publicGuid, CancellationToken.None);

        Assert.All(resolutions, resolution =>
        {
            Assert.True(resolution.Access.Allowed);
            Assert.True(resolution.Created);
            Assert.NotNull(resolution.DevicePublicId);
        });
        Assert.Equal(3, resolutions.Select(item => item.DevicePublicId).Distinct().Count());
        Assert.Equal(3, devices.Count);
        Assert.All(devices, device => Assert.Equal("unified_feed", device.AccessChannel));
    }

    [Fact]
    public async Task UnifiedFeedCredentialIsStableAndSharedAcrossDevices()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        ConfigureUnifiedFeed(fixture.Options);
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 2);
        DefaultHttpContext httpContext = new();
        httpContext.Request.Scheme = "https";
        httpContext.Request.Host = new HostString("vpn.example");
        SubscriptionFeedLinkFactory linkFactory = new(
            Microsoft.Extensions.Options.Options.Create(fixture.Options));

        SubscriptionFeedCredentialResult first =
            await fixture.Repository.EnsureSubscriptionFeedCredentialAsync(
                publicGuid,
                httpContext,
                linkFactory,
                DateTimeOffset.UtcNow,
                CancellationToken.None);
        SubscriptionFeedCredentialResult replay =
            await fixture.Repository.EnsureSubscriptionFeedCredentialAsync(
                publicGuid,
                httpContext,
                linkFactory,
                DateTimeOffset.UtcNow.AddMinutes(1),
                CancellationToken.None);

        Assert.Equal(SubscriptionFeedCredentialStatuses.Created, first.Status);
        Assert.Equal(SubscriptionFeedCredentialStatuses.Existing, replay.Status);
        Assert.NotNull(first.ConnectionUrl);
        Assert.Equal(first.ConnectionUrl, replay.ConnectionUrl);
        Assert.Contains($"/sub/{publicGuid:D}/feed?token=", first.ConnectionUrl);
    }

    [Fact]
    public async Task UnifiedFeedRequiresHwidAndEnforcesDeviceLimitIdempotently()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        ConfigureUnifiedFeed(fixture.Options);
        DateTimeOffset now = DateTimeOffset.UtcNow;
        DateTimeOffset validUntilUtc = now.AddDays(45);
        Guid publicGuid = await fixture.CreateSubscriptionAsync(
            maxDevices: 1,
            validUntilUtc);
        string token = await CreateUnifiedFeedTokenAsync(fixture, publicGuid);

        UnifiedFeedDeviceResolution missingIdentity =
            await fixture.Repository.ResolveUnifiedFeedDeviceAsync(
                publicGuid,
                token,
                CreateUnifiedContext(fixture.Options, null),
                now,
                CancellationToken.None);
        UnifiedFeedDeviceResolution first =
            await fixture.Repository.ResolveUnifiedFeedDeviceAsync(
                publicGuid,
                token,
                CreateUnifiedContext(fixture.Options, "device-a"),
                now,
                CancellationToken.None);
        UnifiedFeedDeviceResolution replay =
            await fixture.Repository.ResolveUnifiedFeedDeviceAsync(
                publicGuid,
                token,
                CreateUnifiedContext(fixture.Options, "device-a"),
                now.AddSeconds(1),
                CancellationToken.None);
        UnifiedFeedDeviceResolution second =
            await fixture.Repository.ResolveUnifiedFeedDeviceAsync(
                publicGuid,
                token,
                CreateUnifiedContext(fixture.Options, "device-b"),
                now.AddSeconds(2),
                CancellationToken.None);

        Assert.False(missingIdentity.Access.Allowed);
        Assert.Equal(UserFacingStatus.DeviceIdentityRequired, missingIdentity.Access.Status);
        Assert.True(first.Access.Allowed);
        Assert.True(first.Created);
        Assert.Equal(
            validUntilUtc.ToUnixTimeMilliseconds(),
            first.Access.ValidUntilUtc?.ToUnixTimeMilliseconds());
        Assert.Equal(first.DevicePublicId, replay.DevicePublicId);
        Assert.False(replay.Created);
        Assert.False(second.Access.Allowed);
        Assert.Equal(UserFacingStatus.DeviceLimitReached, second.Access.Status);
        Assert.Single(await fixture.Repository.ListDeviceTokensAsync(
            publicGuid,
            CancellationToken.None));
    }

    [Fact]
    public async Task DisabledUnifiedDeviceCannotRegisterAgainAndCanBeEnabledExplicitly()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        ConfigureUnifiedFeed(fixture.Options);
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        string token = await CreateUnifiedFeedTokenAsync(fixture, publicGuid);
        DateTimeOffset now = DateTimeOffset.UtcNow;
        DeviceAccessRequestContext context = CreateUnifiedContext(fixture.Options, "device-a");
        UnifiedFeedDeviceResolution first =
            await fixture.Repository.ResolveUnifiedFeedDeviceAsync(
                publicGuid,
                token,
                context,
                now,
                CancellationToken.None);
        Assert.NotNull(first.DevicePublicId);
        await fixture.Repository.MarkUnifiedDeviceActivatedAsync(
            publicGuid,
            first.DevicePublicId!,
            now.AddSeconds(1),
            CancellationToken.None);

        Assert.True(await fixture.Repository.RevokeDeviceTokenAsync(
            publicGuid,
            first.DevicePublicId!,
            now.AddSeconds(2),
            CancellationToken.None));
        UnifiedFeedDeviceResolution denied =
            await fixture.Repository.ResolveUnifiedFeedDeviceAsync(
                publicGuid,
                token,
                context,
                now.AddSeconds(3),
                CancellationToken.None);
        UnifiedDeviceEnableResult enabled =
            await fixture.Repository.EnableUnifiedDeviceAsync(
                publicGuid,
                first.DevicePublicId!,
                now.AddSeconds(4),
                CancellationToken.None);
        UnifiedFeedDeviceResolution restored =
            await fixture.Repository.ResolveUnifiedFeedDeviceAsync(
                publicGuid,
                token,
                context,
                now.AddSeconds(5),
                CancellationToken.None);

        Assert.False(denied.Access.Allowed);
        Assert.Equal(UserFacingStatus.DeviceTokenRevoked, denied.Access.Status);
        Assert.Equal("enabled", enabled.Status);
        Assert.True(restored.Access.Allowed);
        Assert.Equal(first.DevicePublicId, restored.DevicePublicId);
        Assert.False(restored.Created);
        Assert.Single(await fixture.Repository.ListDeviceTokensAsync(
            publicGuid,
            CancellationToken.None));
    }

    [Fact]
    public async Task ConcurrentUnifiedRegistrationsCannotExceedLimit()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        ConfigureUnifiedFeed(fixture.Options);
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        string token = await CreateUnifiedFeedTokenAsync(fixture, publicGuid);
        DateTimeOffset now = DateTimeOffset.UtcNow;

        Task<UnifiedFeedDeviceResolution> first = fixture.Repository.ResolveUnifiedFeedDeviceAsync(
            publicGuid,
            token,
            CreateUnifiedContext(fixture.Options, "device-a"),
            now,
            CancellationToken.None);
        Task<UnifiedFeedDeviceResolution> second = fixture.Repository.ResolveUnifiedFeedDeviceAsync(
            publicGuid,
            token,
            CreateUnifiedContext(fixture.Options, "device-b"),
            now,
            CancellationToken.None);
        UnifiedFeedDeviceResolution[] results = await Task.WhenAll(first, second);

        Assert.Single(results, result => result.Access.Allowed);
        Assert.Single(results, result => result.Access.Status == UserFacingStatus.DeviceLimitReached);
        Assert.Single(await fixture.Repository.ListDeviceTokensAsync(
            publicGuid,
            CancellationToken.None));
    }

    [Fact]
    public async Task MigrationTwentyTwoPreservesUnifiedIdentityAndRestoresDisabledSafeIndex()
    {
        await using TestRepositoryFixture fixture = await TestRepositoryFixture.CreateAsync();
        ConfigureUnifiedFeed(fixture.Options);
        Guid publicGuid = await fixture.CreateSubscriptionAsync(maxDevices: 1);
        string token = await CreateUnifiedFeedTokenAsync(fixture, publicGuid);
        DateTimeOffset now = DateTimeOffset.UtcNow;
        UnifiedFeedDeviceResolution created =
            await fixture.Repository.ResolveUnifiedFeedDeviceAsync(
                publicGuid,
                token,
                CreateUnifiedContext(fixture.Options, "migration-device"),
                now,
                CancellationToken.None);
        Assert.NotNull(created.DevicePublicId);
        await fixture.Repository.MarkUnifiedDeviceActivatedAsync(
            publicGuid,
            created.DevicePublicId!,
            now.AddSeconds(1),
            CancellationToken.None);

        string originalIdentityHash;
        await using (SqliteConnection connection = new($"Data Source={fixture.DatabasePath}"))
        {
            await connection.OpenAsync();
            await using (SqliteCommand select = connection.CreateCommand())
            {
                select.CommandText = """
                    SELECT bound_identity_hash
                    FROM device_access_tokens
                    WHERE public_id = $public_id;
                    """;
                select.Parameters.AddWithValue("$public_id", created.DevicePublicId!);
                originalIdentityHash = Convert.ToString(
                    await select.ExecuteScalarAsync())!;
            }

            await using SqliteCommand downgrade = connection.CreateCommand();
            downgrade.CommandText = """
                DROP INDEX ix_unified_device_state;
                DROP INDEX ux_unified_device_identity;
                CREATE UNIQUE INDEX ux_unified_device_identity
                ON device_access_tokens(subscription_id, bound_identity_hash)
                WHERE revoked_at_utc IS NULL
                  AND bound_identity_hash IS NOT NULL
                  AND access_channel = 'unified_feed';
                ALTER TABLE device_access_tokens DROP COLUMN provisioning_expires_at_utc;
                ALTER TABLE device_access_tokens DROP COLUMN device_state;
                DELETE FROM mediator_migrations WHERE version = 22;
                """;
            await downgrade.ExecuteNonQueryAsync();
        }

        SqliteMediatorRepository migratedRepository = fixture.CreateRepository();
        await migratedRepository.InitializeAsync(CancellationToken.None);

        await using SqliteConnection verification = new($"Data Source={fixture.DatabasePath}");
        await verification.OpenAsync();
        await using SqliteCommand rowQuery = verification.CreateCommand();
        rowQuery.CommandText = """
            SELECT bound_identity_hash, device_state
            FROM device_access_tokens
            WHERE public_id = $public_id;
            """;
        rowQuery.Parameters.AddWithValue("$public_id", created.DevicePublicId!);
        await using SqliteDataReader row = await rowQuery.ExecuteReaderAsync();
        Assert.True(await row.ReadAsync());
        Assert.Equal(originalIdentityHash, row.GetString(0));
        Assert.Equal(UnifiedDeviceStates.Active, row.GetString(1));
        await row.DisposeAsync();

        await using SqliteCommand indexQuery = verification.CreateCommand();
        indexQuery.CommandText = """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'index'
              AND name = 'ux_unified_device_identity';
            """;
        string indexSql = Convert.ToString(await indexQuery.ExecuteScalarAsync())!;
        Assert.DoesNotContain("revoked_at_utc IS NULL", indexSql, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("access_channel = 'unified_feed'", indexSql, StringComparison.OrdinalIgnoreCase);
    }

    private static void ConfigureUnifiedFeed(VpnMediatorOptions options)
    {
        options.DeviceIdentityHashKeyId = "identity-v1";
        options.DeviceIdentityHashKey =
            "test-device-identity-hash-key-with-at-least-thirty-two-characters";
        options.UnifiedDeviceReservationMinutes = 15;
    }

    private static async Task<string> CreateUnifiedFeedTokenAsync(
        TestRepositoryFixture fixture,
        Guid publicGuid)
    {
        DefaultHttpContext httpContext = new();
        httpContext.Request.Scheme = "https";
        httpContext.Request.Host = new HostString("vpn.example");
        SubscriptionFeedCredentialResult credential =
            await fixture.Repository.EnsureSubscriptionFeedCredentialAsync(
                publicGuid,
                httpContext,
                new SubscriptionFeedLinkFactory(
                    Microsoft.Extensions.Options.Options.Create(fixture.Options)),
                DateTimeOffset.UtcNow,
                CancellationToken.None);
        Assert.NotNull(credential.ConnectionUrl);
        Uri uri = new(credential.ConnectionUrl);
        string query = uri.Query.TrimStart('?');
        string encodedToken = Assert.Single(query.Split('&'))["token=".Length..];
        return Uri.UnescapeDataString(encodedToken);
    }

    private static DeviceAccessRequestContext CreateUnifiedContext(
        VpnMediatorOptions options,
        string? hwid)
    {
        return DeviceAccessRequestContextFactory.Create(
            "Happ/1.0 Android",
            IPAddress.Parse("203.0.113.10"),
            observationHashKey: null,
            hwid: hwid,
            deviceOs: "android",
            osVersion: "15",
            deviceModel: "Test phone",
            identityHashKeyId: options.DeviceIdentityHashKeyId,
            identityHashKey: options.DeviceIdentityHashKey);
    }

    private static VpnMediatorOptions CreateValidProductionOptions()
    {
        return new VpnMediatorOptions
        {
            PublicBaseUrl = "https://vpn.example",
            AdminToken = "valid-admin-token-with-more-than-thirty-two-characters",
            DeviceTokenHashKey = "valid-device-hash-key-with-more-than-thirty-two-characters",
            DeviceCredentialProtectionKey = Convert.ToBase64String(RandomNumberGenerator.GetBytes(32)),
            SourceEndpointProtectionKey = Convert.ToBase64String(RandomNumberGenerator.GetBytes(32)),
            AllowDevelopmentHttpSources = false
        };
    }

    private sealed class TestEnvironment : IWebHostEnvironment
    {
        public TestEnvironment(string environmentName)
        {
            EnvironmentName = environmentName;
        }

        public string EnvironmentName { get; set; }

        public string ApplicationName { get; set; } = "Tests";

        public string WebRootPath { get; set; } = Directory.GetCurrentDirectory();

        public IFileProvider WebRootFileProvider { get; set; } = new NullFileProvider();

        public string ContentRootPath { get; set; } = Directory.GetCurrentDirectory();

        public IFileProvider ContentRootFileProvider { get; set; } = new NullFileProvider();
    }

    private sealed class TestRepositoryFixture : IAsyncDisposable
    {
        private TestRepositoryFixture(
            string databasePath,
            VpnMediatorOptions options,
            SqliteMediatorRepository repository)
        {
            DatabasePath = databasePath;
            Options = options;
            Repository = repository;
        }

        public string DatabasePath { get; }

        public VpnMediatorOptions Options { get; }

        public SqliteMediatorRepository Repository { get; }

        public SqliteMediatorRepository CreateRepository()
        {
            TestEnvironment environment = new(Environments.Development);
            AesGcmEndpointProtector protector = new(
                Microsoft.Extensions.Options.Options.Create(Options),
                environment);
            AesGcmDeviceCredentialProtector credentialProtector = new(
                Microsoft.Extensions.Options.Options.Create(Options),
                environment);
            return new SqliteMediatorRepository(
                Microsoft.Extensions.Options.Options.Create(Options),
                protector,
                credentialProtector);
        }

        public static async Task<TestRepositoryFixture> CreateAsync()
        {
            string databasePath = Path.Combine(Path.GetTempPath(), $"vpn-mediator-test-{Guid.NewGuid():N}.db");
            VpnMediatorOptions options = new()
            {
                SqliteDatabasePath = databasePath,
                DatabasePath = Path.Combine(Path.GetTempPath(), $"vpn-mediator-test-{Guid.NewGuid():N}.json"),
                PublicBaseUrl = "https://vpn.example",
                DeviceTokenHashKey = "test-device-token-hash-key-with-at-least-32-characters",
                DeviceCredentialProtectionKeyId = "test-v1",
                DeviceCredentialProtectionKey = Convert.ToBase64String(RandomNumberGenerator.GetBytes(32)),
                LinkSigningSecret = "",
                AdminToken = "test-admin-token-with-at-least-32-characters",
                SourceEndpointProtectionKey = Convert.ToBase64String(RandomNumberGenerator.GetBytes(32)),
                ServerCatalogMaxStaleHours = 24
            };
            TestEnvironment environment = new(Environments.Development);
            AesGcmEndpointProtector protector = new(Microsoft.Extensions.Options.Options.Create(options), environment);
            AesGcmDeviceCredentialProtector credentialProtector = new(
                Microsoft.Extensions.Options.Options.Create(options),
                environment);
            SqliteMediatorRepository repository = new(
                Microsoft.Extensions.Options.Options.Create(options),
                protector,
                credentialProtector);
            await repository.InitializeAsync(CancellationToken.None);
            return new TestRepositoryFixture(databasePath, options, repository);
        }

        public async Task<Guid> CreateSubscriptionAsync(
            int maxDevices,
            DateTimeOffset? validUntilUtc = null)
        {
            CreateSubscriptionResult result = await Repository.CreateMediatedSubscriptionAsync(
                new CreateSubscriptionCommand(
                    ExternalRequestId: Guid.NewGuid().ToString("N"),
                    CustomerReference: "test",
                    Note: "test",
                    PublicGuid: null,
                    Entitlement: new EntitlementUpdateRequest(
                        1,
                        EntitlementStatuses.Active,
                        validUntilUtc ?? DateTimeOffset.UtcNow.AddDays(30),
                        maxDevices)),
                DateTimeOffset.UtcNow,
                CancellationToken.None);
            return result.PublicGuid;
        }

        public ValueTask DisposeAsync()
        {
            if (File.Exists(DatabasePath))
            {
                File.Delete(DatabasePath);
            }

            return ValueTask.CompletedTask;
        }
    }
}

public sealed class SqliteConcurrencyConfigurationTests
{
    [Fact]
    public async Task RepositoryInitializationEnablesWalJournalMode()
    {
        string databasePath = Path.Combine(
            Path.GetTempPath(),
            $"vpn-mediator-wal-{Guid.NewGuid():N}.db");
        VpnMediatorOptions options = new()
        {
            SqliteDatabasePath = databasePath,
            DatabasePath = databasePath + ".json",
            PublicBaseUrl = "https://vpn.example",
            DeviceTokenHashKey = "test-device-token-hash-key-with-at-least-32-characters",
            DeviceCredentialProtectionKeyId = "test-v1",
            DeviceCredentialProtectionKey = Convert.ToBase64String(
                RandomNumberGenerator.GetBytes(32)),
            AdminToken = "test-admin-token-with-at-least-32-characters",
            SourceEndpointProtectionKey = Convert.ToBase64String(
                RandomNumberGenerator.GetBytes(32))
        };
        WalTestEnvironment environment = new();
        SqliteMediatorRepository repository = new(
            Options.Create(options),
            new AesGcmEndpointProtector(Options.Create(options), environment),
            new AesGcmDeviceCredentialProtector(Options.Create(options), environment));

        try
        {
            await repository.InitializeAsync(CancellationToken.None);
            await using SqliteConnection connection = new($"Data Source={databasePath}");
            await connection.OpenAsync();
            await using SqliteCommand command = connection.CreateCommand();
            command.CommandText = "PRAGMA journal_mode;";

            string mode = Convert.ToString(await command.ExecuteScalarAsync()) ?? string.Empty;

            Assert.Equal("wal", mode, ignoreCase: true);
        }
        finally
        {
            File.Delete(databasePath);
            File.Delete(databasePath + "-wal");
            File.Delete(databasePath + "-shm");
        }
    }

    private sealed class WalTestEnvironment : IWebHostEnvironment
    {
        public string EnvironmentName { get; set; } = Environments.Development;
        public string ApplicationName { get; set; } = "Tests";
        public string WebRootPath { get; set; } = Directory.GetCurrentDirectory();
        public IFileProvider WebRootFileProvider { get; set; } = new NullFileProvider();
        public string ContentRootPath { get; set; } = Directory.GetCurrentDirectory();
        public IFileProvider ContentRootFileProvider { get; set; } = new NullFileProvider();
    }
}
