---
paths:
  - "scripts/**"
---

## Database backup convention

`renthub.db` backups (`renthub.db.bak-*`) are created ad hoc before destructive scripts (`scripts/migrate.py`, `scripts/rescrape_details.py`) — follow that convention (`cp renthub.db renthub.db.bak-$(date +%Y%m%d-%H%M%S)`) before running scripts that bulk-mutate the database.

These backups accumulate at the repo root (13-18 MB each) and are never cleaned up automatically. Run `python scripts/prune_db_backups.py` (keeps the 5 most recent by default, `--keep N` to change, `--dry-run` to preview) occasionally, e.g. after finishing a batch of destructive scripts.

## What lives here

- `run_scraper.py` — the scan (see scraper-pipeline.md); `scheduler.py` runs it on an interval (docker "scheduler" service).
- `migrate.py` — the only schema migration tool: idempotent, adds missing columns/indexes, dedupes images, recomputes `content_hash` after a `HASH_FIELDS` change, `--drop-orphans` for tables without a model. Don't add `migrate_add_*` one-offs or `ALTER TABLE` at API startup; extend this script.
- `rescrape_details.py --fields contact|amenities|coords|rooms [--only-missing]` — the only re-scrape tool; don't write another `refresh_*`/`refix_*` loop, add a field group here.
- `migrate_sqlite_to_postgres.py` — initial copy to Postgres; reads `POSTGRES_URL` from the environment (never hardcode a database URL: one was committed to this public repo once).
- `set_premium.py`, `verify_line_ids.py`, `verify_whatsapp_numbers.py`, `filter_problematic_images*.py`, `geocode_*.py` — one-off tools, dependencies in `requirements-tools.txt`. `geocode_addresses.py` produces `frontend/address_coords.json`, optional: the pages fetch it once and fall back to district centres when absent.
