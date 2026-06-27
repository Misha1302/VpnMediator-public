using System.Diagnostics;
using System.Globalization;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading.RateLimiting;
using Microsoft.AspNetCore.HttpOverrides;
using Microsoft.AspNetCore.RateLimiting;
using Microsoft.Extensions.Options;

WebApplicationBuilder builder = WebApplication.CreateBuilder(args);
builder.WebHost.ConfigureKestrel(options =>
{
    options.Limits.MaxRequestBodySize = 64 * 1024;
});

builder.Services.AddOptions<VpnMediatorOptions>()
    .Configure(options => VpnMediatorOptionsConfiguration.Bind(builder.Configuration, options))
    .ValidateOnStart();

builder.Services.AddSingleton<ILinkSigner, HmacLinkSigner>();
builder.Services.AddSingleton<ISubscriptionLinkFactory, SubscriptionLinkFactory>();
builder.Services.AddSingleton<IDeviceSubscriptionLinkFactory, DeviceSubscriptionLinkFactory>();
builder.Services.AddSingleton<ISubscriptionFeedLinkFactory, SubscriptionFeedLinkFactory>();
builder.Services.AddSingleton<ISubscriptionResponseBuilder, SubscriptionResponseBuilder>();
builder.Services.AddSingleton<IEndpointProtector, AesGcmEndpointProtector>();
builder.Services.AddSingleton<IDeviceCredentialProtector, AesGcmDeviceCredentialProtector>();
builder.Services.AddSingleton<SqliteMediatorRepository>();
builder.Services.AddSingleton<ISubscriptionRepository>(provider => provider.GetRequiredService<SqliteMediatorRepository>());
builder.Services.AddSingleton<IValidateOptions<VpnMediatorOptions>, VpnMediatorOptionsValidator>();
builder.Services.AddSingleton<IHostAddressResolver, DnsHostAddressResolver>();
builder.Services.AddSingleton<ISsrfSafeHttpFetcher, SsrfSafeHttpFetcher>();
builder.Services.AddSingleton<IUpstreamSourceReader, SubscriptionUrlSourceReader>();
builder.Services.AddSingleton<IUpstreamSourceReaderRegistry, UpstreamSourceReaderRegistry>();
builder.Services.AddHostedService<CatalogRefreshWorker>();
builder.Services.Configure<HostOptions>(options =>
{
    options.BackgroundServiceExceptionBehavior = BackgroundServiceExceptionBehavior.StopHost;
});
builder.Services.Configure<ForwardedHeadersOptions>(options =>
{
    options.ForwardedHeaders = ForwardedHeaders.XForwardedFor | ForwardedHeaders.XForwardedProto;
    options.ForwardLimit = 1;
    options.KnownProxies.Clear();
    options.KnownProxies.Add(IPAddress.Loopback);
    options.KnownProxies.Add(IPAddress.IPv6Loopback);
});
builder.Services.AddRateLimiter(rateLimiterOptions =>
{
    rateLimiterOptions.RejectionStatusCode = StatusCodes.Status429TooManyRequests;
    rateLimiterOptions.GlobalLimiter = PartitionedRateLimiter.Create<HttpContext, string>(
        httpContext => RateLimitPartition.GetFixedWindowLimiter(
            RateLimitPartitionKey.ForRemoteAddress(httpContext, "global"),
            _ => new FixedWindowRateLimiterOptions
            {
                PermitLimit = 600,
                Window = TimeSpan.FromMinutes(1),
                QueueLimit = 0,
                AutoReplenishment = true
            }));
    rateLimiterOptions.OnRejected = async (context, cancellationToken) =>
    {
        context.HttpContext.Response.StatusCode = StatusCodes.Status429TooManyRequests;
        context.HttpContext.Response.ContentType = "application/json";
        if (context.Lease.TryGetMetadata(MetadataName.RetryAfter, out TimeSpan retryAfter))
        {
            context.HttpContext.Response.Headers.RetryAfter = Math.Max(
                (int)Math.Ceiling(retryAfter.TotalSeconds),
                1).ToString();
        }
        else
        {
            context.HttpContext.Response.Headers.RetryAfter = "60";
        }
        await context.HttpContext.Response.WriteAsJsonAsync(
            new
            {
                errorCode = "rate_limited",
                message = "Too many requests.",
                traceId = context.HttpContext.TraceIdentifier
            },
            cancellationToken);
    };
    rateLimiterOptions.AddPolicy("subscription", httpContext =>
        RateLimitPartition.GetFixedWindowLimiter(
            RateLimitPartitionKey.ForSubscription(httpContext),
            _ => new FixedWindowRateLimiterOptions
            {
                PermitLimit = 120,
                Window = TimeSpan.FromMinutes(1),
                QueueLimit = 0,
                AutoReplenishment = true
            }));
});

WebApplication app = builder.Build();
if (args.Contains("--validate-options-only", StringComparer.Ordinal))
{
    _ = app.Services.GetRequiredService<IOptions<VpnMediatorOptions>>().Value;
    Console.WriteLine("VpnMediator options validation passed.");
    return;
}

app.UseForwardedHeaders();
app.Use(async (httpContext, next) =>
{
    try
    {
        await next(httpContext);
    }
    catch (BadHttpRequestException exception)
    {
        if (httpContext.Response.HasStarted)
        {
            throw;
        }

        int statusCode = exception.StatusCode is StatusCodes.Status400BadRequest
            or StatusCodes.Status413PayloadTooLarge
            ? exception.StatusCode
            : StatusCodes.Status400BadRequest;
        httpContext.Response.StatusCode = statusCode;
        await httpContext.Response.WriteAsJsonAsync(new
        {
            errorCode = statusCode == StatusCodes.Status413PayloadTooLarge
                ? "request_too_large"
                : "invalid_request",
            message = "The request could not be processed.",
            traceId = httpContext.TraceIdentifier
        });
    }
    catch (JsonException)
    {
        if (httpContext.Response.HasStarted)
        {
            throw;
        }

        httpContext.Response.StatusCode = StatusCodes.Status400BadRequest;
        await httpContext.Response.WriteAsJsonAsync(new
        {
            errorCode = "invalid_json",
            message = "The JSON body is invalid.",
            traceId = httpContext.TraceIdentifier
        });
    }
    catch (Exception exception)
    {
        if (httpContext.Response.HasStarted)
        {
            throw;
        }

        ILogger logger = httpContext.RequestServices
            .GetRequiredService<ILoggerFactory>()
            .CreateLogger("VpnMediator.Errors");
        logger.LogError(
            "Unhandled request failure of type {ExceptionType}.",
            exception.GetType().FullName);
        httpContext.Response.StatusCode = StatusCodes.Status500InternalServerError;
        await httpContext.Response.WriteAsJsonAsync(new
        {
            errorCode = "internal_error",
            message = "The request failed.",
            traceId = httpContext.TraceIdentifier
        });
    }
});
app.UseRouting();
app.Use(async (httpContext, next) =>
{
    if (SubscriptionResponseSecurity.IsProtectedPath(httpContext.Request.Path))
    {
        SubscriptionResponseSecurity.Apply(httpContext.Response);
        httpContext.Response.OnStarting(
            static state =>
            {
                SubscriptionResponseSecurity.Apply((HttpResponse)state);
                return Task.CompletedTask;
            },
            httpContext.Response);
    }

    await next(httpContext);
});
app.Use(async (httpContext, next) =>
{
    foreach (string key in new[] { "publicId", "devicePublicId" })
    {
        string? value = httpContext.Request.RouteValues[key]?.ToString();
        if (value is not null && !RouteIdentifierValidator.IsSafe(value))
        {
            httpContext.Response.StatusCode = StatusCodes.Status400BadRequest;
            await httpContext.Response.WriteAsJsonAsync(new
            {
                errorCode = "invalid_route_identifier",
                message = "The route identifier is invalid.",
                traceId = httpContext.TraceIdentifier
            });
            return;
        }
    }

    await next(httpContext);
});
ILogger correlationLogger = app.Services
    .GetRequiredService<ILoggerFactory>()
    .CreateLogger("VpnMediator.Request");
app.Use(async (httpContext, next) =>
{
    long requestStartedTimestamp = Stopwatch.GetTimestamp();
    MediatorMetrics.RequestStarted();
    string? requestedCorrelationId =
        httpContext.Request.Headers["X-Correlation-ID"].FirstOrDefault();
    bool requestedCorrelationIdIsSafe =
        requestedCorrelationId is not null
        && requestedCorrelationId.Length is > 0 and <= 64
        && requestedCorrelationId.All(character =>
            char.IsLetterOrDigit(character) || character is '-' or '_');
    string correlationId = requestedCorrelationIdIsSafe
        ? requestedCorrelationId!
        : Guid.NewGuid().ToString("N");
    httpContext.TraceIdentifier = correlationId;
    httpContext.Response.Headers["X-Correlation-ID"] = correlationId;

    using IDisposable? scope = correlationLogger.BeginScope(
        "CorrelationId: {CorrelationId}",
        correlationId);
    correlationLogger.LogInformation(
        "Request started: {Method} {Path}",
        httpContext.Request.Method,
        httpContext.Request.Path);
    try
    {
        await next(httpContext);
    }
    finally
    {
        double durationMilliseconds = Stopwatch
            .GetElapsedTime(requestStartedTimestamp)
            .TotalMilliseconds;
        correlationLogger.LogInformation(
            "Request completed: {Method} {Path} {StatusCode} in {DurationMilliseconds:F1} ms",
            httpContext.Request.Method,
            httpContext.Request.Path,
            httpContext.Response.StatusCode,
            durationMilliseconds);
        MediatorMetrics.RequestCompleted(httpContext.Response.StatusCode);
    }
});
app.Use(async (httpContext, next) =>
{
    if (AdminPathAuthorization.IsProtectedPath(httpContext.Request.Path))
    {
        VpnMediatorOptions options = httpContext.RequestServices
            .GetRequiredService<IOptions<VpnMediatorOptions>>()
            .Value;
        if (!AdminPathAuthorization.IsAllowed(httpContext, options))
        {
            IResult unauthorized = ApiResults.UnauthorizedText(httpContext);
            await unauthorized.ExecuteAsync(httpContext);
            return;
        }
    }

    await next(httpContext);
});
app.UseRateLimiter();
await app.Services.GetRequiredService<SqliteMediatorRepository>().InitializeAsync(app.Lifetime.ApplicationStopping);

app.MapGet("/metrics", async Task<IResult> (
    HttpContext httpContext,
    SqliteMediatorRepository repository,
    CancellationToken cancellationToken) =>
{
    if (httpContext.Connection.RemoteIpAddress is not IPAddress address
        || !IPAddress.IsLoopback(address))
    {
        return ApiResults.NotFoundText("Resource was not found.");
    }

    ReadinessStatus readiness = await repository.GetReadinessStatusAsync(
        DateTimeOffset.UtcNow,
        cancellationToken);
    return Results.Text(
        MediatorMetrics.Render(readiness),
        "text/plain; version=0.0.4; charset=utf-8");
});

app.MapGet("/ping", () => Results.Text("pong", "text/plain; charset=utf-8"));

app.MapGet("/health/live", () => Results.Ok(new { status = "live" }));

app.MapGet("/health/ready", async Task<IResult> (
    SqliteMediatorRepository repository,
    IOptions<VpnMediatorOptions> options,
    CancellationToken cancellationToken) =>
{
    MediatorReadinessSnapshot snapshot = await MediatorReadinessSnapshotBuilder.BuildWithDeadlineAsync(
        repository,
        options.Value,
        cancellationToken);
    PublicReadinessResponse publicBody = MediatorReadinessResponseFactory.CreatePublic(snapshot);
    return snapshot.Ready
        ? Results.Ok(publicBody)
        : Results.Json(publicBody, statusCode: StatusCodes.Status503ServiceUnavailable);
});

app.MapGet("/internal/health/ready", async Task<IResult> (
    HttpContext httpContext,
    SqliteMediatorRepository repository,
    IOptions<VpnMediatorOptions> options,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(
            httpContext,
            options.Value.AdminToken,
            options.Value.PreviousAdminToken,
            options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.NotFoundText("Resource was not found.");
    }

    MediatorReadinessSnapshot snapshot = await MediatorReadinessSnapshotBuilder.BuildWithDeadlineAsync(
        repository,
        options.Value,
        cancellationToken);
    return snapshot.Ready
        ? Results.Ok(snapshot.Body)
        : Results.Json(snapshot.Body, statusCode: StatusCodes.Status503ServiceUnavailable);
});

app.MapPost("/admin/subscriptions", async Task<IResult> (
    CreateSubscriptionRequest request,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    CreateSubscriptionResult result = await repository.CreateMediatedSubscriptionAsync(
        new CreateSubscriptionCommand(
            request.ExternalRequestId,
            TextSanitizer.NullIfWhiteSpace(request.CustomerReference),
            TextSanitizer.NullIfWhiteSpace(request.Note),
            null,
            request.Entitlement),
        DateTimeOffset.UtcNow,
        cancellationToken);

    return Results.Ok(new
    {
        publicGuid = result.PublicGuid,
        alreadyExisted = result.AlreadyExisted
    });
});

app.MapPut("/admin/subscriptions/{publicGuid:guid}/entitlement", async Task<IResult> (
    Guid publicGuid,
    EntitlementUpdateRequest request,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    EntitlementUpdateResult result = await repository.ApplyEntitlementAsync(
        publicGuid,
        request,
        DateTimeOffset.UtcNow,
        cancellationToken);

    return result.Status switch
    {
        EntitlementUpdateStatus.SubscriptionNotFound => ApiResults.NotFoundText("Subscription was not found."),
        EntitlementUpdateStatus.InvalidState => ApiResults.BadRequestText("Entitlement version was already used with a different state."),
        EntitlementUpdateStatus.StaleVersionRejected => ApiResults.Conflict(
            "stale_entitlement_version",
            "Entitlement version is older than the current mediator version.",
            new { result.CurrentVersion },
            httpContext),
        EntitlementUpdateStatus.DeviceLimitDecreaseNotAllowed => ApiResults.Conflict(
            "device_limit_decrease_not_allowed",
            "The requested device limit is lower than the current entitlement device limit.",
            new
            {
                currentVersion = result.CurrentVersion,
                requestedMaxDeviceTokens = request.MaxDeviceTokens
            },
            httpContext),
        EntitlementUpdateStatus.ActiveDevicesExceedNewLimit => ApiResults.Conflict(
            "active_devices_exceed_new_limit",
            "The requested device limit is lower than the current active device count.",
            new
            {
                activeDeviceTokens = result.ActiveDeviceTokens,
                requestedMaxDeviceTokens = request.MaxDeviceTokens
            },
            httpContext),
        EntitlementUpdateStatus.AlreadyApplied => Results.Ok(new
        {
            status = "already_applied",
            result.CurrentVersion
        }),
        _ => Results.Ok(new
        {
            status = "applied",
            currentVersion = request.Version
        })
    };
});

