#!/usr/bin/env python3
"""
Re-scrape ponctuel des pages detail pour reparer un groupe de champs en
base, sans attendre la peremption normale (detail_refresh_days).

    python scripts/rescrape_details.py --fields contact           # contacts + charges
    python scripts/rescrape_details.py --fields amenities --only-missing
    python scripts/rescrape_details.py --fields coords,rooms --limit 50 --dry-run

Groupes de champs:
    contact    phone, line_id, whatsapp, email, deposit, advance_payment,
               electric_price, water_price, service_fee (apply_detail_fields:
               le JSON de la page fait autorite, meme vide)
    amenities  grille d'equipements de la page
    coords     latitude/longitude (listing.location du JSON)
    rooms      room_types

--only-missing ne retient que les annonces ou le premier groupe demande est
vide en base (amenities NULL/[], coordonnees NULL, contact sans aucun
canal, room_types NULL). Sans lui, toutes les annonces actives y passent.

Remplace six scripts qui repetaient la meme boucle (refix_missing_amenities,
refix_whatsapp_without_link, refresh_contact_and_fees, refresh_coordinates,
refresh_room_types, rescrape_amenities). Sauvegarder la base avant
(.claude/rules/scripts.md): les champs sont ecrases par ce que dit la page.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database.models import Listing  # noqa: E402
from src.database.session import get_session  # noqa: E402
from src.normalizers.contact_description import build_contact_description  # noqa: E402
from src.parser.detail_parser import parse_detail_page  # noqa: E402
from src.scraper.http_client import ScraperClient  # noqa: E402
from src.tracker.change_detector import apply_detail_fields  # noqa: E402

FIELD_GROUPS = ("contact", "amenities", "coords", "rooms")
COMMIT_EVERY = 50


def _missing_filter(group: str):
    """Critere SQLAlchemy "ce groupe est vide en base"."""
    if group == "amenities":
        return Listing.amenities.is_(None) | (Listing.amenities == "[]")
    if group == "coords":
        return Listing.latitude.is_(None) | Listing.longitude.is_(None)
    if group == "rooms":
        return Listing.room_types.is_(None)
    return (
        Listing.phone.is_(None) & Listing.line_id.is_(None)
        & Listing.whatsapp.is_(None) & Listing.email.is_(None)
    )


def _apply(listing: Listing, detail, groups: set[str]) -> list[str]:
    """Reporte les groupes demandes; rend les noms de champs modifies."""
    before = {c.name: getattr(listing, c.name) for c in Listing.__table__.columns}
    if "contact" in groups:
        apply_detail_fields(listing, detail)
    if "amenities" in groups and detail.amenities:
        listing.amenities = json.dumps(detail.amenities)
    if "coords" in groups and detail.latitude is not None and detail.longitude is not None:
        listing.latitude, listing.longitude = detail.latitude, detail.longitude
    if "rooms" in groups and detail.room_types:
        listing.room_types = json.dumps([r.model_dump() for r in detail.room_types])
    if detail.source_id:
        listing.detail_scraped_at = datetime.now(timezone.utc)
    listing.description = build_contact_description(listing)
    return [name for name, value in before.items() if getattr(listing, name) != value]


async def run(groups: set[str], only_missing: bool, limit: int | None, dry_run: bool) -> None:
    with get_session() as session:
        query = session.query(Listing.id, Listing.url, Listing.source_id).filter(Listing.status == "active")
        if only_missing:
            query = query.filter(_missing_filter(next(g for g in FIELD_GROUPS if g in groups)))
        if limit:
            query = query.limit(limit)
        targets = query.all()

    total = len(targets)
    print(f"{total} annonce(s) a re-scraper, groupes: {', '.join(sorted(groups))}" + (" (dry-run)" if dry_run else ""))
    changed = unchanged = failed = 0

    async with ScraperClient() as client:
        for start in range(0, total, COMMIT_EVERY):
            batch = targets[start:start + COMMIT_EVERY]
            with get_session() as session:
                for listing_id, url, source_id in batch:
                    html = await client.get(url)
                    detail = parse_detail_page(html, url) if html else None
                    if detail is None:
                        failed += 1
                        print(f"  [skip] {source_id or listing_id}: page illisible ({url})")
                        continue
                    listing = session.get(Listing, listing_id)
                    if listing is None:
                        continue
                    touched = _apply(listing, detail, groups)
                    if touched:
                        changed += 1
                        print(f"  [maj] {source_id or listing_id}: {', '.join(touched)}")
                    else:
                        unchanged += 1
                if dry_run:
                    session.rollback()
            print(f"  {min(start + COMMIT_EVERY, total)}/{total} traitees (maj={changed}, inchange={unchanged}, echec={failed})", flush=True)

    print(f"Termine: maj={changed} inchange={unchanged} echec={failed} sur {total}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fields", required=True, help="Groupes separes par des virgules: " + ", ".join(FIELD_GROUPS))
    parser.add_argument("--only-missing", action="store_true", help="Seulement les annonces ou le premier groupe est vide")
    parser.add_argument("--limit", type=int, default=None, help="Plafond d'annonces (test)")
    parser.add_argument("--dry-run", action="store_true", help="Scraper et comparer sans ecrire")
    args = parser.parse_args()

    groups = {g.strip() for g in args.fields.split(",") if g.strip()}
    unknown = groups - set(FIELD_GROUPS)
    if unknown or not groups:
        parser.error(f"groupes inconnus: {', '.join(sorted(unknown)) or '(aucun)'}; attendu: {', '.join(FIELD_GROUPS)}")
    asyncio.run(run(groups, args.only_missing, args.limit, args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
