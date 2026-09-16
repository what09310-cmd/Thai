#!/usr/bin/env python3
"""
Migration idempotente : ajoute à `listings` la colonne `detail_scraped_at`
(date du dernier scrape de la *page détail*, à distinguer de
`last_scraped_at` qui avance à chaque scan même quand seule la page de
liste a été lue).

Volontairement laissée à NULL sur les lignes existantes : leur contenu
détail (contact, dépôt, charges) date du tout premier scan et a été
produit par l'ancien parseur regex. NULL les fait re-scraper au prochain
passage, ce qui est précisément la réparation voulue.

Pas d'Alembic configuré dans ce repo (pas de dossier alembic/) : migration
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

        if "detail_scraped_at" in existing:
            print("Colonne 'detail_scraped_at' deja presente, skip.")
        else:
            conn.execute(
                text("ALTER TABLE listings ADD COLUMN detail_scraped_at DATETIME")
            )
            conn.commit()
            print("Colonne 'detail_scraped_at' ajoutee.")

    print("Migration terminee.")


if __name__ == "__main__":
    main()