app.MapPost("/admin/subscriptions/{publicGuid:guid}/entitlement-operations", async Task<IResult> (
    Guid publicGuid,
    EntitlementOperationRequest request,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText(httpContext);
    }

    EntitlementOperationResult result = await repository.ApplyEntitlementOperationAsync(
        publicGuid,
        request,
        DateTimeOffset.UtcNow,
        cancellationToken);

    return result.Status switch
    {
        EntitlementOperationStatus.SubscriptionNotFound =>
            ApiResults.NotFoundText("Subscription was not found."),
        EntitlementOperationStatus.IdempotencyConflict => ApiResults.Conflict(
            "entitlement_operation_idempotency_conflict",
            "The operation id was already used with a different normalized request.",
            new { result.OperationId },
            httpContext),
        EntitlementOperationStatus.VersionConflict => ApiResults.Conflict(
            "entitlement_operation_version_conflict",
            "The authoritative entitlement version changed before this operation was applied.",
            new { result.ExpectedVersion },
            httpContext),
        EntitlementOperationStatus.DeviceLimitDecreaseNotAllowed => ApiResults.Conflict(
            "device_limit_decrease_not_allowed",
            "The requested device limit is lower than the current entitlement device limit.",
            new { result.ExpectedVersion },
            httpContext),
        EntitlementOperationStatus.ActiveDevicesExceedNewLimit => ApiResults.Conflict(
            "active_devices_exceed_new_limit",
            "The requested device limit is lower than the current active device count.",
            new { result.ActiveDeviceTokens },
            httpContext),
        _ => Results.Ok(new
        {
            status = result.Status == EntitlementOperationStatus.AlreadyApplied
                ? "already_applied"
                : "applied",
            result.OperationId,
            result.PublicGuid,
            result.OperationType,
            result.ExpectedVersion,
            result.ResultVersion,
            result.ResultStatus,
            result.ResultValidUntilUtc,
            result.ResultMaxDeviceTokens,
            result.AppliedAtUtc
        })
    };
});

app.MapGet("/admin/entitlement-operations/{operationId}", async Task<IResult> (
    string operationId,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText(httpContext);
    }

    EntitlementOperationResult? result = await repository.GetEntitlementOperationAsync(
        operationId,
        cancellationToken);
    return result is null
        ? ApiResults.NotFoundText("Entitlement operation was not found.")
        : Results.Ok(new
        {
            status = "applied",
            result.OperationId,
            result.PublicGuid,
            result.OperationType,
            result.ExpectedVersion,
            result.ResultVersion,
            result.ResultStatus,
            result.ResultValidUntilUtc,
            result.ResultMaxDeviceTokens,
            result.AppliedAtUtc
        });
});

app.MapGet(
    "/admin/subscriptions/{publicGuid:guid}/entitlement-operations/by-result-version/{resultVersion:long}",
    async Task<IResult> (
        Guid publicGuid,
        long resultVersion,
        HttpContext httpContext,
        IOptions<VpnMediatorOptions> options,
        SqliteMediatorRepository repository,
        CancellationToken cancellationToken) =>
    {
        if (!AdminGuard.IsAllowed(
            httpContext,
            options.Value.AdminToken,
            options.Value.PreviousAdminToken,
            options.Value.PreviousAdminTokenValidUntilUtc))
        {
            return ApiResults.UnauthorizedText(httpContext);
        }

        if (resultVersion < 1)
        {
            return ApiResults.BadRequest("invalid_result_version", "Result version must be positive.");
        }

        EntitlementOperationResult? result =
            await repository.GetEntitlementOperationByResultVersionAsync(
                publicGuid,
                resultVersion,
                cancellationToken);
        return result is null
            ? ApiResults.NotFoundText("Entitlement operation was not found.")
            : Results.Ok(new
            {
                status = "applied",
                result.OperationId,
                result.PublicGuid,
                result.OperationType,
                result.ExpectedVersion,
                result.ResultVersion,
                result.ResultStatus,
                result.ResultValidUntilUtc,
                result.ResultMaxDeviceTokens,
                result.AppliedAtUtc
            });
    });

app.MapGet("/admin/subscriptions/{publicGuid:guid}/entitlement", async Task<IResult> (
    Guid publicGuid,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText(httpContext);
    }

    EntitlementDetails? entitlement = await repository.GetEntitlementDetailsAsync(
        publicGuid,
        cancellationToken);

    return entitlement is null
        ? ApiResults.NotFoundText("Subscription was not found.")
        : Results.Ok(entitlement);
});

app.MapPost("/admin/subscriptions/{publicGuid:guid}/feed-credential/ensure", async Task<IResult> (
    Guid publicGuid,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    ISubscriptionFeedLinkFactory linkFactory,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(
        httpContext,
        options.Value.AdminToken,
        options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText(httpContext);
    }

    SubscriptionFeedCredentialResult result = await repository.EnsureSubscriptionFeedCredentialAsync(
        publicGuid,
        httpContext,
        linkFactory,
        DateTimeOffset.UtcNow,
        cancellationToken);

    return result.Status switch
    {
        SubscriptionFeedCredentialStatuses.NotFound =>
            ApiResults.NotFoundText("Subscription was not found."),
        SubscriptionFeedCredentialStatuses.Invalid => ApiResults.Conflict(
            result.ErrorCode ?? "feed_credential_unavailable",
            "The subscription feed credential is unavailable.",
            null,
            httpContext),
        SubscriptionFeedCredentialStatuses.Created =>
            Results.Json(result, statusCode: StatusCodes.Status201Created),
        _ => Results.Ok(result)
    };
});

app.MapPost("/admin/subscriptions/{publicGuid:guid}/devices/{devicePublicId}/enable", async Task<IResult> (
    Guid publicGuid,
    string devicePublicId,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(
        httpContext,
        options.Value.AdminToken,
        options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText(httpContext);
    }

    UnifiedDeviceEnableResult result = await repository.EnableUnifiedDeviceAsync(
        publicGuid,
        devicePublicId,
        DateTimeOffset.UtcNow,
        cancellationToken);

    return result.Status switch
    {
        "not_found" => ApiResults.NotFoundText("Device was not found."),
        "limit_reached" => ApiResults.Conflict(
            result.ErrorCode ?? "device_limit_reached",
            "Device limit was reached.",
            new { result.OccupiedSlots, result.MaxDeviceTokens },
            httpContext),
        "invalid" => ApiResults.Conflict(
            result.ErrorCode ?? "device_enable_failed",
            "The device cannot be enabled while the subscription is inactive.",
            null,
            httpContext),
        _ => Results.Ok(result)
    };
});

app.MapGet("/admin/subscriptions/{publicGuid:guid}/device-tokens", async Task<IResult> (
    Guid publicGuid,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    IReadOnlyList<DeviceTokenListItem> tokens = await repository.ListDeviceTokensAsync(
        publicGuid,
        cancellationToken);

    return Results.Ok(tokens);
});

app.MapGet("/connect/{publicId}", (
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options) =>
{
    LegacyHandoffResponseSecurity.Apply(httpContext.Response);
    return Results.Content(
        LegacyHandoffTombstoneRenderer.Render(
            options.Value.ProductName,
            options.Value.SupportTelegramBotUsername),
        "text/html; charset=utf-8",
        Encoding.UTF8,
        StatusCodes.Status410Gone);
});

app.MapDelete("/admin/subscriptions/{publicGuid:guid}/device-tokens/{devicePublicId}", async Task<IResult> (
    Guid publicGuid,
    string devicePublicId,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    bool revoked = await repository.RevokeDeviceTokenAsync(
        publicGuid,
        devicePublicId,
        DateTimeOffset.UtcNow,
        cancellationToken);

    if (!revoked)
    {
        return ApiResults.NotFoundText("Subscription or device token was not found.");
    }

    return Results.Ok(new
    {
        publicGuid,
        devicePublicId,
        revoked = true
    });
});

app.MapDelete("/admin/subscriptions/{publicGuid:guid}/device-tokens", async Task<IResult> (
    Guid publicGuid,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    DeviceTokenRevokeAllResult result = await repository.RevokeAllDeviceTokensAsync(
        publicGuid,
        DateTimeOffset.UtcNow,
        cancellationToken);

    if (!result.SubscriptionFound)
    {
        return ApiResults.NotFoundText("Subscription was not found.");
    }

    return Results.Ok(new
    {
        publicGuid,
        revokedTokens = result.RevokedCount
    });
});

app.MapGet("/admin/server-sources", async Task<IResult> (
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    return Results.Ok(await repository.ListSourcesAsync(cancellationToken));
});

app.MapPost("/admin/server-sources", async Task<IResult> (
    CreateSourceRequest request,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    SourceDetails source = await repository.CreateSourceAsync(
        request,
        DateTimeOffset.UtcNow,
        cancellationToken);

    return Results.Ok(source);
});

app.MapPost("/admin/server-sources/{sourceId:long}/test", async Task<IResult> (
    long sourceId,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    IUpstreamSourceReaderRegistry readerRegistry,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    UpstreamSource? source = await repository.GetSourceAsync(sourceId, cancellationToken);

    if (source is null)
    {
        return ApiResults.NotFoundText("Source was not found.");
    }

    if (!readerRegistry.TryGet(source.Kind, out IUpstreamSourceReader? reader))
    {
        return ApiResults.BadRequestText("Source kind is not supported.");
    }

    SourceReadResult readResult = await reader!.ReadAsync(source, cancellationToken);
    return Results.Ok(await repository.SaveSourceTestResultAsync(
        sourceId,
        readResult,
        DateTimeOffset.UtcNow,
        cancellationToken));
});

app.MapPost("/admin/server-sources/{sourceId:long}/enable", async Task<IResult> (
    long sourceId,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    bool updated = await repository.SetSourceStateAsync(
        sourceId,
        SourceStates.Enabled,
        DateTimeOffset.UtcNow,
        cancellationToken);

    return updated ? Results.Ok(new { sourceId, state = SourceStates.Enabled }) : ApiResults.NotFoundText("Source was not found.");
});

app.MapPost("/admin/server-sources/{sourceId:long}/disable", async Task<IResult> (
    long sourceId,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    bool updated = await repository.SetSourceStateAsync(
        sourceId,
        SourceStates.Disabled,
        DateTimeOffset.UtcNow,
        cancellationToken);

    return updated ? Results.Ok(new { sourceId, state = SourceStates.Disabled }) : ApiResults.NotFoundText("Source was not found.");
});

app.MapPost("/admin/server-sources/{sourceId:long}/revoke", async Task<IResult> (
    long sourceId,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    bool updated = await repository.SetSourceStateAsync(
        sourceId,
        SourceStates.Revoked,
        DateTimeOffset.UtcNow,
        cancellationToken);

    return updated ? Results.Ok(new { sourceId, state = SourceStates.Revoked }) : ApiResults.NotFoundText("Source was not found.");
});

app.MapPost("/admin/server-sources/reencrypt", async Task<IResult> (
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(
        httpContext,
        options.Value.AdminToken,
        options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText(httpContext);
    }

    int reencrypted = await repository.ReencryptSourceEndpointsAsync(
        DateTimeOffset.UtcNow,
        cancellationToken);
    return Results.Ok(new { reencrypted });
});

app.MapPost("/admin/server-catalog/refresh", async Task<IResult> (
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    IUpstreamSourceReaderRegistry readerRegistry,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    return Results.Ok(await repository.RefreshCatalogAsync(
        readerRegistry,
        DateTimeOffset.UtcNow,
        cancellationToken));
});

app.MapGet("/admin/server-catalog/status", async Task<IResult> (
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    return Results.Ok(await repository.GetCatalogStatusAsync(cancellationToken));
});

app.MapPost("/admin/server-catalog/rollback", async Task<IResult> (
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    PublishedSnapshot? snapshot = await repository.RollbackPublishedSnapshotAsync(
        DateTimeOffset.UtcNow,
        cancellationToken);

    return snapshot is null ? Results.Conflict(new { status = "rollback_unavailable" }) : Results.Ok(snapshot);
});

app.MapGet("/admin/subscriptions", async Task<IResult> (
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    ISubscriptionRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    IReadOnlyList<SubscriptionRecord> subscriptions = await repository.GetSubscriptionsAsync(cancellationToken);

    return Results.Ok(subscriptions.Select(SubscriptionMapper.ToDetailsResponse));
});

app.MapGet("/admin/subscriptions/{publicGuid:guid}", async Task<IResult> (
    Guid publicGuid,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    ISubscriptionRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    SubscriptionRecord? subscription = await repository.GetSubscriptionAsync(
        publicGuid,
        cancellationToken);

    if (subscription is null)
    {
        return ApiResults.NotFoundText("Subscription was not found.");
    }

    return Results.Ok(SubscriptionMapper.ToDetailsResponse(subscription));
});

app.MapPatch("/admin/subscriptions/{publicGuid:guid}/limit", async Task<IResult> (
    Guid publicGuid,
    UpdateDeviceLimitRequest request,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    if (request.MaxDevices < 1 || request.MaxDevices > 100)
    {
        return ApiResults.BadRequestText("MaxDevices must be between 1 and 100.");
    }

    EntitlementDetails? current = await repository.GetEntitlementDetailsAsync(publicGuid, cancellationToken);

    if (current is null)
    {
        return ApiResults.NotFoundText("Subscription was not found.");
    }

    EntitlementUpdateResult result = await repository.ApplyEntitlementAsync(
        publicGuid,
        new EntitlementUpdateRequest(
            current.Version + 1,
            current.Status,
            current.ValidUntilUtc,
            request.MaxDevices),
        DateTimeOffset.UtcNow,
        cancellationToken);

    if (result.Status == EntitlementUpdateStatus.ActiveDevicesExceedNewLimit)
    {
        return ApiResults.Conflict(
            "active_devices_exceed_new_limit",
            "The requested device limit is lower than the current active device count.",
            new
            {
                activeDeviceTokens = result.ActiveDeviceTokens,
                requestedMaxDeviceTokens = request.MaxDevices
            },
            httpContext);
    }

    if (result.Status == EntitlementUpdateStatus.DeviceLimitDecreaseNotAllowed)
    {
        return ApiResults.Conflict(
            "device_limit_decrease_not_allowed",
            "The requested device limit is lower than the current entitlement device limit.",
            new
            {
                currentVersion = result.CurrentVersion,
                requestedMaxDeviceTokens = request.MaxDevices
            },
            httpContext);
    }

    return Results.Ok(new { publicGuid, request.MaxDevices });
});

