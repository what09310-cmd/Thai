"""Une annonce -> dict, partage par l'API (src/api/main.py) et l'export
(src/tracker/exporter.py), qui tenaient chacun leur propre liste de
colonnes."""
from __future__ import annotations

import json

from src.database.models import Listing


def listing_to_dict(listing: Listing) -> dict:
    """Toutes les colonnes de la ligne; `amenities` et `room_types`
    decodes (listes) au lieu de leur JSON stocke."""
    d = {col.name: getattr(listing, col.name) for col in Listing.__table__.columns}
    d["amenities"] = json.loads(listing.amenities) if listing.amenities else []
    d["room_types"] = json.loads(listing.room_types) if listing.room_types else []
    return d
