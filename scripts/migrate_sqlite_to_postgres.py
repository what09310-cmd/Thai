"""
Migre les données de SQLite local vers Postgres Render.
Usage: python scripts/migrate_sqlite_to_postgres.py
"""
import os
import sys
from pathlib import Path

# Charge SQLite
os.environ["DATABASE_URL"] = "sqlite:///renthub.db"

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sqlite_engine = create_engine("sqlite:///renthub.db", connect_args={"check_same_thread": False})
SqliteSession = sessionmaker(bind=sqlite_engine)

POSTGRES_URL = "postgresql://thaimonth_db_user:itGVxKYqUHAmNS0Ubwbt7RB7UXLqPBTl@dpg-daimj1lg1s2s73futnp0-a.oregon-postgres.render.com/thaimonth_db"
pg_engine = create_engine(POSTGRES_URL, pool_pre_ping=True)
PgSession = sessionmaker(bind=pg_engine)

# Importe les modèles et init la base Postgres
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.database.models import Base, Listing, ListingImage, ListingHistory, Province, ScanLog

print("Initialisation des tables Postgres...")
Base.metadata.create_all(bind=pg_engine)

tables = [
    ("Province", Province),
    ("Listing", Listing),
    ("ListingImage", ListingImage),
    ("ListingHistory", ListingHistory),
    ("ScanLog", ScanLog),
]

CHUNK_SIZE = 1000

with SqliteSession() as sqlite_sess, PgSession() as pg_sess:
    for name, Model in tables:
        rows = sqlite_sess.query(Model).all()
        print(f"{name}: {len(rows)} lignes à migrer...")
        if not rows:
            continue
        # Vide la table Postgres d'abord
        pg_sess.query(Model).delete()
        columns = [c.name for c in Model.__table__.columns]
        for i in range(0, len(rows), CHUNK_SIZE):
            chunk = rows[i : i + CHUNK_SIZE]
            values = [{c: getattr(row, c) for c in columns} for row in chunk]
            pg_sess.execute(Model.__table__.insert(), values)
            print(f"  ... {min(i + CHUNK_SIZE, len(rows))}/{len(rows)}")
        pg_sess.commit()
        print(f"  [OK]{name} migré et commité")

print("\n[OK]Migration terminée !")
