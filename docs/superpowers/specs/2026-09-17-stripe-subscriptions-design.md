# Stripe subscriptions — design

Status: draft, pending review. Author: Claude (session 2026-09-17).

## Goal

Replace the `mailto:` handoff on `frontend/payant.html`'s plan picker with a real
Stripe Checkout subscription flow, so choosing Flex / Essentiel / Sérénité takes a
user through payment and lands them back on `/vip.html` with `is_premium` set —
no manual DB edit, no re-login required.

## Non-goals

- No changes to the three plan names, prices, or descriptions already on
  `payant.html` (Flex 19,99€/mois, Essentiel 6,67€/mois, Sérénité 3,85€/mois) —
  reuse them as-is, do not touch `premium.html` (that page has no pricing UI).
- No Stripe Customer Portal / self-service cancellation UI in this pass — status
  changes (cancel, payment failure) are absorbed via webhook only; a portal link
  can be added later.
- No proration / plan-switch UI. Changing plans means canceling and resubscribing.
- Real Stripe keys are not available yet: the app must work with the checkout
  button hidden/disabled when Stripe isn't configured, mirroring how the Google
  OAuth button is hidden when its keys are empty.

## Data model

New `Subscription` table in `src/database/models.py`, next to `User`:

```python
class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)  # stripe_subscription_id
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    stripe_customer_id: Mapped[str] = mapped_column(String(100), nullable=False)
    plan_name: Mapped[str] = mapped_column(String(20), nullable=False)  # flex, essentiel, serenite
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # active, canceled, past_due, trialing
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_subscriptions_user_id", "user_id"),)
```

`User.is_premium` (existing column) remains the single field the rest of the app
reads to gate access — `Subscription` is bookkeeping/audit trail for what plan and
Stripe IDs back that flag, not a second source of truth. Applied via
`scripts/migrate.py` (never bare `create_all`), per `.claude/rules/database.md`.

## Config

Add to `Settings` in `src/config.py`, same pattern as `google_client_id/secret`
(empty-string defaults, `.env`-driven, never required at import time):

```python
stripe_secret_key: str = ""
stripe_webhook_secret: str = ""
stripe_price_flex: str = ""
stripe_price_essentiel: str = ""
stripe_price_serenite: str = ""
```

A helper `_stripe_configured()` (mirrors `_google_configured()` in
`auth_routes.py`) returns `bool(settings.stripe_secret_key and
settings.stripe_price_flex and settings.stripe_price_essentiel and
settings.stripe_price_serenite)`. Checkout route 404s and the frontend hides/
disables the "Commencer maintenant" button when this is false, so a dev without
Stripe keys sees the existing page in a safe, inert state rather than a broken
fetch.

`stripe` added to `requirements-api.txt` (and the dev venv) — Render's build only
installs that file, so a missing entry there is an import-time crash on deploy,
per CLAUDE.md.

## Routes — `src/api/billing_routes.py` (new `APIRouter`, same shape as `auth_routes.py`)

### `POST /api/checkout/create-session`

- Requires an authenticated session (reuses the existing session-cookie
  dependency used elsewhere in `src/api`, not a new auth mechanism).
- Body: `{"plan": "flex" | "essentiel" | "serenite"}`.
- 404 if `not _stripe_configured()`. 400 on unknown plan. 401 if not logged in
  (frontend catches this and redirects to `/login`, matching the pasted spec's
  frontend behavior).
- Creates a Stripe Checkout Session, `mode="subscription"`,
  `client_reference_id=str(user.id)`, `customer_email=user.email`,
  `success_url="<origin>/billing/success?session_id={CHECKOUT_SESSION_ID}"`,
  `cancel_url="<origin>/payant.html"`.
- Returns `{"url": session.url}`.

### `GET /billing/success`

This route exists specifically to solve the stale-cookie problem: the session
cookie bakes in `role` and is verified without a DB read
(`.claude/rules/api.md`'s Accounts section), so a bare webhook flipping
`is_premium` in the DB would leave the just-paid user looking non-premium until
they log out and back in.

- Reads `session_id` from the query string, retrieves the Checkout Session from
  Stripe, and confirms `payment_status == "paid"` and `client_reference_id`
  matches a real user.
- Idempotently upserts the `Subscription` row and sets `user.is_premium = True`,
  commits.
- Re-issues the session cookie via the existing
  `_login_response(token_for_user(user), url="/vip.html")` helper from
  `auth_routes.py` — same helper already used by login/register/Google OAuth —
  so the redirect to `/vip.html` lands the user already recognized as premium.
- If the session_id is missing/invalid/unpaid, redirects to `/premium.html` with
  no state change (mirrors the existing "not premium yet" offer redirect
  already used by `AuthMiddleware`).

### `POST /api/webhooks/stripe`

- Public route (Stripe can't send a session cookie), verified via
  `stripe.Webhook.construct_event` against `STRIPE_WEBHOOK_SECRET` — the
  signature check is the auth.
- This is the durable source of truth for subscription status over time,
  independent of whether the user's browser ever reaches `/billing/success`
  (closed tab, flaky network, renewal months later, involuntary churn):
  - `checkout.session.completed` — same upsert as `/billing/success` (handles
    the case where the webhook arrives before the browser redirect).
  - `customer.subscription.updated` — sync `status` / `current_period_end` on
    the matching `Subscription` row.
  - `customer.subscription.deleted` / `invoice.payment_failed` — set matching
    user's `is_premium = False`, update `Subscription.status`.
- Look up the `Subscription`/`User` by `stripe_customer_id` (present on every
  subscription event), not by cookie — there is no session on this request.
- Returns `{"status": "success"}` on 200; Stripe retries on non-2xx, so
  unexpected exceptions should surface as 500 rather than being swallowed.

## Frontend — `frontend/payant.html`

Replace the `plan-submit-btn` click handler's `mailto:` construction (around
line 1013) with:

```js
const response = await fetch('/api/checkout/create-session', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ plan: planKey }),  // "flex" | "essentiel" | "serenite", derived from the radio value
});
if (response.status === 401) { window.location.href = '/login'; return; }
if (response.status === 404) { /* Stripe not configured: leave existing mailto: as fallback */ }
const { url } = await response.json();
window.location.href = url;
```

The existing plan radio `value` attributes are French sentences ("Flex — 19,99 €
/ mois, sans engagement"), not slugs — add a `data-plan="flex"` attribute to each
`<label class="plan-option">` (three-line change) so the handler has a stable
key without touching the display text or scraping the string.

Keep the `mailto:` construction as a fallback path when the checkout endpoint
404s, so the page still works before Stripe keys are ever added to `.env`.

## Error handling

- Stripe API errors in `create-session` → 500 with a generic message (don't leak
  Stripe error internals to the client); log the real exception.
- Webhook signature failure → 400, no state change, matches Stripe's own
  recommended handling.
- Double-delivery of the same webhook event (Stripe's at-least-once delivery) —
  upserts are keyed by `Subscription.id` (the Stripe subscription id) so
  re-processing `checkout.session.completed` twice is a no-op, not a duplicate
  row.

## Testing

- Unit tests for `billing_routes.py` mock the `stripe` module (no real network
  calls in the suite) — check the plan-not-found 400, the unauthenticated 401,
  the 404-when-unconfigured behavior, and webhook signature-failure 400.
- No new fixture files needed (this isn't parser work); follows
  `.claude/rules/testing.md` conventions for the rest of `src/api` tests.
- Manual verification against the Stripe CLI's `stripe trigger` / test-mode
  Checkout is a follow-up once real test keys exist — out of scope for the
  automated suite.
