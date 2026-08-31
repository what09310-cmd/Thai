#!/usr/bin/env python3
"""
Nettoie le champ `description` déjà en base: retire les règles CSS ayant
fuité dans le texte scrapé (".css-xxxxx{...}") et les caractères corrompus
issus d'un mauvais décodage d'emoji, via
src.parser.detail_parser._clean_description_text.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database.session import get_session
from src.database.models import Listing
from src.parser.detail_parser import _clean_description_text


def main() -> None:
    changed = 0
    unchanged = 0

    with get_session() as session:
        listings = session.query(Listing).filter(Listing.description.isnot(None)).all()
        total = len(listings)

        for listing in listings:
            cleaned = _clean_description_text(listing.description)
            if cleaned != listing.description:
                listing.description = cleaned
                changed += 1
            else:
                unchanged += 1

    print(f"Descriptions traitées : {total}")
    print(f"Nettoyées             : {changed}")
    print(f"Inchangées             : {unchanged}")


if __name__ == "__main__":
    main()
