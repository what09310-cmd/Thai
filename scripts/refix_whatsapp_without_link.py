#!/usr/bin/env python3
"""
Re-scrape ponctuel des annonces actives dont le `whatsapp` en base vient du
champ JSON `contactInformation[0].whatsApp`, meme quand la page n'affiche
en realite aucun bouton/lien WhatsApp (wa.me) -- RentHub remplit parfois ce
champ sans que ce soit un vrai contact WhatsApp (cf. _page_has_whatsapp_link
dans src/parser/detail_parser.py). Corrige les lignes deja en base scrapees
avant ce filtre.

A lancer seulement apres backup de la base (renthub.db.bak-*).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database.session import get_session
from src.database.models import Listing
from src.parser.detail_parser import parse_detail_page
from src.scraper.http_client import ScraperClient


async def main() -> None:
    with get_session() as session:
        listings = (
            session.query(Listing)
            .filter(Listing.status == "active", Listing.whatsapp.isnot(None))
            .all()
        )
        print(f"Annonces a re-scraper : {len(listings)}")

        cleared = 0
        async with ScraperClient() as client:
            for listing in listings:
                html = await client.get(listing.url)
                if not html:
                    print(f"  [skip] {listing.source_id}: pas de reponse ({listing.url})")
                    continue

                detail = parse_detail_page(html, listing.url)
                if detail is None:
                    print(f"  [skip] {listing.source_id}: page non reconnue")
                    continue

                if detail.whatsapp != listing.whatsapp:
                    print(f"  [fixed] {listing.source_id}: {listing.whatsapp!r} -> {detail.whatsapp!r}")
                    listing.whatsapp = detail.whatsapp
                    cleared += 1

        print(f"Corrigees : {cleared}/{len(listings)}")


if __name__ == "__main__":
    asyncio.run(main())
