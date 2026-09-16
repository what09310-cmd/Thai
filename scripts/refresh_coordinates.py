#!/usr/bin/env python3
"""
Re-scrape la page détail des annonces sans latitude/longitude en base.

RentHub embarque un objet `location: {lat, lng}` précis dans le JSON
__NEXT_DATA__ de chaque page détail (vérifié manuellement — présent même
sur des annonces choisies au hasard). src/parser/detail_parser.py
l'extrait déjà correctement, mais scripts/run_scraper.py ne re-scrape
jamais la page détail d'une annonce qui a déjà une `description` en base
(optimisation pour éviter de re-taper le site à chaque scan) — du coup
cette donnée n'a jamais été récupérée pour les annonces scrapées avant
qu'on cherche spécifiquement ce champ.

Usage:
    python scripts/refresh_coordinates.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.database.models import Listing
from src.database.session import get_session
from src.scraper.detail_scraper import scrape_details_batch

BATCH_SIZE = 20


def main() -> None:
    with get_session() as session:
        urls = [
            r[0] for r in session.query(Listing.url)
            .filter(Listing.latitude.is_(None))
            .all()
        ]
    print(f"{len(urls)} annonces sans coordonnées à re-scraper")

    updated = 0
    not_found = 0
    for i in range(0, len(urls), BATCH_SIZE):
        batch = urls[i:i + BATCH_SIZE]
        results = asyncio.run(scrape_details_batch(batch))

        with get_session() as session:
            for url, detail in results.items():
                if detail and detail.latitude is not None and detail.longitude is not None:
                    listing = session.query(Listing).filter(Listing.url == url).first()
                    if listing:
                        listing.latitude = detail.latitude
                        listing.longitude = detail.longitude
                        updated += 1
                else:
                    not_found += 1

        print(
            f"Batch {i // BATCH_SIZE + 1}/{(len(urls) - 1) // BATCH_SIZE + 1}: "
            f"{updated} avec coordonnées, {not_found} sans, sur {i + len(batch)}/{len(urls)} traitées"
        )

    print(f"\nTerminé: {updated}/{len(urls)} annonces ont maintenant des coordonnées précises")


if __name__ == "__main__":
    main()
