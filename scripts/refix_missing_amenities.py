#!/usr/bin/env python3
"""
Re-scrape ponctuel des annonces actives dont le dernier passage detail a
laisse `amenities` vide (NULL/[]) alors que `detail_scraped_at` est renseigne
(cf. bug du batch scanne le 2026-08-29 20:38-21:09 : ~17 annonces ont eu un
scrape partiel -- source_id lu mais grille d'equipements/description non
recuperees -- sans que cela le signale comme un echec).

A lancer seulement apres backup de la base (renthub.db.bak-*).
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database.session import get_session
from src.database.models import Listing
from src.normalizers.contact_description import build_contact_description
from src.parser.detail_parser import parse_detail_page
from src.scraper.http_client import ScraperClient


async def main() -> None:
    with get_session() as session:
        listings = (
            session.query(Listing)
            .filter(
                Listing.status == "active",
                Listing.detail_scraped_at.isnot(None),
                (Listing.amenities.is_(None)) | (Listing.amenities == "[]"),
            )
            .all()
        )
        print(f"Annonces a re-scraper : {len(listings)}")

        fixed = 0
        async with ScraperClient() as client:
            for listing in listings:
                html = await client.get(listing.url)
                if not html:
                    print(f"  [skip] {listing.source_id}: pas de reponse ({listing.url})")
                    continue

                detail = parse_detail_page(html, listing.url)
                if detail is None or not detail.amenities:
                    print(f"  [skip] {listing.source_id}: toujours vide apres re-scrape")
                    continue

                listing.amenities = json.dumps(detail.amenities)
                if detail.description:
                    listing.description = detail.description
                listing.description = build_contact_description(listing)
                fixed += 1
                print(f"  [fixed] {listing.source_id}: {detail.amenities}")

        print(f"Corrigees : {fixed}/{len(listings)}")


if __name__ == "__main__":
    asyncio.run(main())
