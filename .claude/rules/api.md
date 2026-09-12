---
paths:
  - "src/api/**"
  - "frontend/**"
---

## API

`src/api/main.py`: FastAPI app serving both the JSON API (`/listings*`, `/provinces`, `/stats`, `/history/{id}`) and the static frontend (mounted at `/static`, plus explicit routes per page under `frontend/`). `AuthMiddleware` gates everything except `PUBLIC_PATHS`/`PUBLIC_PATH_PREFIXES` (login, health, `/listings*`, and a few public map pages) behind a signed session cookie (`src/api/auth.py`); static `.html` files reached via `/static/...` are checked against the same public-path list so that route doesn't bypass the login gate. Because `/listings*` is public, every query parameter there needs an explicit range: SQLite reads `LIMIT -1` as "no limit", so a `limit` without `ge=1` is a full-catalogue dump for anyone.

## Public demo contract

`/listings*` and `/stats` stay reachable without a session because the shop window (`/test`, `/payant.html`) calls them, but an anonymous caller gets a sample, not the catalogue. Three rules, and each one is useless without the other two:

- `_public_limit` caps the page at `PUBLIC_DEMO_LIMIT` (12) **and** `_public_offset` forces `offset` to 0. A cap alone is defeated by looping over the following pages, 12 rows at a time.
- `_listing_to_response` blanks `_CONTACT_FIELDS` **and** `_PRECIOUS_FIELDS` (`address`, `latitude`, `longitude`, `url`) for anonymous callers. `url` is the one that matters most: it is the source of each row, so handing it over hands over the aggregation work itself. Province/district, price, contract and photos stay — that is what a shop window needs.
- `/stats` answers anonymous callers with `PublicStatsResponse` (the five counters the hero shows) instead of `StatsResponse`. The omitted fields describe the *activity* — geographic coverage, refresh rhythm, scan frequency, churn — which is competitor intelligence, not a selling point.

`RateLimitMiddleware` (`src/api/rate_limit.py`) closes the loop: bounded parameters decide what one request returns, never how many requests follow. 60 req/min per IP anonymous, 300 authenticated (the logged-in frontend paginates the whole catalogue at 500/page), applied to `_RATE_LIMITED_PREFIXES` only — `/health` is polled once a second at startup by the launcher, and `/login` keeps its own counter in `src/api/auth.py`. Counters live in a module-level dict: correct for this single-worker deployment, and each worker would hold its own count (multiplying the ceiling) if that ever changes.

`frontend/test.html` must stay aligned with `PUBLIC_DEMO_LIMIT` (`DEMO_SAMPLE_SIZE`) and must not paginate: it derives no volume from the sample any more, and reads the hero's "new today" figure from `/stats` instead.
