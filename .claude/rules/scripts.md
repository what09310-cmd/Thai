---
paths:
  - "scripts/**"
---

## Database backup convention

`renthub.db` backups (`renthub.db.bak-*`) are created ad hoc before destructive one-off scripts (e.g. `scripts/regenerate_contact_descriptions.py`) — follow that convention (`cp renthub.db renthub.db.bak-$(date +%Y%m%d-%H%M%S)`) before running scripts that bulk-mutate the database.

These backups accumulate at the repo root (13-18 MB each) and are never cleaned up automatically. Run `python scripts/prune_db_backups.py` (keeps the 5 most recent by default, `--keep N` to change, `--dry-run` to preview) occasionally, e.g. after finishing a batch of destructive scripts.
