---
paths:
  - "scripts/**"
---

## Database backup convention

`renthub.db` backups (`renthub.db.bak-*`) are created ad hoc before destructive one-off scripts (e.g. `scripts/regenerate_contact_descriptions.py`) — follow that convention (`cp renthub.db renthub.db.bak-$(date +%Y%m%d-%H%M%S)`) before running scripts that bulk-mutate the database.
