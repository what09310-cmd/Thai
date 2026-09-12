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

## Where the detailed rules live

Module-specific architecture, invariants, and conventions are scoped by path in `.claude/rules/` instead of loaded here on every session:

- `.claude/rules/scraper-pipeline.md` — pipeline stages, parsing, contract filtering, description normalization, scan invariants (`src/scraper/`, `src/parser/`, `src/tracker/`, `src/normalizers/`, `src/filters/`)
- `.claude/rules/database.md` — models and schema (`src/database/`)
- `.claude/rules/api.md` — API, auth, public demo contract, rate limiting (`src/api/`)
- `.claude/rules/testing.md` — regression test conventions (`tests/`)
- `.claude/rules/scripts.md` — one-off/destructive script conventions (`scripts/`)

Historical/migration notes that don't need to load automatically live in `docs/migration-notes.md`.
