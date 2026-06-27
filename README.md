# Razaltush VPN

YooKassa SBP checkout is optional and disabled by default. See
`docs/yookassa-sbp.md` for its security model, configuration, canary, and rollback gates.

Проект состоит из двух сервисов:

- `VpnMediator` хранит подписки и entitlement, обновляет общий каталог серверов,
  выдаёт unified Happ feed и учитывает устройства по HMAC от `x-hwid`;
- `VpnAccessBot` реализует Telegram UI, платежи, возвраты, восстановление операций
  и синхронизацию подписок с Mediator.

У каждого сервиса своя SQLite БД. Платежные state machine, idempotency keys,
outbox, audit trail, recovery workers, миграции и согласованный backup сохранены.

## Доставка конфигурации

Для подписки создаётся одна стабильная ссылка:

```text
/sub/{subscription}/feed?token=...
```

Happ передаёт `x-hwid`; Mediator атомарно создаёт или переиспользует устройство и
проверяет общий лимит. Raw HWID не сохраняется. Старые персональные ссылки
`/sub/{subscription}/devices/{device}/servers.txt` продолжают читаться в течение
переходного периода, но новые персональные ссылки не создаются и не регенерируются.

Все устройства получают один опубликованный upstream-каталог. В проекте больше нет
Probe Agent, запросов к `google.com`, latency ranking и managed provisioning. Поэтому
отключение HWID блокирует последующие обновления feed, но не отзывает уже импортированный
raw upstream credential.

## Локальная проверка

```bash
./scripts/validate-release.sh
```

Validator проверяет Python lock-файлы, wheel, lint/format/tests, supply-chain guards,
backup/restore и .NET build/tests. Нужны Python 3.11-3.13, .NET SDK из `global.json`,
`shellcheck` и доступ к зафиксированным пакетам.

## Конфигурация

Начните с `deploy/mediator.env.example` и `deploy/bot.env.example`. В production
обязательно задаются URL/пути, токены и независимые ключи; обычные таймауты, лимиты
каталога и интервалы уже имеют defaults в коде и не требуют override.

## Deployment

```bash
./update_vpn_mediator.sh --check-only
sudo ./update_vpn_mediator.sh
```

Updater валидирует текущий checkout, собирает два versioned release, создаёт
согласованный backup обеих SQLite БД, атомарно переключает `current` symlinks и
возвращает предыдущие ссылки при неуспешном health-check. Секреты остаются в `/etc`,
данные — в `/var/lib`.

Одноразовое удаление старого `vpn-probe-agent.service` после обновления описано в
`docs/migration-guide.md`.
