---
paths:
  - "src/api/**"
  - "frontend/**"
---

## API

`src/api/main.py`: FastAPI app serving both the JSON API (`/listings*`, `/provinces`, `/stats`, `/history/{id}`) and the static frontend (mounted at `/static`, plus explicit routes per page under `frontend/`). `AuthMiddleware` gates only `PROTECTED_PATHS` (`/` = `index.html`, and `/vip.html`) behind a signed session cookie (`src/api/auth.py`); everything else — other pages, API routes, assets — is public. Files reached via `/static/...` are normalised and checked against the same list (case-insensitively, NTFS oblige) so that route doesn't bypass the login gate. Because `/listings*` is public, every query parameter there needs an explicit range: SQLite reads `LIMIT -1` as "no limit", so a `limit` without `ge=1` is a full-catalogue dump for anyone.

## Public data contract

`/listings*` and `/stats` are reachable without a session because the shop window (`/test`, `/payant.html`) and the public map pages (`carte-*.html`) call them. An anonymous caller gets the **full catalogue, paginated, with GPS coordinates** (the maps need them), but `_listing_to_response` blanks `_CONTACT_FIELDS` and `_PRECIOUS_FIELDS` (`address`, `url`). `url` is the one that matters most: it is the source of each row, so handing it over hands over the aggregation work itself. `/stats` answers anonymous callers with `PublicStatsResponse` (the five counters the hero shows) instead of `StatsResponse`: the omitted fields describe the *activity* — coverage, refresh rhythm, churn — which is competitor intelligence, not a selling point.

`RateLimitMiddleware` (`src/api/rate_limit.py`) closes the loop: bounded parameters decide what one request returns, never how many requests follow. 60 req/min per IP anonymous, 300 authenticated (the logged-in frontend paginates the whole catalogue at 500/page), applied to `_RATE_LIMITED_PREFIXES` only — `/health` is polled once a second at startup by the launcher, and `/login` keeps its own counter in `src/api/auth.py`. Counters live in a module-level dict: correct for this single-worker deployment, and each worker would hold its own count (multiplying the ceiling) if that ever changes.

`frontend/test.html` requests its own sample size (`DEMO_SAMPLE_SIZE`) and reads the hero's "new today" figure from `/stats`.
