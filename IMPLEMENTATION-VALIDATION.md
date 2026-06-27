# YooKassa/SBP implementation validation

Status: release-gate validated candidate; production canary is still required.

Authoritative server baseline:

- Archive: `VpnMediator-ea428d9.tar.gz`
- SHA-256: `e6166f405536d934625dca2ef9a14ab7f992ba87daf2504a3b2f80ad0083b39b`
- The baseline `VpnMediator.csproj` package updates are preserved unchanged.

## Completed checks

- Python source and tests compile with `python -m compileall`.
- Architecture, public-branding, secret-scan, and lock-file guards pass.
- All shell scripts pass `bash -n`.
- Release-evidence and updater self-tests pass.
- Checkout token signature, tamper rejection, and expiry behavior pass an isolated contract test.
- Migration 34 contract passes an isolated SQLite test, including exact quote input storage and
  the one-order-per-quote unique index.
- The coordinated backup/restore self-test passes, including rejection of a migration-history gap.
- Fedora release validation built and imported the Bot wheel, passed Ruff check and format,
  passed all 388 Python tests, and completed `pip-audit` with no known vulnerabilities.
- Fedora SDK 10.0.301 built the Mediator with zero warnings and errors, passed all 148 .NET
  tests, and reported no vulnerable NuGet packages.
- Deployment, coordinated backup/restore, release-evidence, updater, shell syntax, and ShellCheck
  gates passed.
- The release package excludes runtime directories, caches, local databases, and secrets; its
  evidence is regenerated for the exact packaged source tree.

## Tests added

- Independent Stars and RUB pricing.
- Exact paid-time snapshot for device-upgrade repricing.
- Stars/SBP quote-consumption race.
- Signed checkout token tampering and expiry.
- YooKassa request, idempotence, amount parsing, HTTP failure, and path-injection rejection.
- Checkout security headers and HTTPS-only provider redirect.
- GET checkout has no financial side effect.
- YooKassa inbox reconciliation and amount-mismatch manual review.
- Production configuration fail-closed validation.
- Migration 34 schema and unique-index checks.

## External validation still required

- Browser screenshots were not supplied for the checkout page. Automated HTTP, security-header,
  redirect, token, and no-side-effect GET tests passed, but visual rendering remains external.
- Before production enablement, run a YooKassa test-shop matrix and one low-value real SBP canary.
  Verify the self-employed receipt flow, duplicate/canceled webhooks, restart recovery, access
  activation, and Telegram notification.

Keep `EXTERNAL_PAYMENT_ENABLED=false` until the production-facing checks pass. The webhook recovery listener may
remain enabled independently with `YOOKASSA_INTEGRATION_ENABLED=true`.
