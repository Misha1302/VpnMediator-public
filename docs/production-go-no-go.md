# Production go/no-go

Локально собранный архив не является доказательством production readiness.

## Обязательные host gates

1. `./update_vpn_mediator.sh --check-only` проходит на exact source tree.
2. Coordinated backup обеих SQLite БД и restore drill проверены, off-host copy создана.
3. `vpnmediator` и `vpn-access-bot` enabled и стабильны после daemon reload/restart.
4. Fresh catalog имеет ненулевое число серверов; legacy link и unified feed проверены в Happ.
5. Canary покрывает purchase, renewal, resume, duplicate Telegram update, refund и reconciliation.
6. DNS, TLS, Nginx no-cache, NTP, disk, logs, alerts и release-link rollback проверены.
7. Connected Python/NuGet advisory checks завершены.

## Advertising gates

До платной рекламы задайте измеренную capacity, включите admission policy, выполните реальный
Stars purchase/refund и подтвердите restart recovery без повторного provider refund. Trials и
referrals остаются выключенными до отдельных abuse/reversal canary.

Production GO возможен только после host preflight, контролируемого canary и проверки rollback.
