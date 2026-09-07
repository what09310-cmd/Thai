# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

RentHub Tracker: a scraper + tracker + API for rental listings on renthub.in.th (Thailand). It periodically scrapes listing and detail pages, diffs them against a database to detect new/updated/price-changed/removed listings, and serves the result through a FastAPI backend with a static multi-page frontend (one HTML page per city/province map).

## Commands

Windows venv is at `venv/` (activate with `venv\Scripts\activate`).

Run the API (also defined in `.claude/launch.json`):
```
venv\Scripts\python.exe -m uvicorn src.api.main:app --host 127.0.0.1 --port 8000
```

Run a scan:
```
python scripts/run_scraper.py                       # full scan (list + detail pages, all provinces)
python scripts/run_scraper.py --max-pages 5          # smoke test, limited pages
python scripts/run_scraper.py --province bangkok     # single province
python scripts/run_scraper.py --one-month-only       # keep only listings with a monthly contract
python scripts/run_scraper.py --no-scrape-details    # list pages only, skip detail pages (faster)
python scripts/run_scraper.py --include-locations    # also scrape /short-term-rental/<province> pages
python scripts/run_scraper.py --update-provinces     # refresh the provinces table only
python scripts/run_scraper.py --init-db-only         # create tables and exit
python scripts/run_scraper.py --export csv --output ./exports
```
`--max-pages` or `--province` or `--one-month-only` disable removed-listing detection for that run since the collection is partial (see `run_scraper.py::main`).

Tests (pytest, run from repo root so `src.*` imports resolve):
```
pytest
pytest tests/test_parser_regression.py
pytest tests/test_parser_regression.py::test_name -v
```

Index migration (run once on an existing database — `create_all` never adds indexes to a table that already exists):
```
python scripts/migrate_add_indexes.py
```

Docker Compose profiles (Postgres-backed): `docker-compose --profile scraper up`, `docker-compose --profile scheduler up`; the `api` service has no profile and starts by default with `docker-compose up`. All three services read `.env` via `env_file` — the API refuses to boot while `SECRET_KEY`/`SITE_PASSWORD` still hold their `.env.example` values.

## Architecture

