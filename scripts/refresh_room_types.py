#!/usr/bin/env python3
"""
Re-scrape les pages détail de toutes les annonces actives pour remplir
`room_types` avec les nouveaux champs (room_type, size_sqm, status,
monthly_min_thb/monthly_max_thb) ajoutés à `RoomTypeSchema` : les annonces
déjà scrapées avant ce changement n'ont que les prix de contrat court terme
(1/3/6 mois), pas le loyer long terme ("Contract 1 year") ni le nom/statut
de la chambre.

`room_types` n'est pas dans DETAIL_FIELDS (src/tracker/change_detector.py)
donc scripts/refresh_contact_and_fees.py ne le touche pas : ce script est
son équivalent pour ce champ.

À lancer seulement après backup de la base (renthub.db.bak-*).
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database.models import Listing
from src.database.session import get_session
from src.parser.detail_parser import parse_detail_page
from src.scraper.http_client import ScraperClient


async def main() -> None:
    with get_session() as session:
        listings = session.query(Listing).filter(Listing.status == "active").all()
        targets = [(l.id, l.url) for l in listings]

    total = len(targets)
    updated = 0
    errors = 0

    async with ScraperClient() as client:
        for i, (listing_id, url) in enumerate(targets, 1):
            html = await client.get(url)
            if not html:
                errors += 1
                print(f"[{i}/{total}] echec fetch: {url}")
                continue

            detail = parse_detail_page(html, url)
            if detail is None:
                errors += 1
                print(f"[{i}/{total}] echec parse: {url}")
                continue

            with get_session() as session:
                db_listing = session.get(Listing, listing_id)
                if db_listing is None:
                    continue

                if detail.room_types:
                    db_listing.room_types = json.dumps(
                        [r.model_dump() for r in detail.room_types]
                    )

            updated += 1
            if i % 50 == 0:
                print(f"[{i}/{total}] {updated} mises a jour, {errors} erreurs")

    print(f"Termine : {updated} mises a jour, {errors} erreurs sur {total} annonces")


if __name__ == "__main__":
    asyncio.run(main())