app.MapPost("/admin/subscriptions/{publicGuid:guid}/enable", async Task<IResult> (
    Guid publicGuid,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    ISubscriptionRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    bool updated = await repository.SetSubscriptionActiveAsync(
        publicGuid,
        isActive: true,
        DateTimeOffset.UtcNow,
        cancellationToken);

    if (!updated)
    {
        return ApiResults.NotFoundText("Subscription was not found.");
    }

    return Results.Ok(new { publicGuid, isActive = true });
});

app.MapPost("/admin/subscriptions/{publicGuid:guid}/disable", async Task<IResult> (
    Guid publicGuid,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    ISubscriptionRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    bool updated = await repository.SetSubscriptionActiveAsync(
        publicGuid,
        isActive: false,
        DateTimeOffset.UtcNow,
        cancellationToken);

    if (!updated)
    {
        return ApiResults.NotFoundText("Subscription was not found.");
    }

    return Results.Ok(new
    {
        publicGuid,
        isActive = false
    });
});

app.MapDelete("/admin/subscriptions/{publicGuid:guid}/devices/{deviceBindingId:guid}", async Task<IResult> (
    Guid publicGuid,
    Guid deviceBindingId,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    ISubscriptionRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    bool removed = await repository.UnbindDeviceAsync(
        publicGuid,
        deviceBindingId,
        DateTimeOffset.UtcNow,
        cancellationToken);

    if (!removed)
    {
        return ApiResults.NotFoundText("Subscription or device binding was not found.");
    }

    return Results.Ok(new { publicGuid, deviceBindingId, unbound = true });
});

app.MapDelete("/admin/subscriptions/{publicGuid:guid}/devices", async Task<IResult> (
    Guid publicGuid,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    ISubscriptionRepository repository,
    CancellationToken cancellationToken) =>
{
    if (!AdminGuard.IsAllowed(httpContext, options.Value.AdminToken, options.Value.PreviousAdminToken,
        options.Value.PreviousAdminTokenValidUntilUtc))
    {
        return ApiResults.UnauthorizedText();
    }

    UnbindAllDevicesResult result = await repository.UnbindAllDevicesAsync(
        publicGuid,
        DateTimeOffset.UtcNow,
        cancellationToken);

    if (!result.SubscriptionFound)
    {
        return ApiResults.NotFoundText("Subscription was not found.");
    }

    return Results.Ok(new
    {
        publicGuid,
        unboundDevices = result.UnboundDevices
    });
});

app.MapGet("/sub/{publicGuid:guid}/servers.txt", async Task<IResult> (
    Guid publicGuid,
    string? sig,
    HttpContext httpContext,
    ILinkSigner signer,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    ISubscriptionResponseBuilder responseBuilder,
    CancellationToken cancellationToken) =>
{
    if (!options.Value.AllowLegacySubscriptionLinks)
    {
        await repository.RecordLegacyLinkDeniedAsync(publicGuid, DateTimeOffset.UtcNow, cancellationToken);
        return Results.Text(
            responseBuilder.BuildStatusSubscription(
                UserFacingStatus.LegacyLinkDisabled,
                TokenSubscriptionAccessResult.Forbidden(UserFacingStatus.LegacyLinkDisabled)),
            "text/plain; charset=utf-8");
    }

    if (!signer.IsValid(publicGuid, sig))
    {
        return ApiResults.ForbiddenText("Invalid subscription link.");
    }

    DateTimeOffset now = DateTimeOffset.UtcNow;
    TokenSubscriptionAccessResult access = await repository.ValidateLegacyAccessAsync(
        publicGuid,
        now,
        cancellationToken);

    if (!access.SubscriptionFound)
    {
        return ApiResults.NotFoundText("Subscription was not found.");
    }

    if (!access.Allowed)
    {
        return Results.Text(
            responseBuilder.BuildStatusSubscription(access.Status, access),
            "text/plain; charset=utf-8");
    }

    PublishedSnapshot? snapshot = await repository.GetEffectivePublishedSnapshotAsync(
        DateTimeOffset.UtcNow,
        cancellationToken);

    if (snapshot is null || snapshot.ServerLinks.Count == 0)
    {
        return Results.Text(
            responseBuilder.BuildStatusSubscription(UserFacingStatus.ServersUnavailable, access),
            "text/plain; charset=utf-8");
    }

    HappSubscriptionMetadata.Apply(
        httpContext.Response,
        access,
        options.Value.SupportTelegramBotUsername);

    return Results.Text(
        responseBuilder.BuildSnapshotSubscription(snapshot.ServerLinks, access),
        "text/plain; charset=utf-8");
}).RequireRateLimiting("subscription");

app.MapGet("/sub/{publicGuid:guid}/servers.decoded.txt", async Task<IResult> (
    Guid publicGuid,
    string? sig,
    ILinkSigner signer,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    ISubscriptionResponseBuilder responseBuilder,
    CancellationToken cancellationToken) =>
{
    if (!options.Value.AllowLegacySubscriptionLinks)
    {
        await repository.RecordLegacyLinkDeniedAsync(publicGuid, DateTimeOffset.UtcNow, cancellationToken);
        return Results.Text(
            string.Join("\n", responseBuilder.BuildStatusServerLinks(
                UserFacingStatus.LegacyLinkDisabled,
                TokenSubscriptionAccessResult.Forbidden(UserFacingStatus.LegacyLinkDisabled))),
            "text/plain; charset=utf-8");
    }

    if (!signer.IsValid(publicGuid, sig))
    {
        return ApiResults.ForbiddenText("Invalid subscription link.");
    }

    DateTimeOffset now = DateTimeOffset.UtcNow;
    TokenSubscriptionAccessResult access = await repository.ValidateLegacyAccessAsync(
        publicGuid,
        now,
        cancellationToken);

    if (!access.SubscriptionFound)
    {
        return ApiResults.NotFoundText("Subscription was not found.");
    }

    IReadOnlyList<string> decodedServers;

    if (!access.Allowed)
    {
        decodedServers = responseBuilder.BuildStatusServerLinks(access.Status, access);
    }
    else
    {
        PublishedSnapshot? snapshot = await repository.GetEffectivePublishedSnapshotAsync(
            DateTimeOffset.UtcNow,
            cancellationToken);

        decodedServers = snapshot is null || snapshot.ServerLinks.Count == 0
            ? responseBuilder.BuildStatusServerLinks(UserFacingStatus.ServersUnavailable, access)
            : responseBuilder.BuildSnapshotServerLinks(snapshot.ServerLinks, access);
    }

    return Results.Text(
        string.Join("\n", decodedServers),
        "text/plain; charset=utf-8");
}).RequireRateLimiting("subscription");

app.MapGet("/sub/{publicGuid:guid}/feed", async Task<IResult> (
    Guid publicGuid,
    string? token,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    ISubscriptionResponseBuilder responseBuilder,
    CancellationToken cancellationToken) =>
{
    DeviceAccessRequestContext requestContext = DeviceAccessRequestContextFactory.Create(
        httpContext.Request.Headers.UserAgent.ToString(),
        httpContext.Connection.RemoteIpAddress,
        options.Value.DeviceObservationHashKey,
        httpContext.Request.Headers["x-hwid"].ToString(),
        httpContext.Request.Headers["x-device-os"].ToString(),
        httpContext.Request.Headers["x-ver-os"].ToString(),
        httpContext.Request.Headers["x-device-model"].ToString(),
        options.Value.DeviceIdentityHashKeyId,
        options.Value.DeviceIdentityHashKey,
        options.Value.PreviousDeviceIdentityHashKeyId,
        options.Value.PreviousDeviceIdentityHashKey);
    UnifiedFeedDeviceResolution resolution = await repository.ResolveUnifiedFeedDeviceAsync(
        publicGuid,
        token,
        requestContext,
        DateTimeOffset.UtcNow,
        cancellationToken);

    if (!resolution.Access.SubscriptionFound)
    {
        return ApiResults.NotFoundText("Subscription was not found.");
    }

    if (!resolution.Access.Allowed || resolution.DevicePublicId is null)
    {
        return Results.Text(
            responseBuilder.BuildStatusSubscription(
                resolution.Access.Status,
                resolution.Access),
            "text/plain; charset=utf-8");
    }

    PublishedSnapshot? snapshot = await repository.GetEffectivePublishedSnapshotAsync(
        DateTimeOffset.UtcNow,
        cancellationToken);
    if (snapshot is null || snapshot.ServerLinks.Count == 0)
    {
        return Results.Text(
            responseBuilder.BuildStatusSubscription(
                UserFacingStatus.ServersUnavailable,
                resolution.Access),
            "text/plain; charset=utf-8");
    }

    await repository.MarkUnifiedDeviceActivatedAsync(
        publicGuid,
        resolution.DevicePublicId,
        DateTimeOffset.UtcNow,
        cancellationToken);
    HappSubscriptionMetadata.Apply(
        httpContext.Response,
        resolution.Access,
        options.Value.SupportTelegramBotUsername);
    return Results.Text(
        responseBuilder.BuildSnapshotSubscription(snapshot.ServerLinks, resolution.Access),
        "text/plain; charset=utf-8");
}).RequireRateLimiting("subscription");

app.MapGet("/sub/{publicGuid:guid}/devices/{devicePublicId}/servers.txt", async Task<IResult> (
    Guid publicGuid,
    string devicePublicId,
    string? token,
    HttpContext httpContext,
    IOptions<VpnMediatorOptions> options,
    SqliteMediatorRepository repository,
    ISubscriptionResponseBuilder responseBuilder,
    CancellationToken cancellationToken) =>
{
    TokenSubscriptionAccessResult access = await repository.ValidateDeviceTokenAccessAsync(
        publicGuid,
        devicePublicId,
        token,
        DateTimeOffset.UtcNow,
        cancellationToken,
        httpContext.Request.Headers.UserAgent.ToString(),
        httpContext.Connection.RemoteIpAddress,
        httpContext.Request.Headers["x-hwid"].ToString(),
        httpContext.Request.Headers["x-device-os"].ToString(),
        httpContext.Request.Headers["x-ver-os"].ToString(),
        httpContext.Request.Headers["x-device-model"].ToString());

    if (!access.SubscriptionFound)
    {
        return ApiResults.NotFoundText("Subscription was not found.");
    }

    if (!access.Allowed)
    {
        return Results.Text(
            responseBuilder.BuildStatusSubscription(access.Status, access),
            "text/plain; charset=utf-8");
    }

    PublishedSnapshot? snapshot = await repository.GetEffectivePublishedSnapshotAsync(
        DateTimeOffset.UtcNow,
        cancellationToken);
    if (snapshot is null || snapshot.ServerLinks.Count == 0)
    {
        return Results.Text(
            responseBuilder.BuildStatusSubscription(UserFacingStatus.ServersUnavailable, access),
            "text/plain; charset=utf-8");
    }

    HappSubscriptionMetadata.Apply(
        httpContext.Response,
        access,
        options.Value.SupportTelegramBotUsername);

    return Results.Text(
        responseBuilder.BuildSnapshotSubscription(snapshot.ServerLinks, access),
        "text/plain; charset=utf-8");
}).RequireRateLimiting("subscription");

app.Run();

public static class MediatorMetrics
{
    private static long _requestsStarted;
    private static long _requestsCompleted;
    private static long _clientErrors;
    private static long _serverErrors;

    public static void RequestStarted()
    {
        Interlocked.Increment(ref _requestsStarted);
    }

    public static void RequestCompleted(int statusCode)
    {
        Interlocked.Increment(ref _requestsCompleted);
        if (statusCode is >= 400 and < 500)
        {
            Interlocked.Increment(ref _clientErrors);
        }
        else if (statusCode >= 500)
        {
            Interlocked.Increment(ref _serverErrors);
        }
    }

    public static string Render(ReadinessStatus readiness)
    {
        long started = Interlocked.Read(ref _requestsStarted);
        long completed = Interlocked.Read(ref _requestsCompleted);
        return string.Join(
            '\n',
            [
                "# TYPE vpnmediator_http_requests_started_total counter",
                $"vpnmediator_http_requests_started_total {started}",
                "# TYPE vpnmediator_http_requests_completed_total counter",
                $"vpnmediator_http_requests_completed_total {completed}",
                "# TYPE vpnmediator_http_client_errors_total counter",
                $"vpnmediator_http_client_errors_total {Interlocked.Read(ref _clientErrors)}",
                "# TYPE vpnmediator_http_server_errors_total counter",
                $"vpnmediator_http_server_errors_total {Interlocked.Read(ref _serverErrors)}",
                "# TYPE vpnmediator_http_requests_in_flight gauge",
                $"vpnmediator_http_requests_in_flight {Math.Max(started - completed, 0)}",
                "# TYPE vpnmediator_catalog_servers gauge",
                $"vpnmediator_catalog_servers {readiness.ServerCount}",
                "# TYPE vpnmediator_ready gauge",
                $"vpnmediator_ready {(readiness.HttpStatusCode == StatusCodes.Status200OK ? 1 : 0)}",
                string.Empty
            ]);
    }
}

public sealed record MediatorReadinessSnapshot(
    bool Ready,
    IReadOnlyDictionary<string, object?> Body);

public sealed record PublicReadinessResponse(string Status);

public static class MediatorReadinessResponseFactory
{
    public static PublicReadinessResponse CreatePublic(MediatorReadinessSnapshot snapshot)
    {
        return new PublicReadinessResponse(snapshot.Ready ? "ready" : "not_ready");
    }
}