**Pipeline** (`scripts/run_scraper.py` orchestrates all of it):
1. **List scrape** (`src/scraper/list_scraper.py`) — paginates province listing pages, yields `ListingRaw`.
2. **Detail scrape** (`src/scraper/detail_scraper.py`) — fetches individual listing pages, batched (`BATCH_SIZE = 20`). Only re-scraped when `detail_scraped_at` is older than `settings.detail_refresh_days` (7 days) or never scraped — `source_id` (the site's "Listing no") is the only reliable marker of a successful detail scrape, not `description` (which is regenerated locally every scan).
3. **Merge** — `ListingRaw` + `ListingDetail` → `ListingFull` (`src/models/schemas.py`), detail fields enrich but never overwrite raw fields.
4. **Persist & diff** (`src/tracker/change_detector.py`) — `upsert_listing` creates/updates `Listing` rows and appends `ListingHistory` entries (`NEW`/`UPDATED`/`PRICE_CHANGED`/`REMOVED`/`REACTIVATED`). `mark_removed_listings` flags listings absent from the current scan after `removed_after_missing_scans` (default 3) consecutive misses — only runs on a full, unfiltered scan.

**Parsing** (`src/parser/list_parser.py`, `src/parser/detail_parser.py`, via Scrapling `Selector`): the primary path extracts structured data from the page's embedded `__NEXT_DATA__` JSON; an HTML fallback (used when that JSON is absent) uses Scrapling's adaptive element matching (`adaptive=True`, fingerprints persisted in `.scrapling/`, identifier `"listing-card"` shared site-wide). The networking layer (`src/scraper/http_client.py`, httpx-based) deliberately does not use Scrapling's stealth/anti-bot fetchers — bypassing anti-bot protection is out of scope for this project.

**Database** (`src/database/models.py`, SQLAlchemy 2.0 declarative): `Listing` (core table, ~40 columns spanning location/pricing/contract-length/amenities/contact/tracking), `ListingImage`, `ListingHistory` (audit trail), `Province`, `ScanLog` (one row per scan run), `RentalRequest` (inbound demand form from the frontend). Same models run against SQLite (dev, `renthub.db`) or Postgres (`docker-compose.yml`) — `src/database/session.py` branches connection-pool args on the URL scheme.

**Contract-length filtering** (`src/filters/contract.py`): `has_monthly_contract` is derived strictly from the structured `Contract monthly` field (`"true"`/`"false"`/`"unknown"`), never from free-text description. `has_short_term_contract` additionally considers the 3-month/6-month structured fields — used to decide whether a `--include-locations` listing (which only comes with a Short-Term Rental Contract flag) is worth keeping.

**Description normalization** (`src/normalizers/contact_description.py`): `build_contact_description` regenerates a listing's displayed description from structured fields every scan (self-healing) and from `scripts/regenerate_contact_descriptions.py` for backfills. Format is fixed: `Deposit: …`, `Electric price: …` (omitted if `"Please contact"`), `Air Conditioner : YES/NO` — no phone/LINE/other free text; those live in their own `phone`/`line_id`/`whatsapp` columns and are rendered separately by the frontend.

**Scan invariants** — a scan that changes nothing must write no history. Three rules keep it that way, and breaking any one of them was worth hundreds of bogus rows in `listing_history`:
- `apply_contract_fields` (`src/tracker/change_detector.py`) only lets a source overwrite the 1/3/6-month contracts with `None` when it actually describes the short-term offer (`ListingRaw.from_structured_list`, set from `shortTerm.oneMonth` in the JSON). Pages under `/en/short-term-rental/<slug>` carry `price.monthly` but no contract block, so without the guard they erased contracts that a `/browse/short-term-monthly` pass had learned.
- `compute_content_hash` hashes the **persisted row**, after all fields are applied — never the scraped `ListingFull`. `description` and `amenities` are only populated when the detail page was read in that run, so hashing the scraped object made the hash flip every time the `detail_refresh_days` window rolled over.
- `collect_unique_listings` (`scripts/run_scraper.py`) keeps only the first occurrence of each slug. RentHub repeats listings across pagination pages (~19% of cards over a two-page sample); upserting a slug twice in one scan made each pass undo the other.

**API** (`src/api/main.py`): FastAPI app serving both the JSON API (`/listings*`, `/provinces`, `/stats`, `/history/{id}`) and the static frontend (mounted at `/static`, plus explicit routes per page under `frontend/`). `AuthMiddleware` gates everything except `PUBLIC_PATHS`/`PUBLIC_PATH_PREFIXES` (login, health, `/listings*`, and a few public map pages) behind a signed session cookie (`src/api/auth.py`); static `.html` files reached via `/static/...` are checked against the same public-path list so that route doesn't bypass the login gate. Because `/listings*` is public, every query parameter there needs an explicit range: SQLite reads `LIMIT -1` as "no limit", so a `limit` without `ge=1` is a full-catalogue dump for anyone.

**Public demo contract** — `/listings*` and `/stats` stay reachable without a session because the shop window (`/test`, `/payant.html`) calls them, but an anonymous caller gets a sample, not the catalogue. Three rules, and each one is useless without the other two:
- `_public_limit` caps the page at `PUBLIC_DEMO_LIMIT` (12) **and** `_public_offset` forces `offset` to 0. A cap alone is defeated by looping over the following pages, 12 rows at a time.
- `_listing_to_response` blanks `_CONTACT_FIELDS` **and** `_PRECIOUS_FIELDS` (`address`, `latitude`, `longitude`, `url`) for anonymous callers. `url` is the one that matters most: it is the source of each row, so handing it over hands over the aggregation work itself. Province/district, price, contract and photos stay — that is what a shop window needs.
- `/stats` answers anonymous callers with `PublicStatsResponse` (the five counters the hero shows) instead of `StatsResponse`. The omitted fields describe the *activity* — geographic coverage, refresh rhythm, scan frequency, churn — which is competitor intelligence, not a selling point.

`RateLimitMiddleware` (`src/api/rate_limit.py`) closes the loop: bounded parameters decide what one request returns, never how many requests follow. 60 req/min per IP anonymous, 300 authenticated (the logged-in frontend paginates the whole catalogue at 500/page), applied to `_RATE_LIMITED_PREFIXES` only — `/health` is polled once a second at startup by the launcher, and `/login` keeps its own counter in `src/api/auth.py`. Counters live in a module-level dict: correct for this single-worker deployment, and each worker would hold its own count (multiplying the ceiling) if that ever changes.

`frontend/test.html` must stay aligned with `PUBLIC_DEMO_LIMIT` (`DEMO_SAMPLE_SIZE`) and must not paginate: it derives no volume from the sample any more, and reads the hero's "new today" figure from `/stats` instead.

**Testing**: regression tests (`tests/test_parser_regression.py`, `tests/test_bugfix_regression.py`, `tests/test_contact_description.py`) parse real captured HTML fixtures (`tests/fixtures/*.html`) and compare against golden JSON snapshots (`tests/fixtures/*.golden.json`). When a fixture-based test fails after a parser change, check whether the difference is a genuine bug fix before updating the golden file — see the note on the BeautifulSoup→Scrapling migration below.

## Project-specific notes

- The parser was migrated from BeautifulSoup to Scrapling; if a migration or engine swap changes output on a golden test, treat it as a signal to investigate the underlying HTML rather than assuming the old output was correct — fix genuine bugs surfaced by the new engine instead of reproducing them.
- `renthub.db` backups (`renthub.db.bak-*`) are created ad hoc before destructive one-off scripts (e.g. `scripts/regenerate_contact_descriptions.py`) — follow that convention (`cp renthub.db renthub.db.bak-$(date +%Y%m%d-%H%M%S)`) before running scripts that bulk-mutate the database.
