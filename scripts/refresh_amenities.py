#!/usr/bin/env python3
"""
Recalcule le champ `amenities` de toutes les annonces en base à partir de
leur `description`, via src.normalizers.amenities.derive_amenities.

Corrige le bug historique où le scraper recopiait la légende générique
d'équipements de RentHub (identique sur ~95% des pages détail) au lieu des
équipements réellement associés à chaque annonce.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database.session import get_session
from src.database.models import Listing
from src.normalizers.amenities import derive_amenities


def main() -> None:
    changed = 0
    unchanged = 0
    now_empty = 0

    with get_session() as session:
        listings = session.query(Listing).all()
        total = len(listings)

        for listing in listings:
            new_amenities = derive_amenities(listing.description)
            new_json = json.dumps(new_amenities) if new_amenities else None

            if new_json != listing.amenities:
                listing.amenities = new_json
                changed += 1
                if not new_amenities:
                    now_empty += 1
            else:
                unchanged += 1

    print(f"Annonces traitées   : {total}")
    print(f"Amenities corrigées : {changed}")
    print(f"Inchangées          : {unchanged}")
    print(f"Désormais vides     : {now_empty} (pas de description exploitable)")


if __name__ == "__main__":
    main()
