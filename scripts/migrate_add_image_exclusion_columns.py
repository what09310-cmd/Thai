#!/usr/bin/env python3
"""
Migration idempotente : ajoute à `listing_images` les colonnes nécessaires
au nettoyage automatique des photos (QR code / texte / logo étranger) :
- excluded (bool, default false)
- exclusion_reason (texte, ex: "qr_code,foreign_logo")
- reviewed_at (timestamp du dernier passage de scripts/filter_problematic_images.py)

Pas d'Alembic configuré dans ce repo (pas de dossier alembic/) : migration
manuelle via ALTER TABLE, comme le fait le reste du projet pour ses scripts
de maintenance.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from src.database.session import engine

NEW_COLUMNS = {
    "excluded": "BOOLEAN NOT NULL DEFAULT 0",
    "exclusion_reason": "VARCHAR(200)",
    "reviewed_at": "DATETIME",
}


def main() -> None:
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.execute(text("PRAGMA table_info(listing_images)"))
        }

        for column, ddl_type in NEW_COLUMNS.items():
            if column in existing:
                print(f"Colonne '{column}' deja presente, skip.")
                continue
            conn.execute(text(f"ALTER TABLE listing_images ADD COLUMN {column} {ddl_type}"))
            conn.commit()
            print(f"Colonne '{column}' ajoutee.")

    print("Migration terminee.")


if __name__ == "__main__":
    main()
