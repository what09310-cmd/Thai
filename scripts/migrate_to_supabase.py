"""
Migrate data from local SQLite (thaimonth.db) to a remote Postgres (Supabase).

Usage:
    python scripts/migrate_to_supabase.py --target "postgresql://postgres:PWD@db.XXX.supabase.co:5432/postgres"

Tables migrated (in order to respect FK constraints):
    users, listings, listing_images, listing_history, scan_log
"""
import argparse
import sys
from pathlib import Path

# Allow "src.*" imports from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from src.database.models import Base, Listing, ListingHistory, ListingImage, ScanLog, User

SQLITE_URL = "sqlite:///thaimonth.db"
BATCH = 500  # rows per INSERT batch

TABLE_ORDER = [User, ScanLog, Listing, ListingImage, ListingHistory]


def make_engine(url: str, is_sqlite: bool = False):
    if is_sqlite:
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=5)


def table_count(session, model) -> int:
    return session.query(model).count()


def migrate_table(src_session, dst_session, model, dry_run: bool = False):
    name = model.__tablename__
    total = table_count(src_session, model)
    print(f"  {name}: {total} rows", end="", flush=True)
    if total == 0:
        print(" — skipped (empty)")
        return 0

    if dry_run:
        print(" — dry run, skipping INSERT")
        return total

    copied = 0
    offset = 0
    while True:
        rows = src_session.query(model).order_by(model.id).offset(offset).limit(BATCH).all()
        if not rows:
            break
        for row in rows:
            dst_session.merge(row)  # upsert: safe to re-run
        dst_session.flush()
        copied += len(rows)
        offset += BATCH
        print(f"\r  {name}: {copied}/{total}", end="", flush=True)

    dst_session.commit()
    print(f"\r  {name}: {copied}/{total} ✓")
    return copied


def main():
    parser = argparse.ArgumentParser(description="Migrate SQLite → Supabase (Postgres)")
    parser.add_argument("--target", required=True, help="Target Postgres URL (Supabase)")
    parser.add_argument("--dry-run", action="store_true", help="Count rows only, no writes")
    args = parser.parse_args()

    print(f"Source : {SQLITE_URL}")
    print(f"Target : {args.target[:40]}...")
    print(f"Mode   : {'DRY RUN' if args.dry_run else 'LIVE'}\n")

    src_engine = make_engine(SQLITE_URL, is_sqlite=True)
    dst_engine = make_engine(args.target)

    # Verify connections
    try:
        with src_engine.connect() as c:
            c.execute(text("SELECT 1"))
        print("✓ SQLite connection OK")
    except Exception as e:
        print(f"✗ Cannot open SQLite: {e}")
        sys.exit(1)

    try:
        with dst_engine.connect() as c:
            c.execute(text("SELECT 1"))
        print("✓ Supabase connection OK")
    except Exception as e:
        print(f"✗ Cannot connect to Supabase: {e}")
        sys.exit(1)

    if not args.dry_run:
        print("\nCreating tables on Supabase (if not exist)...")
        Base.metadata.create_all(bind=dst_engine)
        print("✓ Schema ready\n")

    SrcSession = sessionmaker(bind=src_engine)
    DstSession = sessionmaker(bind=dst_engine, autocommit=False, autoflush=False)

    src = SrcSession()
    dst = DstSession()

    print("Migrating tables:")
    total = 0
    try:
        for model in TABLE_ORDER:
            total += migrate_table(src, dst, model, dry_run=args.dry_run)
    except Exception as e:
        dst.rollback()
        print(f"\n✗ Error during migration: {e}")
        raise
    finally:
        src.close()
        dst.close()

    print(f"\nDone — {total} rows {'counted' if args.dry_run else 'migrated'}.")


if __name__ == "__main__":
    main()
