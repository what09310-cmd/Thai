# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

RentHub Tracker: a scraper + tracker + API for rental listings on renthub.in.th (Thailand). It periodically scrapes listing and detail pages, diffs them against a database to detect new/updated/price-changed/removed listings, and serves the result through a FastAPI backend with a static multi-page frontend (one HTML page per city/province map).

## Commands

Windows venv is at `venv/` (activate with `venv\Scripts\activate`). `pip install -r requirements.txt` installs everything; each deployment installs only its own file (`requirements-api.txt` on Render, `requirements-scraper.txt` in the daily GitHub Actions scan and docker, `requirements-tools.txt` for the one-off scripts, `requirements-dev.txt` for tests/lint). Pins are the versions the venv runs the test suite on; Python is 3.12 everywhere (`runtime.txt`, `PYTHON_VERSION` on Render, Dockerfile, workflow).

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
python scripts/run_scraper.py --init-db-only         # create tables and exit
python scripts/run_scraper.py --export csv --output ./exports
```
`--max-pages` or `--province` or `--one-month-only` disable removed-listing detection for that run since the collection is partial (see `run_scraper.py::main`).

Tests and lint (run from repo root so `src.*` imports resolve; config in `pyproject.toml`):
```
pytest
pytest tests/test_parser_regression.py::test_name -v
ruff check src scripts tests
```

Schema migration of an existing database (`create_all` never adds a column or index to a table that already exists; idempotent, SQLite and Postgres, back the database up first):
```
python scripts/migrate.py --dry-run
python scripts/migrate.py [--drop-orphans]
```

Render (service "Thai Month", branch `landing-page-test`) builds with `pip install -r requirements-api.txt`: any new runtime dependency of `src/api/` must be added there (and to the venv) or the deploy fails at import time.

Docker Compose profiles (Postgres-backed): `docker-compose --profile scraper up`, `docker-compose --profile scheduler up`; the `api` service has no profile and starts by default with `docker-compose up`. All three services read `.env` via `env_file` — the API refuses to boot while `SECRET_KEY`/`SITE_PASSWORD` still hold their `.env.example` values. Unknown keys in `.env` are ignored (a removed setting never blocks startup).

## Where the detailed rules live

Module-specific architecture, invariants, and conventions are scoped by path in `.claude/rules/` instead of loaded here on every session:

- `.claude/rules/scraper-pipeline.md` — pipeline stages, parsing, contract filtering, description normalization, scan invariants (`src/scraper/`, `src/parser/`, `src/tracker/`, `src/normalizers/`, `src/filters/`)
- `.claude/rules/database.md` — models and schema (`src/database/`)
- `.claude/rules/api.md` — API, auth, public demo contract, rate limiting (`src/api/`)
- `.claude/rules/frontend.md` — the pages and the shared `frontend/js/`, `frontend/css/` modules (`frontend/`)
- `.claude/rules/testing.md` — regression test conventions (`tests/`)
- `.claude/rules/scripts.md` — one-off/destructive script conventions (`scripts/`)

`docs/audit-2026-09-16.md` is the audit that shaped the current layout (what was removed, merged, and why).

## Large files — don't read in full without a reason

- `frontend/index.html`, `premium.html`, `test.html`, `carte-thailande.html` are 600-1 900 lines each. Their shared logic lives in `frontend/js/*.js` and `frontend/css/*.css` (see `.claude/rules/frontend.md`): to fix a behaviour shared by several pages, read the module, not the pages.
- `frontend/test.html` is the public landing page (the "vitrine"), served on `/test` — not a draft.
- `tests/fixtures/*.html` and `tests/fixtures/*.golden.json` are raw captured pages and expected-parse snapshots (up to ~340 KB each), used only by the parser regression tests in `tests/`. Only open them when working on `src/parser/` or the tests that reference them.
- `frontend/img/provinces/*.webp` are static province photos (640 px, q80) — binary assets, never source of truth for anything; don't attempt to read them as text.
- `renthub.db.bak-*` (if present at the repo root) are manual SQLite backups, not part of the app; ignore them entirely.
