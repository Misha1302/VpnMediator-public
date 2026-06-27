# Server catalog model

The catalog flow is:

`source reads → normalization/deduplication → health evaluation/filtering → ranking or deterministic fallback → output bound → final display formatting → fingerprints → publish`

`ContentFingerprint` hashes normalized technical identities without URI fragments/display names. `PresentationFingerprint` hashes the ordered rendered links. `PresentationVersion` versions the presentation policy; flag-prefixed final-position country naming is version `4`.

The compact model keeps protocol and canonical URI identity separate from fragment display text. When health ranking is enabled, fresh usable servers with measured latency are published before usable servers without a comparable latency measurement. Inside each measurement group, health state, latency bucket, previous position and deterministic tie-breakers remain authoritative. Unknown, unsupported and quarantined servers remain after usable states. Otherwise the fallback order is deterministic and natural-numeric (`Server 2` before `Server 10`) with technical identity as the stable tie-breaker.

After the final order and any safe-snapshot fallback are selected, each fragment is replaced completely with:

`<final publication number> | <flag> <Russian country name>`

For example: `1 | 🇩🇪 Германия` or `123 | 🇮🇹 Италия`. The publication number is always derived from the final list position and does not preserve an upstream sequence number. Country is resolved from `ServerPresentationCountryBySourceNumber`, keyed by the stable leading number in the upstream display name. Common country names/flags already present in an upstream name are normalized automatically, and repeated publication never duplicates the flag. Missing mappings are rendered explicitly as `🌐 Неизвестно`; the mediator does not guess a country from protocol, latency or display category.

Example environment entries:

```dotenv
VpnMediator__ServerPresentationCountryBySourceNumber__4=Германия
VpnMediator__ServerPresentationCountryBySourceNumber__17=Италия
```

This presentation-only formatting does not change technical identity, health state, deduplication or the underlying VPN configuration. `ServerCatalogMaxServers` remains the output bound.

Protocol validation still requires a supported scheme, host, port and credential/user info; private IP literals are rejected. Upstream response size and redirects remain bounded by fetcher options. Duplicate identities collapse deterministically. A near-total replacement of an established catalog is rejected and the last-known-good snapshot is republished as stale.

Structured city/business priority can be added to source metadata later without mixing it into technical identity. Happ auto-ping/autoconnect is not enabled without device evidence.
