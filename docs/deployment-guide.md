# Deployment guide

## Topology

Один Linux host запускает два writer-процесса:

```text
Telegram -> VpnAccessBot -> VpnMediator -> external subscription_url
             |                 |
             SQLite            SQLite
```

Горизонтальный запуск нескольких writer-экземпляров одного сервиса не поддерживается.
Nginx публикует только allowlisted routes; `/internal/*` остаётся локальным.

## Конфигурация

Скопируйте `deploy/mediator.env.example` в `/etc/vpn-mediator/mediator.env`, а
`deploy/bot.env.example` в `/etc/vpn-access-bot/bot.env`. Права обоих файлов — `0600`.
Задайте реальные URL, Telegram/admin токены и независимые ключи. Не дублируйте defaults
из примеров без операционной причины.

## Install и update

1. Установите .NET 10, Python 3.11-3.13, SQLite, ShellCheck и Nginx.
2. Создайте пользователей `vpn-mediator` и `vpn-access-bot`.
3. Установите два systemd unit из `deploy/` и Nginx config.
4. Сделайте persistent `/var/lib/vpn-mediator` и `/var/lib/vpn-access-bot`.
5. Выполните:

```bash
./update_vpn_mediator.sh --check-only
sudo ./update_vpn_mediator.sh
```

Updater валидирует текущий checkout, собирает два versioned release, останавливает writers
для согласованного backup обеих SQLite БД, атомарно переключает `current` symlinks и
проверяет `/health/ready` Mediator и `/health/live` Bot.

## Проверка

- оба systemd unit enabled и active;
- каталог fresh и содержит серверы;
- `/sub/*` отвечает с `Cache-Control: private, no-store`;
- существующая legacy device-link и unified feed обновляются;
- disabled HWID получает отказ;
- реальная покупка, продление и order-scoped refund проходят canary.

## Rollback

При неуспешном health-check updater возвращает предыдущие symlinks и systemd units; при первом
переходе с фиксированных путей восстанавливаются старые unit-файлы и процессы.
Базы автоматически не откатываются: additive migrations рассчитаны на binary rollback, а
restore двух БД является отдельным incident-решением после оценки новых платежей и entitlement.
