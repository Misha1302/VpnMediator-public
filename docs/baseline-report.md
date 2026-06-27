# Baseline report

## Source

- Supplied archive: `VpnMediator-ea428d9.tar.gz`
- Local baseline commit: `6e9d4d833cc959d12da09bd525c20e431b20a0f1`
- Format-only commit: `8f76e7a256e19936a754e26fafaf764b56d08cdc`
- Baseline date: 2026-06-07

## Inventory

The supplied package contains the ASP.NET mediator, the Python/aiogram bot, tests,
deployment assets, documentation, CI configuration, and the release validation script.
No parallel replacement project was created.

## Baseline verification

| Check | Result | Evidence |
|---|---|---|
| Architecture guard | PASS | `scripts/architecture-guard.sh` |
| Python compileall | PASS | Release-gate run |
| Ruff lint | PASS | Release-gate run |
| Ruff format | FAIL, then fixed | Four files listed by the master-spec were reformatted in an isolated commit |
| Python tests | PASS | 60 passed before implementation changes |
| Shell syntax | PASS | Release-gate run |
| ShellCheck | PASS | ShellCheck 0.10.0 |
| .NET restore/build/test | BLOCKED | The execution image has no .NET SDK and outbound package download is unavailable |

The missing SDK is an execution-environment blocker, not recorded as a passing project check.
CI and clean-room evidence must run the .NET jobs before release approval.

## Baseline findings

Confirmed P0 gaps included the absence of a central private-chat boundary, non-owner-scoped
quote/payment service contracts, browser handoff as the normal credential path, hash-only raw
credential storage, and activation-time expiration recalculation. Confirmed P1 gaps included
support-role separation, worker supervision, production configuration validation, and incomplete
release evidence.
