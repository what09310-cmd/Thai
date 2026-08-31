#!/usr/bin/env python3
"""
Re-scrape les pages détail des annonces actives pour extraire les
amenities depuis la grille d'icônes de la page (voir
detail_parser._extract_amenities_from_icons), plus fiable que l'ancienne
dérivation depuis le texte libre de la description : cette grille n'est
pas capturée dans le texte scrapé, donc `derive_amenities` produisait
souvent des faux négatifs (ex: "Air Conditioner" absent alors qu'il est
bien présent sur la page).

Ne touche que `amenities` et régénère `description` (contact/charges) en
conséquence via build_contact_description. Sollicite le site en direct
(~1115 requêtes, throttlées à REQUEST_DELAY) : à lancer après backup DB.
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database.session import get_session
from src.database.models import Listing
from src.scraper.http_client import ScraperClient
from src.parser.detail_parser import parse_detail_page
from src.normalizers.contact_description import build_contact_description

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger(__name__)

BATCH_SIZE = 50


async def main() -> None:
    with get_session() as session:
        listings = session.query(Listing).filter(Listing.status == "active").all()
        targets = [(l.id, l.url) for l in listings]
    total = len(targets)
    print(f"Annonces à re-scraper : {total}")

    updated = 0
    unchanged = 0
    failed = 0

    async with ScraperClient() as client:
        for i in range(0, total, BATCH_SIZE):
            batch = targets[i:i + BATCH_SIZE]
            with get_session() as session:
                for listing_id, url in batch:
                    html = await client.get(url)
                    if not html:
                        failed += 1
                        continue
                    detail = parse_detail_page(html, url)
                    if detail is None:
                        failed += 1
                        continue

                    listing = session.get(Listing, listing_id)
                    if listing is None:
                        continue

                    new_amenities_json = json.dumps(detail.amenities) if detail.amenities else None
                    if new_amenities_json != listing.amenities:
                        listing.amenities = new_amenities_json
                        updated += 1
                    else:
                        unchanged += 1

                    listing.description = build_contact_description(listing)

            done = min(i + BATCH_SIZE, total)
            print(f"  {done}/{total} traitées (maj={updated}, inchangé={unchanged}, échec={failed})", flush=True)

    print(f"Terminé. maj={updated} inchangé={unchanged} échec={failed}")


if __name__ == "__main__":
    asyncio.run(main())
