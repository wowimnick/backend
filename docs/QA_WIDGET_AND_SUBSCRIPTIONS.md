# QA: Widget embed & subscriptions

Use this checklist before releases that touch billing, webhooks, or the embed.

## Stripe webhooks

- [ ] Replay the same `invoice.payment_failed` event ID twice → second delivery is a no-op (ProcessedStripeEvent).
- [ ] `invoice.paid` replay → idempotent (no duplicate ledger effects).
- [ ] `customer.subscription.updated` / `deleted` for widget SaaS → DB row matches Stripe.
- [ ] Widget SaaS `invoice.payment_failed` sets `payment_grace_until` and owner notification path runs once.

## Widget SaaS access

- [ ] `active` / `trialing` → widget API and dashboard widget tab allowed (when plan applies).
- [ ] `past_due` **without** grace → blocked.
- [ ] `past_due` **with** `payment_grace_until` in future → access allowed until grace end.
- [ ] Growth/Advanced + `active`/`trialing` → membership dashboard visible; `past_due` (even with widget grace) → **membership** tab hidden per entitlement rules.

## Admin

- [ ] User **without** `quickstart.view_widgetsubscription` cannot call comp-override.
- [ ] Comp override writes AuditLog / reason required.
- [ ] Widget subscriptions list shows grace + comp columns.

## Embed (popup loader)

- [ ] Loader `postMessage` uses widget CDN `targetOrigin` (not `*` where possible).
- [ ] Iframe has `sandbox` attribute; booking + Stripe still complete.
- [ ] `data-ce-open-booking` / `data-ce-open-membership` open flows on host page.

## Observability

- [ ] Failed `ProcessedStripeEvent` persistence logs error and reports to Sentry (if configured).
- [ ] Optional: CloudWatch metric/alarm on webhook 5xx rate or DLQ (ops).

## Dashboard diagnostics

- [ ] GET `/api/my-business/widget-diagnostics/?referrer=https://client-site.com` returns allowlist match + recent funnel events (authenticated owner).
