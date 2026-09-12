---
paths:
  - "src/database/**"
---

## Database

`src/database/models.py`, SQLAlchemy 2.0 declarative: `Listing` (core table, ~40 columns spanning location/pricing/contract-length/amenities/contact/tracking), `ListingImage`, `ListingHistory` (audit trail), `Province`, `ScanLog` (one row per scan run), `RentalRequest` (inbound demand form from the frontend). Same models run against SQLite (dev, `renthub.db`) or Postgres (`docker-compose.yml`) — `src/database/session.py` branches connection-pool args on the URL scheme.
