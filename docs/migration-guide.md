# Migration guide

## Перед обновлением

Сериализуйте два writer-процесса и создайте verified backup pair Bot/Mediator. Code rollback
и database restore — разные решения. Текущие миграции additive; исторические health и
managed-profile tables остаются в БД только для совместимости с предыдущим binary.

## Переход на unified feed

Новые подключения используют одну ссылку `/sub/{subscription}/feed?token=...` и `x-hwid`.
Старые per-device URL продолжают читаться, учитываются в том же entitlement, но больше не
выдаются, не показываются повторно и не регенерируются.

## Удаление Probe Agent

Перед фактическим deployment замените production env по актуальным примерам. Удалите старые
`VpnMediator__ServerHealth*`, `VpnMediator__DeviceAccessMode`,
`VpnMediator__ManagedDeviceProvisioning*`, `VpnMediator__UnifiedSubscriptionFeedEnabled`,
`VpnMediator__FileLogging*`, а также Bot keys `COMMERCE_MINIMUM_HEALTHY_SERVERS`,
`TELEGRAM_PROVIDER_TOKEN` и `SUPPORT_TELEGRAM_USERNAME`. Updater прекращает работу, если
такие ключи ещё присутствуют, чтобы ignored configuration не выглядела действующей.

После успешного запуска новой версии один раз выполните:

```bash
sudo systemctl disable --now vpn-probe-agent.service || true
sudo rm -f /etc/systemd/system/vpn-probe-agent.service
sudo rm -rf /etc/systemd/system/vpnmediator.service.d/50-vpn-probe.conf
sudo systemctl daemon-reload
```

Каталоги `/opt/vpn-probe-agent` и `/etc/vpn-probe-agent` можно удалить после закрытия
rollback window согласно обычной change procedure.

## Rollback

Верните предыдущие `current` symlinks и перезапустите два сервиса. Не восстанавливайте только
одну БД и не применяйте автоматический restore после новых платежных или entitlement-записей.
