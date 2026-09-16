---
paths:
  - "src/database/**"
---

## Database

`src/database/models.py`, SQLAlchemy 2.0 declarative: `Listing` (core table, ~50 columns spanning location/pricing/contract-length/amenities/contact/tracking), `ListingImage` (unique per `(listing_id, image_url)`), `ListingHistory` (audit trail, composite index `(change_type, changed_at)`), `ScanLog` (one row per scan run), `User` (accounts). `Province` and `RentalRequest` were removed in the 2026-09 audit (never populated / never called); `scripts/migrate.py --drop-orphans` drops their tables. Schema changes on an existing database go through `scripts/migrate.py`, never through `create_all` alone. `src/database/serialize.py::listing_to_dict` is the one row-to-dict used by the API and the export. Same models run against SQLite (dev, `renthub.db`) or Postgres (`docker-compose.yml`) — `src/database/session.py` branches connection-pool args on the URL scheme.
