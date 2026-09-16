"""Export des listings en CSV et JSON."""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from src.database.models import Listing
from src.database.serialize import listing_to_dict

log = logging.getLogger(__name__)


def export_listings(
    session: Session,
    format: Literal["csv", "json"],
    output_path: str = ".",
    monthly_only: bool = False,
) -> Path:
    """
    Exporte les annonces actives en CSV ou JSON.

    Returns: Path du fichier créé.
    """
    query = session.query(Listing).filter(Listing.status == "active")
    if monthly_only:
        query = query.filter(Listing.has_monthly_contract == "true")

    listings = query.order_by(Listing.province, Listing.price_monthly_min).all()
    log.info(f"Export {len(listings)} annonces en {format.upper()}")

    output_dir = Path(output_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    if format == "csv":
        return _export_csv(listings, output_dir)
    elif format == "json":
        return _export_json(listings, output_dir)
    else:
        raise ValueError(f"Format inconnu: {format}")


# Colonnes de suivi interne, sans interet dans un export.
_INTERNAL = ("content_hash", "missing_scan_count", "source")


def _listing_to_dict(listing: Listing) -> dict:
    d = listing_to_dict(listing)
    for name in _INTERNAL:
        d.pop(name, None)
    for name, value in d.items():
        if isinstance(value, datetime):
            d[name] = value.isoformat()
    return d


def _export_csv(listings: list[Listing], output_dir: Path) -> Path:
    filepath = output_dir / "renthub_listings.csv"

    if not listings:
        filepath.write_text("")
        return filepath

    fieldnames = list(_listing_to_dict(listings[0]).keys())
    # Remplacer les listes par des strings pour CSV
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for listing in listings:
            row = _listing_to_dict(listing)
            # Sérialiser les listes
            row["amenities"] = "; ".join(row["amenities"]) if row["amenities"] else ""
            row["room_types"] = json.dumps(row["room_types"], ensure_ascii=False)
            writer.writerow(row)

    log.info(f"CSV exporté: {filepath}")
    return filepath


def _export_json(listings: list[Listing], output_dir: Path) -> Path:
    filepath = output_dir / "renthub_listings.json"
    data = [_listing_to_dict(l) for l in listings]
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    log.info(f"JSON exporté: {filepath}")
    return filepath
