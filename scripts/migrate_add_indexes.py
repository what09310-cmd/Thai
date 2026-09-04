#!/usr/bin/env python3
"""
Migration idempotente : cree les index manquants sur `listings` et
`listing_images`. Voir src/database/models.py, ou ils sont declares.

`Base.metadata.create_all` (src/database/session.py::init_db) ne touche pas
aux tables qui existent deja : declarer un Index dans le modele ne suffit
donc pas a le creer sur une base en place, d'ou ce script.

Ce qu'ils corrigent, mesure par EXPLAIN QUERY PLAN sur renthub.db :
  - listing_images.listing_id n'avait aucun index alors que c'est une cle
    etrangere : chaque appel de GET /listings faisait un `SCAN
    listing_images` sur ~60 000 lignes pour n'en tirer qu'une vignette par
    annonce.
  - listings.source_updated_at porte le tri par defaut de GET /listings, et
    listings.first_seen_at le filtre de GET /listings/new : les deux
    donnaient `SCAN listings` + `USE TEMP B-TREE FOR ORDER BY`.

Creer un index ne modifie aucune donnee, mais la convention du depot reste
de sauvegarder la base avant toute migration :
    cp renthub.db renthub.db.bak-$(date +%Y%m%d-%H%M%S)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from src.database.session import engine

INDEXES = [
    ("ix_listing_images_listing_id", "listing_images", "listing_id"),
    ("ix_listings_source_updated_at", "listings", "source_updated_at"),
    ("ix_listings_first_seen_at", "listings", "first_seen_at"),
]


def main() -> None:
    with engine.connect() as conn:
        for name, table, column in INDEXES:
            # IF NOT EXISTS suffirait sur SQLite et Postgres, mais un
            # message explicite par index rend la reprise lisible.
            existing = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'index' AND tbl_name = :table"
                    ),
                    {"table": table},
                )
            }
            if name in existing:
                print(f"Index '{name}' deja present, skip.")
                continue
            conn.execute(text(f"CREATE INDEX {name} ON {table} ({column})"))
            conn.commit()
            print(f"Index '{name}' cree sur {table}({column}).")

        # ANALYZE met a jour les statistiques que le planificateur SQLite
        # consulte pour choisir d'utiliser ces index.
        conn.execute(text("ANALYZE"))
        conn.commit()

    print("Migration terminee.")


if __name__ == "__main__":
    main()
