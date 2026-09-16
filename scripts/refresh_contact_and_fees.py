#!/usr/bin/env python3
"""
Re-scrape les pages détail de toutes les annonces actives pour corriger
phone/line_id/whatsapp/email/deposit/advance_payment/electric_price/
water_price/service_fee : ces champs étaient extraits par un regex fragile
sur le texte visible, remplacé par une lecture du JSON __NEXT_DATA__
embarqué dans la page (voir src/parser/detail_parser.py).

Contacts et charges sont écrasés même par None dès que le JSON
__NEXT_DATA__ de la page a pu être lu : c'est le seul moyen d'effacer le
texte parasite produit par l'ancien fallback regex (dépôt "This is not
verified listing...", line_id "Unavailable"). La règle exacte vit dans
change_detector.apply_detail_fields, partagée avec le scan normal.

À lancer seulement après backup de la base (renthub.db.bak-*).
"""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database.models import Listing
from src.database.session import get_session
from src.normalizers.contact_description import build_contact_description
from src.parser.detail_parser import parse_detail_page
from src.scraper.http_client import ScraperClient
from src.tracker.change_detector import apply_detail_fields


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

                apply_detail_fields(db_listing, detail)
                if detail.source_id:
                    db_listing.detail_scraped_at = datetime.now(timezone.utc)

                db_listing.description = build_contact_description(db_listing)

            updated += 1
            if i % 50 == 0:
                print(f"[{i}/{total}] {updated} mises a jour, {errors} erreurs")

    print(f"Termine : {updated} mises a jour, {errors} erreurs sur {total} annonces")


if __name__ == "__main__":
    asyncio.run(main())
