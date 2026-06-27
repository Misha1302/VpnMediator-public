# Release evidence matrix

| Requirement | Owning component | Automated evidence | External evidence |
|---|---|---|---|
| Payment and entitlement idempotency | Bot state machines/repositories | Full Python suite | Real Stars canary |
| Unified device limit | Mediator unified feed/repository | .NET identity/concurrency tests | Real Happ devices |
| Catalog publication without Probe filtering | Mediator catalog pipeline | Catalog refresh regression test | Upstream smoke |
| Legacy link compatibility | Mediator token validation | .NET compatibility tests | Existing client refresh |
| Two-service atomic deployment | updater/systemd | guards and updater self-test | Host rollback canary |
| Two-database recovery | backup/restore scripts | local restore self-test | Off-host restore drill |
| Dependency security | lockfiles/SBOM | pip-audit and NuGet audit | Ongoing CI |