public static class MediatorReadinessSnapshotBuilder
{
    public static Task<MediatorReadinessSnapshot> BuildWithDeadlineAsync(
        SqliteMediatorRepository repository,
        VpnMediatorOptions options,
        CancellationToken cancellationToken)
    {
        return ExecuteWithDeadlineAsync(
            token => BuildAsync(repository, options, token),
            TimeSpan.FromSeconds(options.ReadinessTimeoutSeconds),
            cancellationToken);
    }

    public static async Task<MediatorReadinessSnapshot> ExecuteWithDeadlineAsync(
        Func<CancellationToken, Task<MediatorReadinessSnapshot>> operation,
        TimeSpan timeout,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(operation);
        if (timeout <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(timeout));
        }

        using CancellationTokenSource deadline =
            CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(timeout);
        try
        {
            return await operation(deadline.Token).WaitAsync(timeout, cancellationToken);
        }
        catch (TimeoutException)
        {
            deadline.Cancel();
            return CreateDeadlineExceededSnapshot(timeout);
        }
        catch (OperationCanceledException)
            when (!cancellationToken.IsCancellationRequested && deadline.IsCancellationRequested)
        {
            return CreateDeadlineExceededSnapshot(timeout);
        }
    }

    private static MediatorReadinessSnapshot CreateDeadlineExceededSnapshot(TimeSpan timeout)
    {
        return new MediatorReadinessSnapshot(
            false,
            new Dictionary<string, object?>(StringComparer.Ordinal)
            {
                ["status"] = "not_ready",
                ["reason"] = "readiness_deadline_exceeded",
                ["readinessDeadlineSeconds"] = timeout.TotalSeconds,
                ["snapshotCapturedAtUtc"] = DateTimeOffset.UtcNow
            });
    }

    public static async Task<MediatorReadinessSnapshot> BuildAsync(
        SqliteMediatorRepository repository,
        VpnMediatorOptions options,
        CancellationToken cancellationToken)
    {
        MigrationState migrationState = await repository.GetMigrationStateAsync(cancellationToken);
        int migrations = migrationState.DatabaseMaxVersion;
        const int expectedMigrations = SqliteMediatorRepository.CurrentMigrationVersion;
        bool migrationsCurrent = migrationState.IsCurrent;
        string effectiveDeviceSecurityPosture = DeviceSecurityPostureEvaluator.GetEffective(options);
        bool deviceSecurityPostureSatisfied = DeviceSecurityPostureEvaluator.MeetsRequired(
            effectiveDeviceSecurityPosture,
            options.RequiredDeviceSecurityPosture);

        DateTimeOffset readinessNow = DateTimeOffset.UtcNow;
        ReadinessStatus catalogReadiness = await repository.GetReadinessStatusAsync(
            readinessNow,
            cancellationToken);
        MediatorCapacitySnapshot capacity = await repository.GetCapacitySnapshotAsync(
            readinessNow,
            cancellationToken);
        double subscriptionUtilization = options.ConfiguredSubscriptionCapacity > 0
            ? (double)capacity.ActiveSubscriptions / options.ConfiguredSubscriptionCapacity
            : 0;
        double deviceUtilization = options.ConfiguredDeviceCapacity > 0
            ? (double)capacity.ActiveDevices / options.ConfiguredDeviceCapacity
            : 0;
        double utilization = Math.Max(subscriptionUtilization, deviceUtilization);
        string capacityState = options.ConfiguredSubscriptionCapacity <= 0
            && options.ConfiguredDeviceCapacity <= 0
                ? "unknown"
                : utilization >= 0.85
                    ? "saturated"
                    : utilization >= 0.70
                        ? "constrained"
                        : "healthy";
        bool ready = migrationsCurrent
            && deviceSecurityPostureSatisfied
            && catalogReadiness.HttpStatusCode == StatusCodes.Status200OK;
        string? reason = !migrationsCurrent
            ? migrationState.IsAhead ? "database_schema_ahead" : "migrations_pending"
            : !deviceSecurityPostureSatisfied
                ? "device_security_posture_not_satisfied"
                : catalogReadiness.Reason;
        bool identityHashConfigured = !string.IsNullOrWhiteSpace(options.DeviceIdentityHashKey);

        Dictionary<string, object?> body = new(StringComparer.Ordinal)
        {
            ["status"] = ready ? "ready" : "not_ready",
            ["reason"] = reason,
            ["deviceIssuanceVersion"] = 2,
            ["deviceFeedPolicyVersion"] = options.DefaultNewDeviceFeedPolicyVersion,
            ["deviceFeedBindingMode"] = options.DeviceFeedBindingMode,
            ["defaultNewDeviceFeedPolicy"] = options.DefaultNewDeviceFeedPolicy,
            ["requiredDeviceSecurityPosture"] = options.RequiredDeviceSecurityPosture,
            ["deviceSecurityPosture"] = effectiveDeviceSecurityPosture,
            ["effectiveDeviceSecurityPosture"] = effectiveDeviceSecurityPosture,
            ["deviceSecurityPostureSatisfied"] = deviceSecurityPostureSatisfied,
            ["identityHashConfigured"] = identityHashConfigured,
            ["deviceIdentityHashConfigured"] = identityHashConfigured,
            ["deviceIdentityHashKeyId"] = options.DeviceIdentityHashKeyId,
            ["requireDeviceIssuanceKey"] = options.RequireDeviceIssuanceKey,
            ["unifiedSubscriptionFeedEnabled"] = true,
            ["sharedSubscriptionLinksOnly"] = true,
            ["personalDeviceLinksEnabled"] = false,
            ["deviceLimitScope"] = "subscription_feed_tokens",
            ["catalogState"] = catalogReadiness.CatalogState,
            ["serverCount"] = catalogReadiness.ServerCount,
            ["dataAsOfUtc"] = catalogReadiness.DataAsOfUtc,
            ["snapshotCapturedAtUtc"] = capacity.CapturedAtUtc,
            ["activeSubscriptions"] = capacity.ActiveSubscriptions,
            ["activeDevices"] = capacity.ActiveDevices,
            ["configuredSubscriptionCapacity"] = options.ConfiguredSubscriptionCapacity > 0
                ? options.ConfiguredSubscriptionCapacity
                : null,
            ["configuredDeviceCapacity"] = options.ConfiguredDeviceCapacity > 0
                ? options.ConfiguredDeviceCapacity
                : null,
            ["capacityUtilizationPercent"] = Math.Round(utilization * 100, 2),
            ["capacityState"] = capacityState,
            ["migrationsApplied"] = migrations,
            ["expectedMigrations"] = expectedMigrations,
            ["migrationsCurrent"] = migrationsCurrent,
            ["isAhead"] = migrationState.IsAhead,
            ["missingRequiredVersions"] = migrationState.MissingRequiredVersions,
            ["unknownVersions"] = migrationState.UnknownVersions
        };
        return new MediatorReadinessSnapshot(ready, body);
    }
}

public static class RateLimitPartitionKey
{
    public static string ForRemoteAddress(HttpContext httpContext, string policy)
    {
        string remoteAddress = httpContext.Connection.RemoteIpAddress?.ToString() ?? "unknown";
        return $"{policy}:{remoteAddress}";
    }

    public static string ForHandoff(HttpContext httpContext)
    {
        return ForRemoteAddress(httpContext, "handoff");
    }

    public static string ForSubscription(HttpContext httpContext)
    {
        string? publicGuid = httpContext.Request.RouteValues["publicGuid"]?.ToString();
        string? devicePublicId = httpContext.Request.RouteValues["devicePublicId"]?.ToString();
        string? token = FirstNonEmpty(
            httpContext.Request.Query["token"].FirstOrDefault(),
            httpContext.Request.Query["sig"].FirstOrDefault());

        if (!string.IsNullOrWhiteSpace(publicGuid) && !string.IsNullOrWhiteSpace(token))
        {
            return $"subscription:{HashPartitionMaterial(publicGuid, devicePublicId, token)}";
        }

        return ForRemoteAddress(httpContext, "subscription-fallback");
    }

    private static string? FirstNonEmpty(params string?[] values)
    {
        return values.FirstOrDefault(value => !string.IsNullOrWhiteSpace(value));
    }

    private static string HashPartitionMaterial(params string?[] values)
    {
        string material = string.Join('\n', values.Select(value => value ?? string.Empty));
        byte[] hash = SHA256.HashData(Encoding.UTF8.GetBytes(material));
        return Convert.ToHexString(hash.AsSpan(0, 16));
    }
}


public static class VpnMediatorOptionsConfiguration
{
    public static void Bind(IConfiguration configuration, VpnMediatorOptions options)
    {
        ArgumentNullException.ThrowIfNull(configuration);
        ArgumentNullException.ThrowIfNull(options);

        options.ServerPresentationCountryBySourceNumber =
            new Dictionary<string, string>(StringComparer.Ordinal);
        configuration.GetSection(VpnMediatorOptions.SectionName).Bind(options);
    }
}

public sealed class VpnMediatorOptions
{
    public const string SectionName = "VpnMediator";

    public string ProductName { get; set; } = "Razaltush VPN";

    public string DatabasePath { get; set; } = "data/vpn-mediator-db.json";

    public string SqliteDatabasePath { get; set; } = "data/vpn-mediator.db";

    public int DefaultMaxDevices { get; set; } = 3;

    public int ConfiguredSubscriptionCapacity { get; set; }

    public int ConfiguredDeviceCapacity { get; set; }

    public int ReadinessTimeoutSeconds { get; set; } = 5;

    public string? PublicBaseUrl { get; set; }

    public string LinkSigningSecret { get; set; } = string.Empty;

    public string DeviceTokenHashKey { get; set; } = string.Empty;

    public string DeviceCredentialProtectionKeyId { get; set; } = "v1";

    public string? DeviceCredentialProtectionKey { get; set; }

    public string? PreviousDeviceCredentialProtectionKeyId { get; set; }

    public string? PreviousDeviceCredentialProtectionKey { get; set; }

    public string AdminToken { get; set; } = string.Empty;

    public string? PreviousAdminToken { get; set; }

    public DateTimeOffset? PreviousAdminTokenValidUntilUtc { get; set; }

    public string SourceEndpointProtectionKeyId { get; set; } = "v1";

    public string? SourceEndpointProtectionKey { get; set; }

    public string? PreviousSourceEndpointProtectionKeyId { get; set; }

    public string? PreviousSourceEndpointProtectionKey { get; set; }

    public string SupportTelegramBotUsername { get; set; } = "@RazaltushVpnBot";

    public bool AllowDevelopmentHttpSources { get; set; }

    public bool AllowLegacySubscriptionLinks { get; set; }

    public string DeviceFeedBindingMode { get; set; } = DeviceFeedBindingModes.Off;

    public string DefaultNewDeviceFeedPolicy { get; set; } = DeviceFeedPolicyModes.Legacy;

    public int DefaultNewDeviceFeedPolicyVersion { get; set; } =
        DeviceFeedPolicyVersions.PlatformHeuristic;

    public string RequiredDeviceSecurityPosture { get; set; } = DeviceSecurityPostures.None;

    public bool RequireDeviceIssuanceKey { get; set; }

    public string? DeviceObservationHashKey { get; set; }

    public string DeviceIdentityHashKeyId { get; set; } = "v1";

    public string? DeviceIdentityHashKey { get; set; }

    public string? PreviousDeviceIdentityHashKeyId { get; set; }

    public string? PreviousDeviceIdentityHashKey { get; set; }

    public int DeviceFeedTransferCooldownHours { get; set; } = 24;

    public int DeviceFeedConcurrentNetworkWindowMinutes { get; set; } = 10;

    public int DeviceFeedObservationRetentionDays { get; set; } = 14;

    public int PendingDeviceTokenTtlMinutes { get; set; } = 15;

    public int ServerCatalogRefreshIntervalSeconds { get; set; } = 300;

    public int ServerSourceTimeoutSeconds { get; set; } = 15;

    public int ServerSourceConnectTimeoutSeconds { get; set; } = 10;

    public int ServerCatalogMaxStaleHours { get; set; } = 24;

    public int ServerCatalogMaxResponseBytes { get; set; } = 1_000_000;

    public int ServerCatalogMaxServers { get; set; } = 500;

    public int ServerCatalogMaxLinksPerSourceRead { get; set; } = 5_000;

    public Dictionary<string, string> ServerPresentationCountryBySourceNumber { get; set; } =
        new(StringComparer.Ordinal);

    public int ServerSourceMaxRedirects { get; set; } = 2;

    public int ServerSourceMaxUriLength { get; set; } = 2048;

    public int UnifiedDeviceReservationMinutes { get; set; } = 15;

    public int CriticalWorkerConsecutiveFailureLimit { get; set; } = 5;
}

public sealed class VpnMediatorOptionsValidator : IValidateOptions<VpnMediatorOptions>
{
    private static readonly string[] PlaceholderFragments =
    [
        "change-me",
        "replace-with",
        "placeholder",
        "<",
        ">",
        "your_",
        "your-",
        "test-token"
    ];

    private readonly IWebHostEnvironment _environment;

    public VpnMediatorOptionsValidator(IWebHostEnvironment environment)
    {
        _environment = environment;
    }

