# Known limitations

- Device limit применяется на unified Happ feed по HMAC от HWID.
- Отключение HWID блокирует следующие feed refresh, но не отзывает raw upstream credential,
  уже импортированный сторонним клиентом.
- Fetch подписки не доказывает, что VPN tunnel установлен.
- Reachability и latency серверов не измеряются; fresh catalog означает успешное получение
  и синтаксическую валидацию upstream source.
- Один writer-процесс на SQLite DB; horizontal writers не поддерживаются.
- Telegram updates доставляются at least once, поэтому state-changing handlers обязаны
  сохранять domain idempotency.
- Реальные Telegram Stars, Happ, DNS/TLS, upstream networking, systemd, offsite backup,
  monitoring, staging и canary остаются production-проверками.
- Online dependency advisory checks требуют networked CI.
