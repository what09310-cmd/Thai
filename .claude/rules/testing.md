---
paths:
  - "tests/**"
---

## Testing

Regression tests (`tests/test_parser_regression.py`, `tests/test_bugfix_regression.py`, `tests/test_contact_description.py`) parse real captured HTML fixtures (`tests/fixtures/*.html`) and compare against golden JSON snapshots (`tests/fixtures/*.golden.json`). When a fixture-based test fails after a parser change, check whether the difference is a genuine bug fix before updating the golden file — see `docs/migration-notes.md` for the note on the BeautifulSoup→Scrapling migration.
