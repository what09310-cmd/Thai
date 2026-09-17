# Stripe payment-first signup — design

Status: draft, pending review. Author: Claude (session 2026-09-17).

## Goal

Today (see `2026-09-17-stripe-subscriptions-design.md`, already implemented),
`POST /api/checkout/create-session` requires an existing logged-in `User`
before Stripe is ever reached: an anonymous visitor on `payant.html` is
bounced to `/login?next=...` first, and only comes back to start Checkout
after authenticating.

This changes the order: a visitor can pay **before** having an account.
Stripe collects the paying email itself; account creation (password or
Google) happens on the return page, and only *after* that step succeeds does
the subscription get attached to a `User` and a session cookie get issued.

## Non-goals

- No change to plan names/prices, the three-plan picker UI, or the
  `Subscription` table schema.
- No Stripe Customer Portal, proration, or plan-switch UI (unchanged from the
  original design).
- The existing compte-first path (`/login` → `payant.html` autocheckout,
  `frontend/payant.html:954-1003`) keeps working unmodified for a user who is
  already logged in when they click a plan — this design only changes the
  anonymous path.

## Data model changes

`Subscription.user_id` becomes nullable (currently `nullable=False`,
`2026-09-17-stripe-subscriptions-design.md`'s schema). A row can now exist in
a "paid, not yet linked to an account" state between the webhook firing and
the user finishing signup on the return page. `User.is_premium` is only set
once `user_id` is populated — an unlinked `Subscription` grants no access to
anyone. Migrated via `scripts/migrate.py` (`--dry-run` first), per
`.claude/rules/database.md`.

## Config / routes touched

No new settings. Same `_stripe_configured()` gate as before.

### `POST /api/checkout/create-session` (`src/api/billing_routes.py:53`)

- Drops the `info is None or info.user_id == 0` requirement — reachable
  anonymously.
- Drops `client_reference_id` and `customer_email` from the
  `stripe.checkout.Session.create(...)` call (no user to attribute yet).
  Instead passes `customer_creation="always"` and leaves Stripe's own email
  field enabled on the Checkout page (Stripe collects it).
- If a session *is* present and belongs to a real user (the existing
  compte-first path), keep passing `client_reference_id`/`customer_email` as
  today — an already-authenticated user's checkout still attaches
  immediately at `/billing/success`, no behavior change for that path.
- `success_url` unchanged in shape:
  `<origin>/billing/success?session_id={CHECKOUT_SESSION_ID}`.

### `POST /api/webhooks/stripe` (`billing_routes.py:149`)

- `checkout.session.completed`: when `client_reference_id` is present (logged-in
  path), behavior is unchanged — upsert with that `user_id` immediately.
- When `client_reference_id` is absent (anonymous path), create the
  `Subscription` row with `user_id=NULL`, `stripe_customer_id` from the
  event, status from the Stripe subscription. This is the row the return page
  and `/api/billing/finalize` will later attach a user to.
- `customer.subscription.updated`/`deleted`/`invoice.payment_failed`: look up
  the `Subscription` row as today; if `user_id` is still NULL, update
  `status` only (nothing to flip `is_premium` on yet).

### `GET /billing/success` (`billing_routes.py:119`)

- No longer creates/updates anything or issues a cookie by itself.
- Retrieves the Checkout Session from Stripe, confirms `payment_status ==
  "paid"`.
- If `client_reference_id` is present (compte-first path): unchanged
  behavior — upsert, re-issue cookie, redirect to `/vip.html`.
- If absent (payment-first path): redirect to
  `/finaliser-compte.html?session_id=<id>` — no account/session touched yet.

### `POST /api/billing/finalize` (new)

Single endpoint that turns a paid-but-unlinked session into a real account
and an active subscription. Never trusts the client past `session_id`:

1. Re-retrieves the Checkout Session from Stripe using `session_id` and
   re-checks `payment_status == "paid"` server-side — the same check
   `/billing/success` already does, repeated here because this is the
   endpoint that actually grants access, and `session_id` alone (leaked via
   browser history, a forwarded link, a shared screen) must not be
   sufficient.
2. Looks up the `Subscription` row by `stripe_customer_id` from that session
   (created by the webhook in step above); 404 if the webhook hasn't landed
   yet (client retries — webhook delivery is normally sub-second but not
   instant).
3. Branch on how identity is proven:
   - **Password path** — body `{"session_id", "email", "password"}`. If
     `email` (defaults to the Stripe-collected email, or a different one the
     user typed after an email-taken conflict) is not yet registered,
     `register_user()` creates it (reusing `src/api/auth.py`'s existing
     password validation). If it *is* already registered, this path 409s —
     password-path can only create a fresh account, never silently claim an
     existing one (that requires the Google path below, which proves
     ownership via OAuth instead of a client-supplied string).
   - **Google path** — reuses `/auth/google` (`auth_routes.py:398`) with
     `session_id` threaded through `request.session["billing_session_id"]`
     the same way `post_login_next` already works; `google_callback`
     (`auth_routes.py:414`) checks for it, and — after Google verifies the
     email as it already does today — calls the same finalize logic:
     `get_or_create_google_user()` (existing helper, unchanged) either
     matches an existing account by verified email or creates one.
4. Either path ends by setting `Subscription.user_id`, `User.is_premium =
   True`, committing, and issuing the session cookie via the existing
   `_login_response(token_for_user(user), url="/vip.html")` — identical to
   how the compte-first path already finishes.

### `frontend/finaliser-compte.html` (new page)

Mirrors `frontend/payant.html`'s existing auth-card styling
(`auth_routes.py`'s `_AUTH_PAGE` inline CSS as the closest reference for
matching visual style). Reads `session_id` from the query string.

- Calls a small read-only endpoint (or reuses `finalize`'s dry-run via a
  `GET`) to show the Stripe-collected email and a password field, plus a
  "Continuer avec Google" button — same two options as `/login`.
- On password submit: `POST /api/billing/finalize`. A 409 (email already
  registered) re-renders the same page with "cet email est déjà associé à un
  compte" and switches the primary field to let the visitor either type a
  different email and retry, or click "Se connecter avec Google" instead —
  matching what was agreed in brainstorming.
- On success: redirect to `/vip.html` (cookie already set by `finalize`).

## Error handling

- `finalize` called with an unpaid or unknown `session_id` → 400, no state
  change (mirrors `/billing/success`'s existing "not paid yet" handling).
- `finalize` called before the webhook has created the `Subscription` row →
  404 with a message the frontend can retry on ("le paiement est en cours de
  confirmation, réessayez dans un instant") rather than failing hard —
  webhook delivery racing the browser redirect is expected, not an error
  state.
- Password path against an already-registered email → 409 (see above);
  frontend switches to the conflict UI, does not retry with the same body.
- Google path where `get_or_create_google_user` returns `None` (Google
  reports the email unverified) → same existing error page
  `auth_routes.py:428` already renders for `/auth/google/callback`, reused
  as-is.

## Testing

- Unit tests for the new `finalize` branch logic in `billing_routes.py`,
  mocking `stripe.checkout.Session.retrieve` as the existing tests already
  do: paid+unlinked session creates a user and sets `is_premium`; unpaid
  session 400s; already-registered email on the password path 409s; second
  call with the same `session_id` (double submit) is idempotent (no second
  `User` row, no error) since `Subscription.id` is the upsert key.
- Webhook test: `checkout.session.completed` with no `client_reference_id`
  creates a `Subscription` with `user_id=NULL` rather than raising on a
  missing user.
- Existing compte-first tests (`client_reference_id` present at checkout
  creation) must keep passing unchanged — this is an additive path, not a
  replacement.