    public ValidateOptionsResult Validate(string? name, VpnMediatorOptions options)
    {
        List<string> failures = [];
        bool isDevelopment = _environment.IsDevelopment();

        string? productName = TextSanitizer.NullIfWhiteSpace(options.ProductName);
        if (productName is null || productName.Length < 2)
        {
            failures.Add("ProductName must contain at least 2 characters.");
        }
        else if (productName.Length > 32)
        {
            failures.Add("ProductName must not exceed 32 characters.");
        }

        if (!string.Equals(
            PublicBrandingPolicy.NormalizeTelegramUsername(
                options.SupportTelegramBotUsername),
            options.SupportTelegramBotUsername.Trim(),
            StringComparison.Ordinal))
        {
            failures.Add(
                "SupportTelegramBotUsername must be the canonical @RazaltushVpnBot value.");
        }

        if (!isDevelopment)
        {
            ValidateProductionPublicBaseUrl(options, failures);
            RequireStrongSecret(options.AdminToken, "AdminToken", failures);
            if (!string.IsNullOrWhiteSpace(options.PreviousAdminToken))
            {
                RequireStrongSecret(options.PreviousAdminToken, "PreviousAdminToken", failures);
                if (options.PreviousAdminTokenValidUntilUtc is null
                    || options.PreviousAdminTokenValidUntilUtc <= DateTimeOffset.UtcNow)
                {
                    failures.Add("PreviousAdminTokenValidUntilUtc must be a future timestamp when a previous token is configured.");
                }
            }
            else if (options.PreviousAdminTokenValidUntilUtc is not null)
            {
                failures.Add("PreviousAdminTokenValidUntilUtc requires PreviousAdminToken.");
            }
            RequireStrongSecret(options.DeviceTokenHashKey, "DeviceTokenHashKey", failures);
            ValidateCredentialProtectionKeys(options, failures);

            if (options.AllowLegacySubscriptionLinks)
            {
                RequireStrongSecret(options.LinkSigningSecret, "LinkSigningSecret", failures);
            }

            if (options.AllowDevelopmentHttpSources)
            {
                failures.Add("AllowDevelopmentHttpSources must be false outside Development.");
            }

            ValidateEndpointProtectionKey(options.SourceEndpointProtectionKey, failures);
            ValidateEndpointProtectionKeyRing(options, failures);
        }

        ValidateBoundedPositive(options.DefaultMaxDevices, "DefaultMaxDevices", 1, 100, failures);
        if (options.ConfiguredSubscriptionCapacity < 0)
        {
            failures.Add("ConfiguredSubscriptionCapacity must be zero or positive.");
        }
        if (options.ConfiguredDeviceCapacity < 0)
        {
            failures.Add("ConfiguredDeviceCapacity must be zero or positive.");
        }
        ValidateBoundedPositive(
            options.ReadinessTimeoutSeconds,
            "ReadinessTimeoutSeconds",
            1,
            30,
            failures);
        if (!DeviceFeedBindingModes.IsSupported(options.DeviceFeedBindingMode))
        {
            failures.Add("DeviceFeedBindingMode must be off, observe, or enforce.");
        }
        if (!DeviceFeedPolicyModes.IsSupported(options.DefaultNewDeviceFeedPolicy))
        {
            failures.Add("DefaultNewDeviceFeedPolicy must be legacy, observe, or enforce.");
        }
        if (!DeviceFeedPolicyVersions.IsSupported(options.DefaultNewDeviceFeedPolicyVersion))
        {
            failures.Add("DefaultNewDeviceFeedPolicyVersion must be 1 or 2.");
        }
        if (!DeviceSecurityPostures.IsSupported(options.RequiredDeviceSecurityPosture))
        {
            failures.Add(
                "RequiredDeviceSecurityPosture must be none, observe, or feed_enforced.");
        }
        ValidateDeviceIdentityConfiguration(options, isDevelopment, failures);
        if (!isDevelopment
            && !string.Equals(
                options.DeviceFeedBindingMode,
                DeviceFeedBindingModes.Off,
                StringComparison.Ordinal))
        {
            RequireStrongSecret(
                options.DeviceObservationHashKey ?? string.Empty,
                "DeviceObservationHashKey",
                failures);
        }
        ValidateBoundedPositive(options.DeviceFeedTransferCooldownHours, "DeviceFeedTransferCooldownHours", 1, 720, failures);
        ValidateBoundedPositive(options.DeviceFeedConcurrentNetworkWindowMinutes, "DeviceFeedConcurrentNetworkWindowMinutes", 1, 1440, failures);
        ValidateBoundedPositive(options.DeviceFeedObservationRetentionDays, "DeviceFeedObservationRetentionDays", 1, 90, failures);
        ValidateBoundedPositive(options.PendingDeviceTokenTtlMinutes, "PendingDeviceTokenTtlMinutes", 1, 1440, failures);
        ValidateBoundedPositive(options.UnifiedDeviceReservationMinutes, "UnifiedDeviceReservationMinutes", 1, 1440, failures);
        if (string.IsNullOrWhiteSpace(options.DeviceIdentityHashKey)
            || string.IsNullOrWhiteSpace(options.DeviceIdentityHashKeyId))
        {
            failures.Add("DeviceIdentityHashKey and DeviceIdentityHashKeyId are required for the subscription feed.");
        }
        ValidateBoundedPositive(options.ServerCatalogRefreshIntervalSeconds, "ServerCatalogRefreshIntervalSeconds", 10, 86_400, failures);
        if (options.ServerCatalogMaxLinksPerSourceRead < options.ServerCatalogMaxServers)
        {
            failures.Add(
                "ServerCatalogMaxLinksPerSourceRead cannot be lower than ServerCatalogMaxServers.");
        }
        ValidateBoundedPositive(options.ServerSourceTimeoutSeconds, "ServerSourceTimeoutSeconds", 1, 300, failures);
        ValidateBoundedPositive(options.ServerSourceConnectTimeoutSeconds, "ServerSourceConnectTimeoutSeconds", 1, 60, failures);
        ValidateBoundedPositive(options.ServerCatalogMaxStaleHours, "ServerCatalogMaxStaleHours", 1, 720, failures);
        ValidateBoundedPositive(options.ServerCatalogMaxResponseBytes, "ServerCatalogMaxResponseBytes", 1024, 20_000_000, failures);
        ValidateBoundedPositive(options.ServerCatalogMaxServers, "ServerCatalogMaxServers", 1, 10_000, failures);
        ValidateBoundedPositive(options.ServerCatalogMaxLinksPerSourceRead, "ServerCatalogMaxLinksPerSourceRead", 1, 100_000, failures);
        ValidateServerPresentationCountries(
            options.ServerPresentationCountryBySourceNumber,
            failures);
        ValidateBoundedPositive(options.ServerSourceMaxRedirects, "ServerSourceMaxRedirects", 0, 10, failures);
        ValidateBoundedPositive(options.ServerSourceMaxUriLength, "ServerSourceMaxUriLength", 128, 8192, failures);
        ValidateBoundedPositive(options.CriticalWorkerConsecutiveFailureLimit, "CriticalWorkerConsecutiveFailureLimit", 1, 100, failures);

        if (failures.Count > 0)
        {
            return ValidateOptionsResult.Fail(failures);
        }

        return ValidateOptionsResult.Success;
    }

    private static void ValidateServerPresentationCountries(
        IReadOnlyDictionary<string, string> countries,
        List<string> failures)
    {
        foreach ((string sourceNumber, string country) in countries)
        {
            if (!int.TryParse(
                    sourceNumber,
                    NumberStyles.None,
                    CultureInfo.InvariantCulture,
                    out int parsedSourceNumber)
                || parsedSourceNumber <= 0)
            {
                failures.Add(
                    "ServerPresentationCountryBySourceNumber keys must be positive integer source numbers.");
                continue;
            }

            string normalizedCountry = country?.Trim() ?? string.Empty;
            if (normalizedCountry.Length is < 2 or > 64
                || normalizedCountry.Contains('|')
                || normalizedCountry.Any(char.IsControl))
            {
                failures.Add(
                    "ServerPresentationCountryBySourceNumber values must contain 2 to 64 visible characters without '|'.");
            }
        }
    }


    private static void ValidateDeviceIdentityConfiguration(
        VpnMediatorOptions options,
        bool isDevelopment,
        List<string> failures)
    {
        bool hasPreviousId = !string.IsNullOrWhiteSpace(
            options.PreviousDeviceIdentityHashKeyId);
        bool hasPreviousKey = !string.IsNullOrWhiteSpace(
            options.PreviousDeviceIdentityHashKey);
        if (hasPreviousId != hasPreviousKey)
        {
            failures.Add(
                "PreviousDeviceIdentityHashKeyId and PreviousDeviceIdentityHashKey must be configured together.");
        }

        if (string.IsNullOrWhiteSpace(options.DeviceIdentityHashKeyId)
            || options.DeviceIdentityHashKeyId.Length > 32)
        {
            failures.Add(
                "DeviceIdentityHashKeyId must contain from 1 to 32 characters.");
        }

        bool versionTwoCanBeUsed = options.DefaultNewDeviceFeedPolicyVersion
                == DeviceFeedPolicyVersions.HwidIdentity
            && !string.Equals(
                options.DefaultNewDeviceFeedPolicy,
                DeviceFeedPolicyModes.Legacy,
                StringComparison.Ordinal)
            && !string.Equals(
                options.DeviceFeedBindingMode,
                DeviceFeedBindingModes.Off,
                StringComparison.Ordinal);
        bool postureRequiresIdentity = options.RequiredDeviceSecurityPosture
            is DeviceSecurityPostures.Observe
                or DeviceSecurityPostures.FeedEnforced;

        if (!isDevelopment && (versionTwoCanBeUsed || postureRequiresIdentity))
        {
            RequireStrongSecret(
                options.DeviceIdentityHashKey,
                "DeviceIdentityHashKey",
                failures);
        }
        if (!isDevelopment && hasPreviousKey)
        {
            RequireStrongSecret(
                options.PreviousDeviceIdentityHashKey,
                "PreviousDeviceIdentityHashKey",
                failures);
        }

        if (string.Equals(
            options.RequiredDeviceSecurityPosture,
            DeviceSecurityPostures.Observe,
            StringComparison.Ordinal))
        {
            if (string.Equals(
                options.DeviceFeedBindingMode,
                DeviceFeedBindingModes.Off,
                StringComparison.Ordinal))
            {
                failures.Add(
                    "RequiredDeviceSecurityPosture=observe requires DeviceFeedBindingMode=observe or enforce.");
            }
            if (options.DefaultNewDeviceFeedPolicyVersion
                != DeviceFeedPolicyVersions.HwidIdentity)
            {
                failures.Add(
                    "RequiredDeviceSecurityPosture=observe requires DefaultNewDeviceFeedPolicyVersion=2.");
            }
        }

        bool feedEnforced = options.RequiredDeviceSecurityPosture
            is DeviceSecurityPostures.FeedEnforced;
        if (feedEnforced)
        {
            if (!string.Equals(
                options.DeviceFeedBindingMode,
                DeviceFeedBindingModes.Enforce,
                StringComparison.Ordinal))
            {
                failures.Add(
                    "Feed-enforced security posture requires DeviceFeedBindingMode=enforce.");
            }
            if (!string.Equals(
                options.DefaultNewDeviceFeedPolicy,
                DeviceFeedPolicyModes.Enforce,
                StringComparison.Ordinal))
            {
                failures.Add(
                    "Feed-enforced security posture requires DefaultNewDeviceFeedPolicy=enforce.");
            }
            if (options.DefaultNewDeviceFeedPolicyVersion
                != DeviceFeedPolicyVersions.HwidIdentity)
            {
                failures.Add(
                    "Feed-enforced security posture requires DefaultNewDeviceFeedPolicyVersion=2.");
            }
            if (!options.RequireDeviceIssuanceKey)
            {
                failures.Add(
                    "Feed-enforced security posture requires RequireDeviceIssuanceKey=true.");
            }
        }

    }

    private static void ValidateCredentialProtectionKeys(
        VpnMediatorOptions options,
        List<string> failures)
    {
        if (string.IsNullOrWhiteSpace(options.DeviceCredentialProtectionKeyId)
            || options.DeviceCredentialProtectionKeyId.Length > 32)
        {
            failures.Add("DeviceCredentialProtectionKeyId must contain from 1 to 32 characters.");
        }

        ValidateBase64Key(
            options.DeviceCredentialProtectionKey,
            "DeviceCredentialProtectionKey",
            failures);

        bool hasPreviousId = !string.IsNullOrWhiteSpace(options.PreviousDeviceCredentialProtectionKeyId);
        bool hasPreviousKey = !string.IsNullOrWhiteSpace(options.PreviousDeviceCredentialProtectionKey);

        if (hasPreviousId != hasPreviousKey)
        {
            failures.Add(
                "PreviousDeviceCredentialProtectionKeyId and PreviousDeviceCredentialProtectionKey must be configured together.");
        }

        if (hasPreviousKey)
        {
            ValidateBase64Key(
                options.PreviousDeviceCredentialProtectionKey,
                "PreviousDeviceCredentialProtectionKey",
                failures);
        }
    }

    private static void ValidateBase64Key(
        string? value,
        string name,
        List<string> failures)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            failures.Add($"{name} is required outside Development.");
            return;
        }

        try
        {
            if (Convert.FromBase64String(value).Length != 32)
            {
                failures.Add($"{name} must be a base64 encoded 32-byte key.");
            }
        }
        catch (FormatException)
        {
            failures.Add($"{name} must be a base64 encoded 32-byte key.");
        }
    }

    private static void ValidateEndpointProtectionKeyRing(
        VpnMediatorOptions options,
        List<string> failures)
    {
        if (string.IsNullOrWhiteSpace(options.SourceEndpointProtectionKeyId)
            || options.SourceEndpointProtectionKeyId.Length > 32)
        {
            failures.Add("SourceEndpointProtectionKeyId must contain from 1 to 32 characters.");
        }

        bool hasPreviousId = !string.IsNullOrWhiteSpace(
            options.PreviousSourceEndpointProtectionKeyId);
        bool hasPreviousKey = !string.IsNullOrWhiteSpace(
            options.PreviousSourceEndpointProtectionKey);
        if (hasPreviousId != hasPreviousKey)
        {
            failures.Add(
                "PreviousSourceEndpointProtectionKeyId and PreviousSourceEndpointProtectionKey must be configured together.");
        }
        if (hasPreviousKey)
        {
            ValidateEndpointProtectionKey(options.PreviousSourceEndpointProtectionKey, failures);
        }
    }

    private static void ValidateProductionPublicBaseUrl(
        VpnMediatorOptions options,
        List<string> failures)
    {
        if (!Uri.TryCreate(options.PublicBaseUrl, UriKind.Absolute, out Uri? uri))
        {
            failures.Add("PublicBaseUrl must be an absolute HTTPS URL outside Development.");
            return;
        }

        if (uri.Scheme != Uri.UriSchemeHttps)
        {
            failures.Add("PublicBaseUrl must use HTTPS outside Development.");
        }

        if (!string.IsNullOrEmpty(uri.Query) || !string.IsNullOrEmpty(uri.Fragment))
        {
            failures.Add("PublicBaseUrl must not contain a query string or fragment.");
        }
    }

    private static void RequireStrongSecret(string? value, string optionName, List<string> failures)
    {
        string? normalized = TextSanitizer.NullIfWhiteSpace(value);

        if (normalized is null || normalized.Length < 32)
        {
            failures.Add($"{optionName} must contain at least 32 random characters.");
            return;
        }

        if (PlaceholderFragments.Any(fragment => normalized.Contains(fragment, StringComparison.OrdinalIgnoreCase)))
        {
            failures.Add($"{optionName} contains a tracked placeholder value.");
        }

        if (normalized.Distinct().Count() < 12)
        {
            failures.Add($"{optionName} does not look random enough.");
        }
    }

    private static void ValidateEndpointProtectionKey(string? value, List<string> failures)
    {
        string? normalized = TextSanitizer.NullIfWhiteSpace(value);

        if (normalized is null)
        {
            failures.Add("SourceEndpointProtectionKey is required outside Development.");
            return;
        }

        try
        {
            if (Convert.FromBase64String(normalized).Length != 32)
            {
                failures.Add("SourceEndpointProtectionKey must decode to exactly 32 bytes.");
            }
        }
        catch (FormatException)
        {
            failures.Add("SourceEndpointProtectionKey must be valid Base64.");
        }
    }

    private static void ValidateBoundedPositive(
        int value,
        string optionName,
        int minimum,
        int maximum,
        List<string> failures)
    {
        if (value < minimum || value > maximum)
        {
            failures.Add($"{optionName} must be between {minimum} and {maximum}.");
        }
    }
}


