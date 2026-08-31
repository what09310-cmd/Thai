#!/usr/bin/env python3
"""
Remplace le champ `description` de chaque annonce active par un texte
standardisé (contact + dépôt + prix électricité + climatisation), via
src.normalizers.contact_description.build_contact_description.

Écrase le texte scrapé d'origine : à lancer seulement après backup de la
base (renthub.db.bak-*).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database.session import get_session
from src.database.models import Listing
from src.normalizers.contact_description import build_contact_description


def main() -> None:
    with get_session() as session:
        listings = session.query(Listing).filter(Listing.status == "active").all()
        total = len(listings)

        for listing in listings:
            listing.description = build_contact_description(listing)

    print(f"Descriptions régénérées : {total}")


if __name__ == "__main__":
    main()
