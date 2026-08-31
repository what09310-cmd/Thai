"""Export des listings en CSV et JSON."""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from src.database.models import Listing

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


def _listing_to_dict(listing: Listing) -> dict:
    return {
        "id": listing.id,
        "source_id": listing.source_id,
        "name": listing.name,
        "url": listing.url,
        "address": listing.address,
        "subdistrict": listing.subdistrict,
        "district": listing.district,
        "province": listing.province,
        "latitude": listing.latitude,
        "longitude": listing.longitude,
        "price_monthly_raw": listing.price_monthly_raw,
        "price_monthly_min": listing.price_monthly_min,
        "price_monthly_max": listing.price_monthly_max,
        "daily_price_raw": listing.daily_price_raw,
        "daily_price_min": listing.daily_price_min,
        "daily_price_max": listing.daily_price_max,
        "contract_monthly_raw": listing.contract_monthly_raw,
        "contract_monthly_min": listing.contract_monthly_min,
        "contract_monthly_max": listing.contract_monthly_max,
        "contract_3_month_raw": listing.contract_3_month_raw,
        "contract_3_month_min": listing.contract_3_month_min,
        "contract_3_month_max": listing.contract_3_month_max,
        "contract_6_month_raw": listing.contract_6_month_raw,
        "contract_6_month_min": listing.contract_6_month_min,
        "contract_6_month_max": listing.contract_6_month_max,
        "has_monthly_contract": listing.has_monthly_contract,
        "description": listing.description,
        "amenities": json.loads(listing.amenities) if listing.amenities else [],
        "room_types": json.loads(listing.room_types) if listing.room_types else [],
        "phone": listing.phone,
        "line_id": listing.line_id,
        "whatsapp": listing.whatsapp,
        "is_verified": listing.is_verified,
        "has_promotion": listing.has_promotion,
        "status": listing.status,
        "source_updated_at": listing.source_updated_at.isoformat() if listing.source_updated_at else None,
        "first_seen_at": listing.first_seen_at.isoformat() if listing.first_seen_at else None,
        "last_seen_at": listing.last_seen_at.isoformat() if listing.last_seen_at else None,
        "last_scraped_at": listing.last_scraped_at.isoformat() if listing.last_scraped_at else None,
    }


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