public sealed record CreateSubscriptionRequest(
    string ExternalRequestId,
    string? CustomerReference,
    string? Note,
    EntitlementUpdateRequest Entitlement);

public sealed record UpdateDeviceLimitRequest(int MaxDevices);

public sealed record SubscriptionDetailsResponse(
    Guid PublicGuid,
    string? SubscriptionUrl,
    int MaxDevices,
    bool IsActive,
    DateTimeOffset CreatedAtUtc,
    DateTimeOffset? ExpiresAtUtc,
    string? CustomerName,
    string? Note,
    int ActiveDeviceCount,
    IReadOnlyList<DeviceBindingResponse> Devices);

public sealed record DeviceBindingResponse(
    Guid DeviceBindingId,
    string DeviceLabel,
    string DeviceHashPrefix,
    bool IsActive,
    DateTimeOffset FirstSeenAtUtc,
    DateTimeOffset LastSeenAtUtc,
    long AccessCount,
    string IdentitySource);

public sealed class SubscriptionRecord
{
    public Guid PublicGuid { get; set; }

    public string UpstreamSubscriptionUrl { get; set; } = string.Empty;

    public int MaxDevices { get; set; }

    public bool IsActive { get; set; }

    public string? CustomerName { get; set; }

    public string? Note { get; set; }

    public DateTimeOffset CreatedAtUtc { get; set; }

    public DateTimeOffset? ExpiresAtUtc { get; set; }

    public List<DeviceBindingRecord> Devices { get; set; } = [];
}

public sealed class DeviceBindingRecord
{
    public Guid DeviceBindingId { get; set; }

    public string DeviceHash { get; set; } = string.Empty;

    public string DeviceLabel { get; set; } = string.Empty;

    public string IdentitySource { get; set; } = string.Empty;

    public bool IsActive { get; set; }

    public DateTimeOffset FirstSeenAtUtc { get; set; }

    public DateTimeOffset LastSeenAtUtc { get; set; }

    public long AccessCount { get; set; }
}

public sealed record DeviceMetadata(
    string? DeviceType,
    string? Platform,
    string? DetectedModel,
    string? DetectionSource);

public static class DeviceMetadataDetector
{
    public static DeviceMetadata Detect(string? userAgent)
    {
        if (string.IsNullOrWhiteSpace(userAgent))
        {
            return new DeviceMetadata(null, null, null, null);
        }

        string normalized = userAgent.Trim();

        if (normalized.Length > 512)
        {
            normalized = normalized[..512];
        }

        string lower = normalized.ToLowerInvariant();

        if (lower.Contains("android", StringComparison.Ordinal))
        {
            return new DeviceMetadata(
                "phone",
                "android",
                ExtractAndroidModel(normalized),
                "user_agent_normalized");
        }

        if (lower.Contains("iphone", StringComparison.Ordinal))
        {
            return new DeviceMetadata("phone", "ios", "iPhone", "user_agent_normalized");
        }

        if (lower.Contains("ipad", StringComparison.Ordinal))
        {
            return new DeviceMetadata("tablet", "ios", "iPad", "user_agent_normalized");
        }

        if (lower.Contains("windows", StringComparison.Ordinal))
        {
            return new DeviceMetadata("computer", "windows", "Windows-компьютер", "user_agent_normalized");
        }

        if (lower.Contains("mac os", StringComparison.Ordinal)
            || lower.Contains("macintosh", StringComparison.Ordinal))
        {
            return new DeviceMetadata("computer", "macos", "Mac", "user_agent_normalized");
        }

        if (lower.Contains("linux", StringComparison.Ordinal))
        {
            return new DeviceMetadata("computer", "linux", "Linux-компьютер", "user_agent_normalized");
        }

        return new DeviceMetadata(null, null, null, null);
    }

    private static string? ExtractAndroidModel(string userAgent)
    {
        int start = userAgent.IndexOf("Android", StringComparison.OrdinalIgnoreCase);

        if (start < 0)
        {
            return "Android-устройство";
        }

        int semicolon = userAgent.IndexOf(';', start);

        if (semicolon < 0)
        {
            return "Android-устройство";
        }

        int end = userAgent.IndexOf(')', semicolon);

        if (end < 0 || end <= semicolon + 1)
        {
            return "Android-устройство";
        }

        string candidate = userAgent[(semicolon + 1)..end].Trim();

        if (candidate.Length is < 3 or > 64
            || candidate.Contains("Build/", StringComparison.OrdinalIgnoreCase))
        {
            return "Android-устройство";
        }

        return candidate;
    }
}

public sealed class AuditLogRecord
{
    public DateTimeOffset CreatedAtUtc { get; set; }

    public string EventType { get; set; } = string.Empty;

    public Guid? PublicGuid { get; set; }

    public Guid? DeviceBindingId { get; set; }

    public string Message { get; set; } = string.Empty;
}

public sealed class VpnMediatorDatabase
{
    public List<SubscriptionRecord> Subscriptions { get; set; } = [];

    public List<AuditLogRecord> AuditLog { get; set; } = [];
}

public interface ISubscriptionRepository
{
    Task CreateSubscriptionAsync(
        SubscriptionRecord subscription,
        DateTimeOffset now,
        CancellationToken cancellationToken);

    Task<IReadOnlyList<SubscriptionRecord>> GetSubscriptionsAsync(CancellationToken cancellationToken);

    Task<SubscriptionRecord?> GetSubscriptionAsync(
        Guid publicGuid,
        CancellationToken cancellationToken);

    Task<DeviceRegistrationResult> RegisterDeviceAccessAsync(
        Guid publicGuid,
        DeviceIdentity deviceIdentity,
        DateTimeOffset now,
        CancellationToken cancellationToken);

    Task<bool> UnbindDeviceAsync(
        Guid publicGuid,
        Guid deviceBindingId,
        DateTimeOffset now,
        CancellationToken cancellationToken);

    Task<UnbindAllDevicesResult> UnbindAllDevicesAsync(
        Guid publicGuid,
        DateTimeOffset now,
        CancellationToken cancellationToken);

    Task<bool> UpdateDeviceLimitAsync(
        Guid publicGuid,
        int maxDevices,
        DateTimeOffset now,
        CancellationToken cancellationToken);

    Task<bool> SetSubscriptionActiveAsync(
        Guid publicGuid,
        bool isActive,
        DateTimeOffset now,
        CancellationToken cancellationToken);
}

public sealed record UnbindAllDevicesResult(
    bool SubscriptionFound,
    int UnboundDevices);

public sealed class JsonFileSubscriptionRepository : ISubscriptionRepository
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true
    };

    private readonly string _databasePath;
    private readonly SemaphoreSlim _lock = new(1, 1);
    private VpnMediatorDatabase? _database;

    public JsonFileSubscriptionRepository(IOptions<VpnMediatorOptions> options)
    {
        _databasePath = options.Value.DatabasePath;
    }

    public async Task CreateSubscriptionAsync(
        SubscriptionRecord subscription,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await _lock.WaitAsync(cancellationToken);

        try
        {
            VpnMediatorDatabase database = await LoadDatabaseUnsafeAsync(cancellationToken);

            if (database.Subscriptions.Any(x => x.PublicGuid == subscription.PublicGuid))
            {
                throw new InvalidOperationException("Subscription GUID collision.");
            }

            database.Subscriptions.Add(Clone(subscription));
            AddAudit(database, now, "subscription.created", subscription.PublicGuid, null, "Subscription was created.");

            await SaveDatabaseUnsafeAsync(database, cancellationToken);
        }
        finally
        {
            _lock.Release();
        }
    }

    public async Task<IReadOnlyList<SubscriptionRecord>> GetSubscriptionsAsync(CancellationToken cancellationToken)
    {
        await _lock.WaitAsync(cancellationToken);

        try
        {
            VpnMediatorDatabase database = await LoadDatabaseUnsafeAsync(cancellationToken);

            return database.Subscriptions
                .Select(Clone)
                .OrderByDescending(x => x.CreatedAtUtc)
                .ToArray();
        }
        finally
        {
            _lock.Release();
        }
    }

    public async Task<SubscriptionRecord?> GetSubscriptionAsync(
        Guid publicGuid,
        CancellationToken cancellationToken)
    {
        await _lock.WaitAsync(cancellationToken);

        try
        {
            VpnMediatorDatabase database = await LoadDatabaseUnsafeAsync(cancellationToken);
            SubscriptionRecord? subscription = database.Subscriptions.FirstOrDefault(x => x.PublicGuid == publicGuid);

            return subscription is null ? null : Clone(subscription);
        }
        finally
        {
            _lock.Release();
        }
    }

    public async Task<DeviceRegistrationResult> RegisterDeviceAccessAsync(
        Guid publicGuid,
        DeviceIdentity deviceIdentity,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await _lock.WaitAsync(cancellationToken);

        try
        {
            VpnMediatorDatabase database = await LoadDatabaseUnsafeAsync(cancellationToken);
            SubscriptionRecord? subscription = database.Subscriptions.FirstOrDefault(x => x.PublicGuid == publicGuid);

            if (subscription is null)
            {
                return new DeviceRegistrationResult(DeviceRegistrationStatus.SubscriptionNotFound, null, 0, 0);
            }

            DeviceBindingRecord? existingDevice = subscription.Devices.FirstOrDefault(
                x => x.DeviceHash == deviceIdentity.DeviceHash);

            if (existingDevice is not null)
            {
                if (!existingDevice.IsActive)
                {
                    int activeDeviceCount = subscription.Devices.Count(x => x.IsActive);

                    if (activeDeviceCount >= subscription.MaxDevices)
                    {
                        return new DeviceRegistrationResult(DeviceRegistrationStatus.DeviceLimitExceeded, null, activeDeviceCount, subscription.MaxDevices);
                    }

                    existingDevice.IsActive = true;
                    AddAudit(database, now, "device.rebound", publicGuid, existingDevice.DeviceBindingId, "Device was rebound.");
                }

                existingDevice.LastSeenAtUtc = now;
                existingDevice.AccessCount++;

                await SaveDatabaseUnsafeAsync(database, cancellationToken);

                return new DeviceRegistrationResult(
                    DeviceRegistrationStatus.AllowedExistingDevice,
                    Clone(existingDevice),
                    subscription.Devices.Count(x => x.IsActive),
                    subscription.MaxDevices);
            }

            int activeDevices = subscription.Devices.Count(x => x.IsActive);

            if (activeDevices >= subscription.MaxDevices)
            {
                AddAudit(database, now, "device.rejected.limit", publicGuid, null, "Device was rejected because the device limit was exceeded.");
                await SaveDatabaseUnsafeAsync(database, cancellationToken);

                return new DeviceRegistrationResult(DeviceRegistrationStatus.DeviceLimitExceeded, null, activeDevices, subscription.MaxDevices);
            }

            DeviceBindingRecord newDevice = new()
            {
                DeviceBindingId = Guid.NewGuid(),
                DeviceHash = deviceIdentity.DeviceHash,
                DeviceLabel = deviceIdentity.DeviceLabel,
                IdentitySource = deviceIdentity.IdentitySource,
                IsActive = true,
                FirstSeenAtUtc = now,
                LastSeenAtUtc = now,
                AccessCount = 1
            };

            subscription.Devices.Add(newDevice);
            AddAudit(database, now, "device.bound", publicGuid, newDevice.DeviceBindingId, "Device was bound.");

            await SaveDatabaseUnsafeAsync(database, cancellationToken);

            return new DeviceRegistrationResult(
                DeviceRegistrationStatus.AllowedNewDevice,
                Clone(newDevice),
                subscription.Devices.Count(x => x.IsActive),
                subscription.MaxDevices);
        }
        finally
        {
            _lock.Release();
        }
    }

    public async Task<bool> UnbindDeviceAsync(
        Guid publicGuid,
        Guid deviceBindingId,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await _lock.WaitAsync(cancellationToken);

        try
        {
            VpnMediatorDatabase database = await LoadDatabaseUnsafeAsync(cancellationToken);
            SubscriptionRecord? subscription = database.Subscriptions.FirstOrDefault(x => x.PublicGuid == publicGuid);

            if (subscription is null)
            {
                return false;
            }

            DeviceBindingRecord? device = subscription.Devices.FirstOrDefault(x => x.DeviceBindingId == deviceBindingId);

            if (device is null)
            {
                return false;
            }

            device.IsActive = false;
            device.LastSeenAtUtc = now;

            AddAudit(database, now, "device.unbound", publicGuid, deviceBindingId, "Device was unbound.");
            await SaveDatabaseUnsafeAsync(database, cancellationToken);

            return true;
        }
        finally
        {
            _lock.Release();
        }
    }

    public async Task<UnbindAllDevicesResult> UnbindAllDevicesAsync(
        Guid publicGuid,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await _lock.WaitAsync(cancellationToken);

        try
        {
            VpnMediatorDatabase database = await LoadDatabaseUnsafeAsync(cancellationToken);
            SubscriptionRecord? subscription = database.Subscriptions.FirstOrDefault(x => x.PublicGuid == publicGuid);

            if (subscription is null)
            {
                return new UnbindAllDevicesResult(false, 0);
            }

            int unboundDevices = 0;

            foreach (DeviceBindingRecord device in subscription.Devices.Where(x => x.IsActive))
            {
                device.IsActive = false;
                device.LastSeenAtUtc = now;
                unboundDevices++;
            }

            AddAudit(database, now, "devices.unbound_all", publicGuid, null, $"{unboundDevices} devices were unbound.");
            await SaveDatabaseUnsafeAsync(database, cancellationToken);

            return new UnbindAllDevicesResult(true, unboundDevices);
        }
        finally
        {
            _lock.Release();
        }
    }

    public async Task<bool> UpdateDeviceLimitAsync(
        Guid publicGuid,
        int maxDevices,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await _lock.WaitAsync(cancellationToken);

        try
        {
            VpnMediatorDatabase database = await LoadDatabaseUnsafeAsync(cancellationToken);
            SubscriptionRecord? subscription = database.Subscriptions.FirstOrDefault(x => x.PublicGuid == publicGuid);

            if (subscription is null)
            {
                return false;
            }

            subscription.MaxDevices = maxDevices;
            AddAudit(database, now, "subscription.limit.updated", publicGuid, null, $"Device limit was updated to {maxDevices}.");

            await SaveDatabaseUnsafeAsync(database, cancellationToken);

            return true;
        }
        finally
        {
            _lock.Release();
        }
    }

    public async Task<bool> SetSubscriptionActiveAsync(
        Guid publicGuid,
        bool isActive,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        await _lock.WaitAsync(cancellationToken);

        try
        {
            VpnMediatorDatabase database = await LoadDatabaseUnsafeAsync(cancellationToken);
            SubscriptionRecord? subscription = database.Subscriptions.FirstOrDefault(x => x.PublicGuid == publicGuid);

            if (subscription is null)
            {
                return false;
            }

            subscription.IsActive = isActive;

            AddAudit(
                database,
                now,
                isActive ? "subscription.enabled" : "subscription.disabled",
                publicGuid,
                null,
                isActive ? "Subscription was enabled." : "Subscription was disabled.");

            await SaveDatabaseUnsafeAsync(database, cancellationToken);

            return true;
        }
        finally
        {
            _lock.Release();
        }
    }

    private async Task<VpnMediatorDatabase> LoadDatabaseUnsafeAsync(CancellationToken cancellationToken)
    {
        if (_database is not null)
        {
            return _database;
        }

        string? directory = Path.GetDirectoryName(_databasePath);

        if (!string.IsNullOrWhiteSpace(directory))
        {
            Directory.CreateDirectory(directory);
        }

        if (!File.Exists(_databasePath))
        {
            _database = new VpnMediatorDatabase();
            await SaveDatabaseUnsafeAsync(_database, cancellationToken);
            return _database;
        }

        string json = await File.ReadAllTextAsync(_databasePath, cancellationToken);

        _database = JsonSerializer.Deserialize<VpnMediatorDatabase>(json, JsonOptions)
            ?? new VpnMediatorDatabase();

        return _database;
    }

    private async Task SaveDatabaseUnsafeAsync(
        VpnMediatorDatabase database,
        CancellationToken cancellationToken)
    {
        string? directory = Path.GetDirectoryName(_databasePath);

        if (!string.IsNullOrWhiteSpace(directory))
        {
            Directory.CreateDirectory(directory);
        }

        string tempPath = $"{_databasePath}.tmp";
        string json = JsonSerializer.Serialize(database, JsonOptions);

        await File.WriteAllTextAsync(tempPath, json, cancellationToken);
        File.Move(tempPath, _databasePath, overwrite: true);

        _database = database;
    }

    private static void AddAudit(
        VpnMediatorDatabase database,
        DateTimeOffset now,
        string eventType,
        Guid? publicGuid,
        Guid? deviceBindingId,
        string message)
    {
        database.AuditLog.Add(new AuditLogRecord
        {
            CreatedAtUtc = now,
            EventType = eventType,
            PublicGuid = publicGuid,
            DeviceBindingId = deviceBindingId,
            Message = message
        });
    }

    private static SubscriptionRecord Clone(SubscriptionRecord source)
    {
        return new SubscriptionRecord
        {
            PublicGuid = source.PublicGuid,
            UpstreamSubscriptionUrl = source.UpstreamSubscriptionUrl,
            MaxDevices = source.MaxDevices,
            IsActive = source.IsActive,
            CustomerName = source.CustomerName,
            Note = source.Note,
            CreatedAtUtc = source.CreatedAtUtc,
            ExpiresAtUtc = source.ExpiresAtUtc,
            Devices = source.Devices.Select(Clone).ToList()
        };
    }

    private static DeviceBindingRecord Clone(DeviceBindingRecord source)
    {
        return new DeviceBindingRecord
        {
            DeviceBindingId = source.DeviceBindingId,
            DeviceHash = source.DeviceHash,
            DeviceLabel = source.DeviceLabel,
            IdentitySource = source.IdentitySource,
            IsActive = source.IsActive,
            FirstSeenAtUtc = source.FirstSeenAtUtc,
            LastSeenAtUtc = source.LastSeenAtUtc,
            AccessCount = source.AccessCount
        };
    }
}

