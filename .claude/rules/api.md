---
paths:
  - "src/api/**"
  - "frontend/**"
---

## API

`src/api/main.py`: FastAPI app serving both the JSON API (`/listings*`, `/provinces`, `/stats`, `/history/{id}`) and the static frontend (mounted at `/static`, plus explicit routes per page under `frontend/`). `AuthMiddleware` gates only `PROTECTED_PATHS` (a dict: `/` = `index.html` for any signed-in account, `/vip.html` for premium only) behind a signed session cookie (`src/api/auth.py`); everything else — other pages, API routes, assets — is public. A signed-in non-premium account asking for a premium page is redirected to `/premium.html` (the offer), not to `/login`.

## Accounts

Three levels share one cookie, `<user_id>.<role>.<expiry>.<hmac>` (`src/api/auth.py::read_session_token`): anonymous, `user` (free account), `premium`/`admin`. The admin is `SITE_USERNAME`/`SITE_PASSWORD` from `.env` (user id 0, no row in `users`) and counts as premium; `create_session_token()` with no argument still mints an admin token, which is what the older tests rely on. The token is verified without touching the database, so a premium upgrade only takes effect at the next login unless the route performing it re-issues the cookie via `_login_response(token_for_user(user))`.

Routes: `GET/POST /login` (email+password of a `users` row, or the admin pair — the `username` form field carries both), `GET/POST /register`, `GET /auth/google` → `GET /auth/google/callback` (authlib; 404 and no button while `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` are empty), `POST /logout`, `GET /me`. `/register` shares the `/login` failure counter so it cannot be used to enumerate emails. Passwords are bcrypt (`bcrypt` directly, not passlib, which breaks on bcrypt ≥ 4.1); Google accounts are matched by `google_sub`, then by email only when Google reports it verified — an unverified email neither links nor creates an account (`get_or_create_google_user` returns `None`, the callback shows an error), since `users.email` is unique and an unproven address would otherwise reserve it. `SessionMiddleware` exists solely for authlib's OAuth `state`, on a separate `oauth_state` cookie so it never collides with the `session` cookie.

`_is_authenticated` (any account) only decides the rate-limit budget; `_is_premium` decides whether `_listing_to_response` and `/stats` return the sold fields. A free account gets exactly the anonymous view of the data. Files reached via `/static/...` are normalised and checked against the same list (case-insensitively, NTFS oblige) so that route doesn't bypass the login gate. Because `/listings*` is public, every query parameter there needs an explicit range: SQLite reads `LIMIT -1` as "no limit", so a `limit` without `ge=1` is a full-catalogue dump for anyone.

## Public data contract

`/listings*` and `/stats` are reachable without a session because the shop window (`/test`, `/payant.html`) and the public map pages (`carte-*.html`) call them. An anonymous caller gets the **full catalogue, paginated, with GPS coordinates** (the maps need them), but `_listing_to_response` blanks `_CONTACT_FIELDS` and `_PRECIOUS_FIELDS` (`address`, `url`). `url` is the one that matters most: it is the source of each row, so handing it over hands over the aggregation work itself. `/stats` answers anonymous callers with `PublicStatsResponse` (the five counters the hero shows) instead of `StatsResponse`: the omitted fields describe the *activity* — coverage, refresh rhythm, churn — which is competitor intelligence, not a selling point.

`RateLimitMiddleware` (`src/api/rate_limit.py`) closes the loop: bounded parameters decide what one request returns, never how many requests follow. 60 req/min per IP anonymous, 300 authenticated (the logged-in frontend paginates the whole catalogue at 500/page), applied to `_RATE_LIMITED_PREFIXES` only — `/health` is polled once a second at startup by the launcher, and `/login` keeps its own counter in `src/api/auth.py`. Counters live in a module-level dict: correct for this single-worker deployment, and each worker would hold its own count (multiplying the ceiling) if that ever changes.

Both counters are keyed by `_client_key`. Behind a reverse proxy (the Cloudflare tunnel of the launcher, Render, nginx) `request.client.host` is the proxy's address, so every visitor would share one budget and one login lock; `TRUSTED_PROXY_HOPS=N` (`src/config.py`) makes `_client_key` read the N-th address from the *end* of `X-Forwarded-For` instead. Don't rely on uvicorn's `--forwarded-allow-ips="*"` for this: it takes the *first* address, which the client writes itself. With hops > 0, `X-Forwarded-Proto: https` is also trusted to build the Google OAuth `redirect_uri`.

`frontend/test.html` requests its own sample size (`DEMO_SAMPLE_SIZE`) and reads the hero's "new today" figure from `/stats`.
