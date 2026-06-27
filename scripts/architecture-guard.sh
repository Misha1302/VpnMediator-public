#!/usr/bin/env bash

set -Eeuo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPOSITORY_ROOT"

fail()
{
    printf 'Architecture guard failed: %s\n' "$1" >&2
    exit 1
}

grep_python_sources()
{
    grep -R -n \
        --binary-files=without-match \
        --include='*.py' \
        --exclude-dir='__pycache__' \
        "$@"
}

if grep_python_sources --fixed-strings 'main_menu_keyboard()' VpnAccessBot/vpn_access_bot; then
    fail 'a user handler builds a main menu without computed state'
fi

if grep_python_sources -E 'handoff:qr|Дальше:' VpnAccessBot/vpn_access_bot; then
    fail 'removed onboarding or purchase callbacks were reintroduced'
fi

if grep -R -n --binary-files=without-match \
    --include='*.cs' --include='*.py' \
    -E 'HandoffPageRenderer|CreateConnectionHandoffClaimAsync|RedeemConnectionHandoffClaimAsync|GetConnectionHandoffClaimStatusAsync|create_connection_handoff|get_connection_handoff_status' \
    Program.cs MediatorStorage.cs VpnAccessBot/vpn_access_bot; then
    fail 'dynamic browser handoff runtime was reintroduced'
fi

if grep -n -E 'handoff-claims|Map(Post|Get)\("/connect/\{publicId\}/(redeem|status)"' Program.cs; then
    fail 'a dynamic browser handoff endpoint was reintroduced'
fi

if grep -R -n --binary-files=without-match --include='*.py' \
    -E 'callback_data="handoff:(create|check)"' \
    VpnAccessBot/vpn_access_bot; then
    fail 'new Telegram keyboards emit obsolete handoff callbacks'
fi

if ! grep -q 'F.data.in_({"credential:create", "handoff:create"})' \
    VpnAccessBot/vpn_access_bot/handlers/onboarding.py \
    || ! grep -q 'F.data.in_({"credential:check", "handoff:check"})' \
        VpnAccessBot/vpn_access_bot/handlers/onboarding.py; then
    fail 'one-release compatibility aliases for already sent Telegram buttons are missing'
fi

if grep -R -n --binary-files=without-match \
    -E 'HandoffClaimTtlMinutes|VpnMediator__HandoffClaimTtlMinutes' \
    appsettings.json deploy/mediator.env.example; then
    fail 'obsolete dynamic handoff configuration was reintroduced'
fi

if grep -n -E 'VPN РАБОТАЕТ|Свободно подключений|НЕ ПОДКЛЮЧАТЬ' Program.cs; then
    fail 'healthy subscription pseudo-servers were reintroduced'
fi

if grep_python_sources -E 'str\(exception\)|\{exception\}' VpnAccessBot/vpn_access_bot/handlers; then
    fail 'raw exception details are exposed through Telegram handlers'
fi

if grep_python_sources -E 'MIN_PURCHASABLE_|MAX_PURCHASABLE_' \
    VpnAccessBot/vpn_access_bot || \
    grep -n -E 'MIN_PURCHASABLE_|MAX_PURCHASABLE_' \
        VpnAccessBot/.env.example deploy/bot.env.example; then
    fail 'obsolete min/max product configuration was reintroduced'
fi


direct_entitlement_calls="$({
    grep_python_sources -E '\.(apply_entitlement_operation|create_subscription|disable_subscription|update_entitlement)\(' \
        VpnAccessBot/vpn_access_bot || true
} | grep -v '/operations.py:' | grep -v '/mediator_client.py:' || true)"
if [[ -n "$direct_entitlement_calls" ]]; then
    printf '%s\n' "$direct_entitlement_calls" >&2
    fail 'Bot entitlement mutations bypass the durable operation coordinator'
fi

refund_provider_calls="$({
    grep_python_sources --fixed-strings 'refund_star_payment(' VpnAccessBot/vpn_access_bot || true
} | grep -v '/handlers/admin.py:' || true)"
if [[ -n "$refund_provider_calls" ]]; then
    printf '%s\n' "$refund_provider_calls" >&2
    fail 'a Telegram refund provider call bypasses the durable admin refund flow'
fi

if grep_python_sources -E 'state[[:space:]]*=[[:space:]]*"completed"' \
    VpnAccessBot/vpn_access_bot/handlers; then
    fail 'a Telegram handler completes a durable operation state directly'
fi

legacy_device_token_insert_count="$(grep -c 'INSERT INTO device_access_tokens' MediatorStorage.cs || true)"
unified_device_insert_count="$(grep -c 'INSERT INTO device_access_tokens' UnifiedSubscriptionFeed.cs || true)"
all_device_insert_count="$(
    grep -R -h --include='*.cs' 'INSERT INTO device_access_tokens' . \
        | wc -l \
        | tr -d ' '
)"
if [[ "$legacy_device_token_insert_count" != "1" \
    || "$unified_device_insert_count" != "1" \
    || "$all_device_insert_count" != "2" ]]; then
    fail 'device creation bypasses the two owned legacy/unified insertion pipelines'
fi

if ! grep -q 'MapGet("/sub/{publicGuid:guid}/feed"' Program.cs; then
    fail 'the unified subscription feed endpoint is missing'
fi

if grep -n -E \
    'Map(Post|Put)\("/admin/subscriptions/\{publicGuid:guid\}/device-tokens|device-tokens/\{devicePublicId\}/(credential|regenerate|transfer)' \
    Program.cs; then
    fail 'personal device-link issuance endpoints were reintroduced'
fi

if ! grep -q 'ensure_subscription_feed(subscription_guid)' \
    VpnAccessBot/vpn_access_bot/handlers/onboarding.py; then
    fail 'Telegram onboarding does not use the shared subscription feed'
fi

if grep -n -E '5063|vpn-control-provider' deploy/nginx.conf.example; then
    fail 'an internal control-plane provider was exposed through the public reverse proxy'
fi

if grep -R -n --binary-files=without-match \
    -E 'VpnProbeAgent|vpn-probe-agent|ManagedDeviceProvisioning|DeviceAccessMode' \
    Program.cs VpnAccessBot/vpn_access_bot deploy .github/workflows; then
    fail 'a removed Probe Agent or managed provisioning path was reintroduced'
fi

printf 'Architecture guard passed.\n'