public sealed record DeviceIdentity(
    string DeviceHash,
    string DeviceLabel,
    string IdentitySource);

public sealed record DeviceRegistrationResult(
    DeviceRegistrationStatus Status,
    DeviceBindingRecord? Device,
    int ActiveDeviceCount,
    int MaxDevices)
{
    public int AvailableDeviceSlots => Math.Max(0, MaxDevices - ActiveDeviceCount);
}


public enum DeviceRegistrationStatus
{
    SubscriptionNotFound,
    DeviceLimitExceeded,
    AllowedExistingDevice,
    AllowedNewDevice
}

public interface ILinkSigner
{
    string CreateSignature(Guid publicGuid);

    bool IsValid(Guid publicGuid, string? signature);
}

public sealed class HmacLinkSigner : ILinkSigner
{
    private readonly byte[] _secretBytes;
    private readonly bool _enabled;

    public HmacLinkSigner(IOptions<VpnMediatorOptions> options)
    {
        string secret = options.Value.LinkSigningSecret;
        _enabled = options.Value.AllowLegacySubscriptionLinks;

        if (!_enabled)
        {
            _secretBytes = [];
            return;
        }

        if (string.IsNullOrWhiteSpace(secret) || secret.Length < 32)
        {
            throw new InvalidOperationException("VpnMediator:LinkSigningSecret must contain at least 32 characters.");
        }

        _secretBytes = Encoding.UTF8.GetBytes(secret);
    }

    public string CreateSignature(Guid publicGuid)
    {
        if (!_enabled)
        {
            throw new InvalidOperationException("Legacy subscription links are disabled.");
        }

        string payload = CreatePayload(publicGuid);

        using HMACSHA256 hmac = new(_secretBytes);
        byte[] hash = hmac.ComputeHash(Encoding.UTF8.GetBytes(payload));

        return Base64Url.Encode(hash);
    }

    public bool IsValid(Guid publicGuid, string? signature)
    {
        if (!_enabled)
        {
            return false;
        }

        if (string.IsNullOrWhiteSpace(signature))
        {
            return false;
        }

        string expectedSignature = CreateSignature(publicGuid);

        byte[] expectedBytes = Encoding.UTF8.GetBytes(expectedSignature);
        byte[] actualBytes = Encoding.UTF8.GetBytes(signature);

        return expectedBytes.Length == actualBytes.Length
            && CryptographicOperations.FixedTimeEquals(expectedBytes, actualBytes);
    }

    private static string CreatePayload(Guid publicGuid)
    {
        return $"vpn-mediator:v1:subscription:{publicGuid:D}";
    }
}

public interface ISubscriptionLinkFactory
{
    string CreateValidSubscriptionLink(HttpContext httpContext, Guid publicGuid);
}

public sealed class SubscriptionLinkFactory : ISubscriptionLinkFactory
{
    private readonly ILinkSigner _signer;
    private readonly VpnMediatorOptions _options;

    public SubscriptionLinkFactory(
        ILinkSigner signer,
        IOptions<VpnMediatorOptions> options)
    {
        _signer = signer;
        _options = options.Value;
    }

    public string CreateValidSubscriptionLink(HttpContext httpContext, Guid publicGuid)
    {
        string signature = _signer.CreateSignature(publicGuid);
        string publicBaseUrl = GetPublicBaseUrl(httpContext);

        return $"{publicBaseUrl}/sub/{publicGuid:D}/servers.txt?sig={signature}";
    }

    private string GetPublicBaseUrl(HttpContext httpContext)
    {
        return ProgramUrlHelpers.GetPublicBaseUrl(httpContext, _options);
    }
}

public interface IDeviceSubscriptionLinkFactory
{
    string CreateDeviceSubscriptionLink(
        HttpContext httpContext,
        Guid publicGuid,
        string devicePublicId,
        string secret);
}

public sealed class DeviceSubscriptionLinkFactory : IDeviceSubscriptionLinkFactory
{
    private readonly VpnMediatorOptions _options;

    public DeviceSubscriptionLinkFactory(IOptions<VpnMediatorOptions> options)
    {
        _options = options.Value;
    }

    public string CreateDeviceSubscriptionLink(
        HttpContext httpContext,
        Guid publicGuid,
        string devicePublicId,
        string secret)
    {
        string publicBaseUrl = ProgramUrlHelpers.GetPublicBaseUrl(httpContext, _options);
        return $"{publicBaseUrl}/sub/{publicGuid:D}/devices/{Uri.EscapeDataString(devicePublicId)}/servers.txt?token={Uri.EscapeDataString(secret)}";
    }
}

public static class ProgramUrlHelpers
{
    public static string GetPublicBaseUrl(HttpContext httpContext, VpnMediatorOptions options)
    {
        string? configuredBaseUrl = TextSanitizer.NullIfWhiteSpace(options.PublicBaseUrl);

        if (configuredBaseUrl is not null)
        {
            return configuredBaseUrl.TrimEnd('/');
        }

        return $"{httpContext.Request.Scheme}://{httpContext.Request.Host}";
    }
}

public static class SubscriptionResponseSecurity
{
    public static bool IsProtectedPath(PathString path)
    {
        return path.StartsWithSegments("/sub");
    }

    public static void Apply(HttpResponse response)
    {
        response.Headers.CacheControl = "private, no-store, max-age=0, must-revalidate";
        response.Headers.Pragma = "no-cache";
        response.Headers.Expires = "0";
        response.Headers["Referrer-Policy"] = "no-referrer";
        response.Headers["X-Content-Type-Options"] = "nosniff";
        response.Headers["X-Robots-Tag"] = "noindex, nofollow, noarchive";
    }
}

public static class LegacyHandoffResponseSecurity
{
    public static void Apply(HttpResponse response)
    {
        response.Headers.CacheControl = "private, no-store, max-age=0, must-revalidate";
        response.Headers.Pragma = "no-cache";
        response.Headers.Expires = "0";
        response.Headers["Referrer-Policy"] = "no-referrer";
        response.Headers["X-Content-Type-Options"] = "nosniff";
        response.Headers["X-Frame-Options"] = "DENY";
        response.Headers["X-Robots-Tag"] = "noindex, nofollow, noarchive";
        response.Headers["Content-Language"] = "ru";
        response.Headers["Cross-Origin-Opener-Policy"] = "same-origin";
        response.Headers["Cross-Origin-Resource-Policy"] = "same-origin";
        response.Headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()";
        response.Headers["Content-Security-Policy"] =
            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
            + "form-action 'none'; frame-ancestors 'none'";
    }
}

public static class PublicBrandingPolicy
{
    public const string CanonicalTelegramUsername = "@RazaltushVpnBot";

    public static string NormalizeTelegramUsername(string? value)
    {
        _ = value;
        return CanonicalTelegramUsername;
    }

    public static string NormalizeTelegramBotName(string? value)
    {
        return NormalizeTelegramUsername(value).TrimStart('@');
    }
}

public static class LegacyHandoffTombstoneRenderer
{
    public static string Render(string productName, string supportTelegramBotUsername)
    {
        string safeProductName = WebUtility.HtmlEncode(productName);
        string botUsername = PublicBrandingPolicy.NormalizeTelegramBotName(supportTelegramBotUsername);
        string safeBotUsername = WebUtility.HtmlEncode($"@{botUsername}");
        string safeBotUrl = WebUtility.HtmlEncode($"https://t.me/{botUsername}");

        return $$"""
            <!doctype html>
            <html lang="ru">
            <head>
              <meta charset="utf-8">
              <meta name="viewport" content="width=device-width, initial-scale=1">
              <meta name="color-scheme" content="light dark">
              <title>Ссылка устарела — {{safeProductName}}</title>
              <style>
                *, *::before, *::after { box-sizing: border-box; }
                :root {
                  color-scheme: light dark;
                  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                }
                body {
                  margin: 0;
                  min-height: 100vh;
                  display: grid;
                  place-items: center;
                  padding: 24px;
                  background:
                    radial-gradient(circle at 18% 12%, rgb(13 148 136 / 0.12), transparent 34rem),
                    #f4f7f7;
                  color: #17212b;
                }
                main {
                  width: min(680px, 100%);
                  background: rgb(255 255 255 / 0.96);
                  border: 1px solid #d5dfdf;
                  border-radius: 20px;
                  padding: clamp(24px, 5vw, 40px);
                  box-shadow: 0 24px 70px rgb(15 23 42 / 0.12);
                }
                .brand { margin: 0 0 22px; font-size: 0.9rem; font-weight: 750; letter-spacing: 0.08em; text-transform: uppercase; color: #0f766e; }
                .status { display: inline-flex; align-items: center; gap: 8px; margin-bottom: 14px; border-radius: 999px; padding: 7px 11px; background: #e7f6f3; color: #115e59; font-size: 0.86rem; font-weight: 700; }
                .status::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
                h1 { max-width: 16ch; font-size: clamp(1.7rem, 5vw, 2.35rem); line-height: 1.12; margin: 0 0 16px; letter-spacing: -0.025em; }
                p { margin: 0; font-size: 1.04rem; line-height: 1.62; }
                ol { margin: 22px 0 0; padding: 0; list-style: none; counter-reset: steps; display: grid; gap: 12px; }
                li { counter-increment: steps; display: grid; grid-template-columns: 32px 1fr; gap: 12px; align-items: start; line-height: 1.5; }
                li::before { content: counter(steps); display: grid; place-items: center; width: 32px; height: 32px; border-radius: 10px; background: #e7f6f3; color: #115e59; font-weight: 800; }
                a { display: flex; min-height: 50px; align-items: center; justify-content: center; margin-top: 26px; border-radius: 12px; background: #0f766e; color: #fff; font-weight: 700; text-decoration: none; padding: 12px 20px; box-shadow: 0 8px 20px rgb(15 118 110 / 0.22); }
                a:hover { background: #0b665f; }
                a:focus-visible { outline: 3px solid #5eead4; outline-offset: 4px; }
                .muted { margin-top: 16px; color: #52606d; font-size: 0.94rem; }
                @media (prefers-color-scheme: dark) {
                  body { background: radial-gradient(circle at 18% 12%, rgb(45 212 191 / 0.12), transparent 34rem), #0f1720; color: #f3f7f7; }
                  main { background: rgb(24 35 44 / 0.98); border-color: #334852; }
                  .brand { color: #5eead4; }
                  .status, li::before { background: #163f3b; color: #99f6e4; }
                  .muted { color: #b8c5cb; }
                }
                @media (max-width: 480px) {
                  body { place-items: center; padding: 16px; }
                  main { border-radius: 16px; }
                  .brand { margin-bottom: 18px; }
                }
              </style>
            </head>
            <body>
              <main aria-labelledby="page-title">
                <p class="brand">{{safeProductName}}</p>
                <div class="status">Ссылка устарела</div>
                <h1 id="page-title">Откройте подключение через Telegram</h1>
                <p>Эта старая ссылка больше не выдаёт доступ. Вашу подписку она не отключает и не изменяет.</p>
                <ol>
                  <li>Откройте Telegram-бот {{safeBotUsername}}.</li>
                  <li>Нажмите «Открыть в Happ», чтобы получить актуальное подключение.</li>
                </ol>
                <a href="{{safeBotUrl}}" rel="noreferrer" aria-label="Открыть Telegram-бот {{safeBotUsername}}">Открыть Telegram-бот</a>
                <p class="muted">Повторно оплачивать подписку из-за этой страницы не нужно.</p>
              </main>
            </body>
            </html>
            """;
    }

}

