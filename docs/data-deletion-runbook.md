# User data deletion runbook

1. Accept the request only in the user's private bot chat or through an authenticated support process.
2. Resolve actor identity from Telegram, never from a supplied public ID.
3. Create an audited deletion case and disclose which records must be retained for payment, fraud, security or legal obligations.
4. Export/verify the user's scoped records, then remove or anonymize optional profile, onboarding and analytics data in one reviewed transaction/batch.
5. Revoke all device credentials and disable technical access.
6. Do not delete payment/order/audit evidence before the approved retention period; restrict its use instead.
7. Verify no live credential remains, record completion date and send a plain-language confirmation.

This package contains the policy/runbook, not jurisdiction-specific retention periods. Final periods and operator identity are external legal inputs.
