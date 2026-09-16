---
paths:
  - "tests/**"
---

## Testing

Regression tests (`tests/test_parser_regression.py`, `tests/test_bugfix_regression.py`, `tests/test_audit_regression.py`, `tests/test_contact_description.py`) parse real captured HTML fixtures (`tests/fixtures/*.html`) and compare against golden JSON snapshots (`tests/fixtures/*.golden.json`). The list-page fixture without `__NEXT_DATA__` and its relabeled variant are derived from `list_page.html` at test time, not stored. When a fixture-based test fails after a parser change, check whether the difference is a genuine bug fix before updating the golden file — fix genuine bugs surfaced by a new engine instead of reproducing them (this is how the BeautifulSoup → Scrapling migration was handled).

Convention: one test per fixed defect, named after the behaviour it protects, with a docstring saying what broke and why. API tests use the in-memory database of `conftest.py` (`session`, `client`); module-level counters (rate limit, login lock, `/stats` cache) are reset between tests by the autouse fixture. Async tests carry `@pytest.mark.asyncio` (`asyncio_mode = strict` in `pyproject.toml`). Nothing in `tests/` may touch `renthub.db`.

Frontend changes are verified in a headless browser rather than by unit tests: load every page, assert zero console errors, compare screenshots, exercise modal/lightbox/pagination/map filters (Playwright, see `.claude/rules/frontend.md`).
