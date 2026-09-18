#!/usr/bin/env python3
"""
Migration idempotente d'une base existante vers le schema courant des
modeles (src/database/models.py). SQLite comme Postgres.

    python scripts/migrate.py                # applique ce qui manque
    python scripts/migrate.py --dry-run      # montre sans rien ecrire
    python scripts/migrate.py --drop-orphans # + supprime les tables sans modele

`create_all` ne touche jamais a une table qui existe deja: colonnes et
index ajoutes au modele n'arrivent en base que par ce script. Il remplace
les cinq scripts migrate_add_* qui faisaient chacun une de ces etapes a
la main, et couvre les cas suivants, dans l'ordre:

1. tables manquantes (create_all);
2. colonnes presentes dans le modele mais absentes de la table;
3. index et contraintes d'unicite du modele absents de la table -- apres
   dedoublonnage de listing_images (listing_id, image_url), que l'ancien
   calcul de position avait inscrit en double (15 paires dans renthub.db);
4. index retires du modele (ix_listings_source_id, ix_history_change_type);
5. Postgres seulement: sequences SERIAL en retard sur MAX(id) (derive apres
   un import a id explicites) -> resynchronisees, sinon le prochain INSERT
   revient sur un id deja pris (UniqueViolation);
6. donnees: scan_logs restes en "running" depuis plus de 12 h (interrompus
   avant que _mark_scan_failed n'existe) -> "failed"; content_hash recalcule apres un changement de
   HASH_FIELDS, sans quoi le scan suivant ecrit un UPDATED par annonce;
7. avec --drop-orphans: tables sans modele (locations, provinces,
   rental_requests, questionnaire_responses) et colonnes absentes du modele
   (listings.city, listings.published_at, listing_images.local_path --
   jamais renseignees), vestiges de fonctionnalites retirees.

Sauvegarder la base avant (voir .claude/rules/scripts.md): les etapes 3
et 7 suppriment des lignes.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.database.models import Base, Listing, ScanLog, Subscription  # noqa: E402
from src.database.session import engine  # noqa: E402
from src.tracker.change_detector import compute_content_hash  # noqa: E402

# Index qui existaient dans d'anciennes versions du modele et n'y sont plus.
OBSOLETE_INDEXES = {
    "listings": ["ix_listings_source_id"],
    "listing_history": ["ix_history_change_type"],
}

# Tables creees par des fonctionnalites retirees (crawler geo_discovery,
# table provinces jamais alimentee, formulaire rental_requests jamais
# branche). Supprimees seulement sur demande explicite.
ORPHAN_TABLES = ("locations", "provinces", "rental_requests", "questionnaire_responses")

# Age au-dela duquel un ScanLog encore "running" est considere abandonne.
STUCK_AFTER = timedelta(hours=12)


def _log(dry_run: bool, message: str) -> None:
    print(("[dry-run] " if dry_run else "") + message)


def add_missing_columns(conn, dry_run: bool) -> None:
    inspector = inspect(conn)
    for table in Base.metadata.sorted_tables:
        if table.name not in inspector.get_table_names():
            continue
        present = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column.type.compile(engine.dialect)}"
            _log(dry_run, f"colonne {table.name}.{column.name}: {ddl}")
            if not dry_run:
                conn.execute(text(ddl))


def dedupe_listing_images(conn, dry_run: bool) -> None:
    """Garde la plus ancienne ligne (id minimal) de chaque (listing_id, image_url)."""
    duplicates = conn.execute(text(
        "SELECT listing_id, image_url, MIN(id) AS keep, COUNT(*) AS n "
        "FROM listing_images GROUP BY listing_id, image_url HAVING COUNT(*) > 1"
    )).all()
    if not duplicates:
        return
    _log(dry_run, f"listing_images: {len(duplicates)} URL(s) en double a dedoublonner")
    if dry_run:
        return
    for listing_id, image_url, keep, _ in duplicates:
        conn.execute(
            text("DELETE FROM listing_images WHERE listing_id = :lid AND image_url = :url AND id <> :keep"),
            {"lid": listing_id, "url": image_url, "keep": keep},
        )


def add_missing_indexes(conn, dry_run: bool) -> None:
    inspector = inspect(conn)
    for table in Base.metadata.sorted_tables:
        if table.name not in inspector.get_table_names():
            continue
        present = {ix["name"] for ix in inspector.get_indexes(table.name)}
        present |= {uc["name"] for uc in inspector.get_unique_constraints(table.name)}
        for index in table.indexes:
            if index.name in present:
                continue
            _log(dry_run, f"index {index.name} sur {table.name}({', '.join(c.name for c in index.columns)})")
            if not dry_run:
                index.create(conn)
        for constraint in table.constraints:
            name = getattr(constraint, "name", None)
            if not name or name in present or not name.startswith("uq_"):
                continue
            cols = ", ".join(c.name for c in constraint.columns)
            # Un index unique vaut la contrainte, et s'ajoute a une table
            # existante sur SQLite (ALTER TABLE ADD CONSTRAINT n'y existe pas).
            _log(dry_run, f"unicite {name} sur {table.name}({cols})")
            if not dry_run:
                conn.execute(text(f"CREATE UNIQUE INDEX {name} ON {table.name} ({cols})"))


def drop_obsolete_indexes(conn, dry_run: bool) -> None:
    inspector = inspect(conn)
    for table, names in OBSOLETE_INDEXES.items():
        if table not in inspector.get_table_names():
            continue
        present = {ix["name"] for ix in inspector.get_indexes(table)}
        for name in names:
            if name in present:
                _log(dry_run, f"index obsolete {name} supprime")
                if not dry_run:
                    conn.execute(text(f"DROP INDEX {name}"))


def relax_subscription_user_id(conn, dry_run: bool) -> None:
    """Retire NOT NULL de subscriptions.user_id pour les paiements anonymes."""
    inspector = inspect(conn)
    if "subscriptions" not in inspector.get_table_names():
        return
    columns = {column["name"]: column for column in inspector.get_columns("subscriptions")}
    if "user_id" not in columns or columns["user_id"]["nullable"]:
        return
    _log(dry_run, "subscriptions.user_id: retrait de la contrainte NOT NULL")
    if dry_run:
        return
    if engine.url.get_backend_name() == "postgresql":
        conn.execute(text("ALTER TABLE subscriptions ALTER COLUMN user_id DROP NOT NULL"))
        return
    conn.execute(text("ALTER TABLE subscriptions RENAME TO subscriptions_old"))
    Subscription.__table__.create(conn)
    conn.execute(text(
        "INSERT INTO subscriptions (id, user_id, stripe_customer_id, plan_name, status, "
        "current_period_end, created_at) "
        "SELECT id, user_id, stripe_customer_id, plan_name, status, current_period_end, created_at "
        "FROM subscriptions_old"
    ))
    conn.execute(text("DROP TABLE subscriptions_old"))


def fix_sequences(conn, dry_run: bool) -> None:
    """Postgres seulement: resynchronise chaque sequence SERIAL sur MAX(id).

    Derive apres un import qui a insere des id explicites (copie depuis
    SQLite, restauration partielle) sans passer par nextval(): la sequence
    reste en retard et le prochain INSERT choisit un id deja pris
    (UniqueViolation sur la contrainte de cle primaire).
    """
    if engine.url.get_backend_name() != "postgresql":
        return
    inspector = inspect(conn)
    for table in Base.metadata.sorted_tables:
        if table.name not in inspector.get_table_names():
            continue
        pk_cols = [c for c in table.primary_key.columns if c.name == "id"]
        if not pk_cols:
            continue
        seq = conn.execute(text("SELECT pg_get_serial_sequence(:t, 'id')"), {"t": table.name}).scalar()
        if not seq:
            continue
        max_id = conn.execute(text(f"SELECT MAX(id) FROM {table.name}")).scalar()
        next_val, is_called = conn.execute(
            text("SELECT last_value, is_called FROM " + seq)
        ).first()
        expected = (max_id or 0) + 1
        current = next_val + 1 if is_called else next_val
        if current >= expected:
            continue
        _log(dry_run, f"sequence {seq}: {current} -> {expected}")
        if not dry_run:
            conn.execute(text("SELECT setval(:seq, :val, false)"), {"seq": seq, "val": expected})


def fix_data(conn, dry_run: bool) -> None:
    session = Session(bind=conn)
    # Un scan complet dure au plus quelques heures: au-dela de STUCK_AFTER,
    # "running" est un vestige. En dessous, c'est peut-etre un scan en cours
    # (ne jamais migrer pendant un scan, mais au moins ne pas le declarer mort).
    cutoff = datetime.now(timezone.utc) - STUCK_AFTER
    stuck = [
        scan for scan in session.query(ScanLog).filter(ScanLog.status == "running")
        if (scan.started_at.replace(tzinfo=timezone.utc) if scan.started_at.tzinfo is None else scan.started_at) < cutoff
    ]
    if stuck:
        _log(dry_run, f"scan_logs: {len(stuck)} scan(s) restes en 'running' depuis plus de {STUCK_AFTER} -> 'failed'")
        if not dry_run:
            for scan in stuck:
                scan.status = "failed"
                scan.finished_at = scan.finished_at or datetime.now(timezone.utc)
                scan.error_message = "interrompu sans compte rendu (marque par scripts/migrate.py)"
    rehashed = 0
    for listing in session.query(Listing).yield_per(500):
        new_hash = compute_content_hash(listing)
        if listing.content_hash != new_hash:
            rehashed += 1
            if not dry_run:
                listing.content_hash = new_hash
    if rehashed:
        _log(dry_run, f"listings: content_hash recalcule sur {rehashed} annonce(s) (HASH_FIELDS a change)")
    if not dry_run:
        session.flush()


def drop_orphans(conn, dry_run: bool) -> None:
    inspector = inspect(conn)
    for name in ORPHAN_TABLES:
        if name in inspector.get_table_names():
            rows = conn.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar()
            _log(dry_run, f"table orpheline {name} ({rows} lignes) supprimee")
            if not dry_run:
                conn.execute(text(f"DROP TABLE {name}"))
    # Colonnes que le modele ne porte plus. DROP COLUMN existe sur SQLite
    # depuis 3.35 (2021) et sur Postgres depuis toujours; une colonne encore
    # referencee par un index ferait echouer l'instruction, d'ou le try.
    for table in Base.metadata.sorted_tables:
        if table.name not in inspector.get_table_names():
            continue
        present = {c["name"] for c in inspector.get_columns(table.name)}
        for column in sorted(present - {c.name for c in table.columns}):
            _log(dry_run, f"colonne orpheline {table.name}.{column} supprimee")
            if not dry_run:
                try:
                    conn.execute(text(f"ALTER TABLE {table.name} DROP COLUMN {column}"))
                except Exception as exc:  # noqa: BLE001 - on continue, la colonne est inoffensive
                    print(f"  impossible de supprimer {table.name}.{column}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Afficher les operations sans rien ecrire")
    parser.add_argument("--drop-orphans", action="store_true", help="Supprimer aussi les tables sans modele")
    args = parser.parse_args()

    print(f"Base: {engine.url.render_as_string(hide_password=True)}")
    if not args.dry_run:
        Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        add_missing_columns(conn, args.dry_run)
        relax_subscription_user_id(conn, args.dry_run)
        dedupe_listing_images(conn, args.dry_run)
        add_missing_indexes(conn, args.dry_run)
        drop_obsolete_indexes(conn, args.dry_run)
        fix_sequences(conn, args.dry_run)
        fix_data(conn, args.dry_run)
        if args.drop_orphans:
            drop_orphans(conn, args.dry_run)
        if not args.dry_run and engine.url.get_backend_name() == "sqlite":
            conn.execute(text("ANALYZE"))
    print("Migration terminee." if not args.dry_run else "Rien ecrit (dry-run).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
