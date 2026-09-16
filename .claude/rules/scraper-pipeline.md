---
paths:
  - "src/scraper/**"
  - "src/parser/**"
  - "src/tracker/**"
  - "src/normalizers/**"
  - "src/filters/**"
---

## Pipeline

`scripts/run_scraper.py` orchestrates all of it:

1. **List scrape** (`src/scraper/list_scraper.py`) — paginates province listing pages, yields `ListingRaw`.
2. **Detail scrape** (`src/scraper/detail_scraper.py`) — fetches individual listing pages, batched (`BATCH_SIZE = 20`). Only re-scraped when `detail_scraped_at` is older than `settings.detail_refresh_days` (7 days) or never scraped — `source_id` (the site's "Listing no") is the only reliable marker of a successful detail scrape, not `description` (which is regenerated locally every scan).
3. **Merge** — `ListingRaw` + `ListingDetail` → `ListingFull` (`src/models/schemas.py`), detail fields enrich but never overwrite raw fields.
4. **Persist & diff** (`src/tracker/change_detector.py`) — `upsert_listing` creates/updates `Listing` rows and appends `ListingHistory` entries (`NEW`/`UPDATED`/`PRICE_CHANGED`/`REMOVED`/`REACTIVATED`). `mark_removed_listings` flags listings absent from the current scan after `removed_after_missing_scans` (default 3) consecutive misses — only runs on a full, unfiltered scan.

## Parsing

`src/parser/list_parser.py`, `src/parser/detail_parser.py`, via Scrapling `Selector`: the primary path extracts structured data from the page's embedded `__NEXT_DATA__` JSON; an HTML fallback (used when that JSON is absent) uses Scrapling's adaptive element matching (`adaptive=True`, fingerprints persisted in `.scrapling/`, identifier `"listing-card"` shared site-wide). The networking layer (`src/scraper/http_client.py`, httpx-based) deliberately does not use Scrapling's stealth/anti-bot fetchers — bypassing anti-bot protection is out of scope for this project.

## Contract-length filtering

`src/filters/contract.py`: `has_monthly_contract` is derived strictly from the structured `Contract monthly` field (`"true"`/`"false"`/`"unknown"`), never from free-text description. `has_short_term_contract` additionally considers the 3-month/6-month structured fields — used to decide whether a `--include-locations` listing (which only comes with a Short-Term Rental Contract flag) is worth keeping.

## Description normalization

`src/normalizers/contact_description.py`: `build_contact_description` regenerates a listing's displayed description from structured fields every scan (self-healing) and from `scripts/rescrape_details.py` for backfills. Format is fixed: `Deposit: …`, `Electric price: …` (omitted if `"Please contact"`), `Air Conditioner : YES/NO` — no phone/LINE/other free text; those live in their own `phone`/`line_id`/`whatsapp` columns and are rendered separately by the frontend.

## Scan invariants

A scan that changes nothing must write no history. Three rules keep it that way, and breaking any one of them was worth hundreds of bogus rows in `listing_history`:

- `apply_contract_fields` (`src/tracker/change_detector.py`) only lets a source overwrite the 1/3/6-month contracts with `None` when it actually describes the short-term offer (`ListingRaw.from_structured_list`, set from `shortTerm.oneMonth` in the JSON). Pages under `/en/short-term-rental/<slug>` carry `price.monthly` but no contract block, so without the guard they erased contracts that a `/browse/short-term-monthly` pass had learned.
- `compute_content_hash` hashes the **persisted row**, after all fields are applied — never the scraped `ListingFull`. `description` and `amenities` are only populated when the detail page was read in that run, so hashing the scraped object made the hash flip every time the `detail_refresh_days` window rolled over.
- `collect_unique_listings` (`scripts/run_scraper.py`) keeps only the first occurrence of each slug. RentHub repeats listings across pagination pages (~19% of cards over a two-page sample); upserting a slug twice in one scan made each pass undo the other.

## Project-specific notes

The parser was migrated from BeautifulSoup to Scrapling; if a migration or engine swap changes output on a golden test, treat it as a signal to investigate the underlying HTML rather than assuming the old output was correct — fix genuine bugs surfaced by the new engine instead of reproducing them.

The detail parser reads `__NEXT_DATA__` (plus JSON-LD for coordinates and explicit `line.me`/`wa.me`/`mailto` links for contacts) and nothing else: the former HTML-table/regex fallbacks only ever ran on pages that are not listings. A page whose JSON has no `listing` key is RentHub's home page served for a withdrawn listing and comes back as `ListingDetail(page_gone=True)`, like a 404: it sets `detail_scraped_at` without touching known fields and is not counted as a scan error.

`content_hash` covers `HASH_FIELDS` (`change_detector.py`): changing that list requires `python scripts/migrate.py`, which recomputes the hash on every row — otherwise the next scan writes one `UPDATED` per listing. Phase 3 preloads all known listings with `load_existing` (one query, images included) and `upsert_listing(..., existing)`; `mark_removed_listings` works with bulk `UPDATE`s.
