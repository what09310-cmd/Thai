"""
Migre les données de SQLite local vers Postgres (Render).

Usage:
    POSTGRES_URL=postgresql://user:pass@host/db python scripts/migrate_sqlite_to_postgres.py

L'URL Postgres vient de l'environnement, jamais du code: la version
precedente la portait en dur, mot de passe compris, dans un depot public
(commit c696be9) -- ce mot de passe a du etre revoque. SQLITE_URL permet de
pointer une autre base source (defaut: thaimonth.db a la racine du depot).

Vide chaque table Postgres avant d'y recopier la table SQLite: c'est une
migration initiale, pas une synchronisation.
"""
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.database.models import Base, Listing, ListingHistory, ListingImage, ScanLog, User

POSTGRES_URL = os.environ.get("POSTGRES_URL")
if not POSTGRES_URL or not POSTGRES_URL.startswith("postgresql"):
    raise SystemExit(
        "POSTGRES_URL manquante ou invalide dans l'environnement "
        "(attendu: postgresql://user:pass@host/db)."
    )
SQLITE_URL = os.environ.get("SQLITE_URL", "sqlite:///thaimonth.db")

sqlite_engine = create_engine(SQLITE_URL, connect_args={"check_same_thread": False})
SqliteSession = sessionmaker(bind=sqlite_engine)
pg_engine = create_engine(POSTGRES_URL, pool_pre_ping=True)
PgSession = sessionmaker(bind=pg_engine)

print("Initialisation des tables Postgres...")
Base.metadata.create_all(bind=pg_engine)

# Ordre impose par les cles etrangeres (images et historique referencent listings).
tables = [
    ("Listing", Listing),
    ("ListingImage", ListingImage),
    ("ListingHistory", ListingHistory),
    ("ScanLog", ScanLog),
    ("User", User),
]

CHUNK_SIZE = 1000

with SqliteSession() as sqlite_sess, PgSession() as pg_sess:
    for name, Model in tables:
        rows = sqlite_sess.query(Model).all()
        print(f"{name}: {len(rows)} lignes à migrer...")
        if not rows:
            continue
        pg_sess.query(Model).delete()
        columns = [c.name for c in Model.__table__.columns]
        for i in range(0, len(rows), CHUNK_SIZE):
            chunk = rows[i : i + CHUNK_SIZE]
            values = [{c: getattr(row, c) for c in columns} for row in chunk]
            pg_sess.execute(Model.__table__.insert(), values)
            print(f"  ... {min(i + CHUNK_SIZE, len(rows))}/{len(rows)}")
        pg_sess.commit()
        print(f"  [OK] {name} migré et commité")

print("\n[OK] Migration terminée !")
