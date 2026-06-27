# Telegram Stars refund runbook

Only an administrator may initiate a refund for an owner-scoped paid order with a stored Telegram payment charge ID. Read the order and payment state first; never accept a charge ID from user text.

1. Confirm identity, amount, currency, order state and technical refund eligibility.
2. Ensure no earlier refund/provider refund ID exists.
3. Invoke Telegram's Stars refund operation once with an idempotent operator workflow.
4. Mark the order refunded only after provider acceptance; preserve financial history and audit actor/correlation ID.
5. Apply the documented entitlement effect. Do not silently shorten unrelated prior entitlement or other devices.
6. Notify the user with the result and support path.
7. On timeout/unknown result, reconcile provider state before retrying; do not issue a second refund blindly.

A real provider refund test is required before production.
