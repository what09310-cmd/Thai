#!/usr/bin/env python3
"""
Migration idempotente : ajoute a `listings` la colonne `line_verified`
(None = jamais verifie, True/False = resultat du dernier passage de
scripts/verify_line_ids.py). Voir src/database/models.py::Listing.line_verified.

Pas d'Alembic configure dans ce repo (pas de dossier alembic/) : migration
manuelle via ALTER TABLE, comme le reste des scripts de maintenance.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from src.database.session import engine


def main() -> None:
    with engine.connect() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(listings)"))}

        if "line_verified" in existing:
            print("Colonne 'line_verified' deja presente, skip.")
        else:
            conn.execute(text("ALTER TABLE listings ADD COLUMN line_verified BOOLEAN"))
            conn.commit()
            print("Colonne 'line_verified' ajoutee.")

    print("Migration terminee.")


if __name__ == "__main__":
    main()