public static class SubscriptionCodec
{
    public static IReadOnlyList<string> DecodeServerLinks(string encodedSubscription)
    {
        string normalized = encodedSubscription
            .Trim()
            .Replace("\r", string.Empty, StringComparison.Ordinal)
            .Replace("\n", string.Empty, StringComparison.Ordinal)
            .Replace(" ", string.Empty, StringComparison.Ordinal)
            .Replace('-', '+')
            .Replace('_', '/');

        int paddingRemainder = normalized.Length % 4;

        if (paddingRemainder != 0)
        {
            normalized = normalized.PadRight(
                normalized.Length + 4 - paddingRemainder,
                '=');
        }

        byte[] bytes = Convert.FromBase64String(normalized);
        string decoded = Encoding.UTF8.GetString(bytes);

        return decoded.Split(
            ['\r', '\n'],
            StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
    }

    public static string EncodeServerLinks(IEnumerable<string> serverLinks)
    {
        string decodedSubscription = string.Join("\n", serverLinks);
        byte[] bytes = Encoding.UTF8.GetBytes(decodedSubscription);

        return Convert.ToBase64String(bytes);
    }
}

public interface ISubscriptionResponseBuilder
{
    string BuildSnapshotSubscription(
        IReadOnlyList<string> publishedServerLinks,
        TokenSubscriptionAccessResult accessResult);

    IReadOnlyList<string> BuildSnapshotServerLinks(
        IReadOnlyList<string> publishedServerLinks,
        TokenSubscriptionAccessResult accessResult);

    string BuildStatusSubscription(
        UserFacingStatus status,
        TokenSubscriptionAccessResult accessResult);

    IReadOnlyList<string> BuildStatusServerLinks(
        UserFacingStatus status,
        TokenSubscriptionAccessResult accessResult);
}

public sealed class SubscriptionResponseBuilder : ISubscriptionResponseBuilder
{
    public string BuildSnapshotSubscription(
        IReadOnlyList<string> publishedServerLinks,
        TokenSubscriptionAccessResult accessResult)
    {
        return SubscriptionCodec.EncodeServerLinks(
            BuildSnapshotServerLinks(publishedServerLinks, accessResult));
    }

    public IReadOnlyList<string> BuildSnapshotServerLinks(
        IReadOnlyList<string> publishedServerLinks,
        TokenSubscriptionAccessResult accessResult)
    {
        _ = accessResult;
        return publishedServerLinks.ToArray();
    }

    public string BuildStatusSubscription(
        UserFacingStatus status,
        TokenSubscriptionAccessResult accessResult)
    {
        return SubscriptionCodec.EncodeServerLinks(BuildStatusServerLinks(status, accessResult));
    }

    public IReadOnlyList<string> BuildStatusServerLinks(
        UserFacingStatus status,
        TokenSubscriptionAccessResult accessResult)
    {
        _ = accessResult;
        string message = status switch
        {
            UserFacingStatus.ServersUnavailable =>
                "⚠️ Серверы временно обновляются — откройте Telegram-бот",
            UserFacingStatus.DeviceTokenInvalid
                or UserFacingStatus.DeviceTokenRevoked
                or UserFacingStatus.DeviceTokenExpired
                or UserFacingStatus.LegacyLinkDisabled =>
                "⚠️ Подключение больше не действует — откройте Telegram-бот",
            UserFacingStatus.SubscriptionExpired =>
                "⚠️ Доступ закончился — откройте Telegram-бот",
            UserFacingStatus.DeviceProvisioningUnavailable =>
                "⚠️ Подключение ещё создаётся — обновите подписку через несколько секунд",
            UserFacingStatus.DeviceIdentityRequired =>
                "⚠️ Happ не передал идентификатор устройства — включите HWID и обновите подписку",
            UserFacingStatus.DeviceTransferRequired =>
                "⚠️ Этот профиль подключён к другому устройству — перенесите его через Telegram-бот",
            _ =>
                "⚠️ Доступ не обновлён — откройте Telegram-бот"
        };

        return [ServiceServerLinkFactory.CreateServiceServerLink(message)];
    }
}

public static class HappSubscriptionMetadata
{
    public const string AnnouncementHeaderName = "announce";

    public static void Apply(
        HttpResponse response,
        TokenSubscriptionAccessResult accessResult,
        string supportTelegramBotUsername)
    {
        if (!accessResult.Allowed)
        {
            return;
        }

        response.Headers[AnnouncementHeaderName] = BuildAnnouncementHeader(
            accessResult,
            supportTelegramBotUsername);
    }

    public static string BuildAnnouncementHeader(
        TokenSubscriptionAccessResult accessResult,
        string supportTelegramBotUsername,
        DateTimeOffset? nowUtc = null)
    {
        List<string> lines =
        [
            $"📱 Подключено {accessResult.ActiveDeviceTokens} из {accessResult.MaxDeviceTokens} устройств"
        ];

        if (accessResult.ValidUntilUtc is DateTimeOffset validUntilUtc)
        {
            int remainingDays = CalculateRemainingDays(
                validUntilUtc,
                nowUtc ?? DateTimeOffset.UtcNow);
            lines.Add(remainingDays == 0
                ? "⏳ Подписка заканчивается сегодня"
                : $"⏳ Подписка закончится через {remainingDays} {FormatDays(remainingDays)}");
        }

        lines.Add($"💬 Telegram: {PublicBrandingPolicy.NormalizeTelegramUsername(supportTelegramBotUsername)}");
        string encodedAnnouncement = Convert.ToBase64String(
            Encoding.UTF8.GetBytes(string.Join("\n", lines)));

        return $"base64:{encodedAnnouncement}";
    }

    private static int CalculateRemainingDays(
        DateTimeOffset validUntilUtc,
        DateTimeOffset nowUtc)
    {
        double remainingDays = (validUntilUtc - nowUtc).TotalDays;
        return remainingDays <= 0 ? 0 : (int)Math.Ceiling(remainingDays);
    }

    private static string FormatDays(int days)
    {
        int lastTwoDigits = days % 100;
        if (lastTwoDigits is >= 11 and <= 14)
        {
            return "дней";
        }

        return (days % 10) switch
        {
            1 => "день",
            2 or 3 or 4 => "дня",
            _ => "дней"
        };
    }
}

public static class ServiceServerLinkFactory
{
    private static readonly Guid ServiceServerId = Guid.Parse("00000000-0000-0000-0000-000000000001");

    public static string CreateServiceServerLink(string displayName)
    {
        string escapedDisplayName = Uri.EscapeDataString(displayName);

        return $"vless://{ServiceServerId:D}@127.0.0.1:443?encryption=none&security=none&type=tcp#{escapedDisplayName}";
    }
}

public static class Base64Url
{
    public static string Encode(byte[] bytes)
    {
        return Convert.ToBase64String(bytes)
            .TrimEnd('=')
            .Replace('+', '-')
            .Replace('/', '_');
    }
}

public static class AdminGuard
{
    public static bool IsAllowed(
        HttpContext httpContext,
        string expectedAdminToken,
        string? previousAdminToken = null,
        DateTimeOffset? previousAdminTokenValidUntilUtc = null)
    {
        string actualAdminToken = httpContext.Request.Headers["X-Admin-Token"].ToString();
        if (FixedTimeEquals(actualAdminToken, expectedAdminToken))
        {
            return true;
        }

        return previousAdminTokenValidUntilUtc is not null
            && previousAdminTokenValidUntilUtc >= DateTimeOffset.UtcNow
            && FixedTimeEquals(actualAdminToken, previousAdminToken);
    }

    private static bool FixedTimeEquals(string? actualToken, string? expectedToken)
    {
        if (string.IsNullOrWhiteSpace(actualToken) || string.IsNullOrWhiteSpace(expectedToken))
        {
            return false;
        }

        byte[] expectedBytes = Encoding.UTF8.GetBytes(expectedToken);
        byte[] actualBytes = Encoding.UTF8.GetBytes(actualToken);
        return expectedBytes.Length == actualBytes.Length
            && CryptographicOperations.FixedTimeEquals(expectedBytes, actualBytes);
    }
}

public static class AdminPathAuthorization
{
    public static bool IsProtectedPath(PathString path)
    {
        return path.StartsWithSegments("/admin");
    }

    public static bool IsAllowed(HttpContext httpContext, VpnMediatorOptions options)
    {
        return AdminGuard.IsAllowed(
            httpContext,
            options.AdminToken,
            options.PreviousAdminToken,
            options.PreviousAdminTokenValidUntilUtc);
    }
}

public static class RouteIdentifierValidator
{
    public static bool IsSafe(string value)
    {
        return value.Length is > 0 and <= 64
            && value.All(character => char.IsAsciiLetterOrDigit(character) || character is '-' or '_');
    }
}



public static class ApiResults
{
    public static IResult BadRequestText(string message)
    {
        return Error(
            "bad_request",
            message,
            StatusCodes.Status400BadRequest);
    }

    public static IResult BadRequest(
        string errorCode,
        string message,
        HttpContext? httpContext = null)
    {
        return Error(
            errorCode,
            message,
            StatusCodes.Status400BadRequest,
            httpContext: httpContext);
    }

    public static IResult UnauthorizedText(HttpContext? httpContext = null)
    {
        return Error(
            "unauthorized",
            "Admin token is missing or invalid.",
            StatusCodes.Status401Unauthorized,
            httpContext: httpContext);
    }

    public static IResult ForbiddenText(string message)
    {
        return Error(
            "forbidden",
            message,
            StatusCodes.Status403Forbidden);
    }

    public static IResult NotFoundText(string message)
    {
        return Error(
            "not_found",
            message,
            StatusCodes.Status404NotFound);
    }

    public static IResult Gone(
        string errorCode,
        string message,
        object? details,
        HttpContext? httpContext = null)
    {
        return Error(
            errorCode,
            message,
            StatusCodes.Status410Gone,
            details,
            httpContext);
    }

    public static IResult Conflict(
        string errorCode,
        string message,
        object? details,
        HttpContext? httpContext = null)
    {
        return Error(
            errorCode,
            message,
            StatusCodes.Status409Conflict,
            details,
            httpContext);
    }

    public static IResult Error(
        string errorCode,
        string message,
        int statusCode,
        object? details = null,
        HttpContext? httpContext = null)
    {
        string traceId = httpContext?.TraceIdentifier ?? string.Empty;

        return Results.Json(
            new
            {
                errorCode,
                message,
                details = details ?? new { },
                traceId
            },
            statusCode: statusCode);
    }
}

public static class SubscriptionMapper
{
    public static SubscriptionDetailsResponse ToDetailsResponse(SubscriptionRecord subscription)
    {
        return ToDetailsResponse(subscription, null);
    }

    public static SubscriptionDetailsResponse ToDetailsResponse(
        SubscriptionRecord subscription,
        string? subscriptionUrl)
    {
        IReadOnlyList<DeviceBindingResponse> devices = subscription.Devices
            .OrderByDescending(x => x.LastSeenAtUtc)
            .Select(ToDeviceResponse)
            .ToArray();

        return new SubscriptionDetailsResponse(
            subscription.PublicGuid,
            subscriptionUrl,
            subscription.MaxDevices,
            subscription.IsActive,
            subscription.CreatedAtUtc,
            subscription.ExpiresAtUtc,
            subscription.CustomerName,
            subscription.Note,
            subscription.Devices.Count(x => x.IsActive),
            devices);
    }

    private static DeviceBindingResponse ToDeviceResponse(DeviceBindingRecord device)
    {
        string hashPrefix = device.DeviceHash.Length <= 12
            ? device.DeviceHash
            : device.DeviceHash[..12];

        return new DeviceBindingResponse(
            device.DeviceBindingId,
            device.DeviceLabel,
            hashPrefix,
            device.IsActive,
            device.FirstSeenAtUtc,
            device.LastSeenAtUtc,
            device.AccessCount,
            device.IdentitySource);
    }
}

public static class TextSanitizer
{
    public static string? NullIfWhiteSpace(string? value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return null;
        }

        return value.Trim();
    }

    public static string TrimToLength(string value, int maxLength)
    {
        if (value.Length <= maxLength)
        {
            return value;
        }

        return value[..maxLength];
    }
}
