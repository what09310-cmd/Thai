#!/usr/bin/env python3
"""
RentHub Tracker — Script principal

Usage:
    python scripts/run_scraper.py
    python scripts/run_scraper.py --one-month-only
    python scripts/run_scraper.py --max-pages 5
    python scripts/run_scraper.py --province bangkok
    python scripts/run_scraper.py --export csv
    python scripts/run_scraper.py --export json --output ./exports
"""
import asyncio
import json
import logging
import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Ajouter le répertoire racine au PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent))

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from src.config import settings
from src.database.session import init_db, get_session, check_connection
from src.database.models import Listing, ScanLog
from src.models.schemas import ListingFull
from src.scraper.list_scraper import scrape_all_listings, scrape_provinces, scrape_all_location_listings
from src.scraper.detail_scraper import scrape_details_batch
from src.tracker.change_detector import upsert_listing, mark_removed_listings, upsert_province
from src.tracker.exporter import export_listings
from src.filters.contract import has_short_term_contract

console = Console()


def _as_utc(value: datetime) -> datetime:
    """SQLite rend des datetimes naïfs; les valeurs stockées sont en UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def select_detail_urls(
    listings,
    last_detail_scrape: dict[str, Optional[datetime]],
    now: datetime,
    refresh_days: int,
) -> list[str]:
    """URLs des pages détail à (re)scraper pour ce scan.

    Une annonce est scrapée si sa page individuelle ne l'a jamais été, ou
    si le dernier scrape détail remonte à plus de `refresh_days` jours.
    Sans cette péremption, les champs qui ne viennent que de la page
    détail (contact, dépôt, charges) restaient figés sur la valeur du
    tout premier scan: ni une correction du parseur ni un changement côté
    bailleur ne pouvait plus les atteindre.
    """
    cutoff = now - timedelta(days=refresh_days)
    urls = []
    for listing in listings:
        scraped_at = last_detail_scrape.get(listing.slug)
        if scraped_at is None or _as_utc(scraped_at) < cutoff:
            urls.append(listing.url)
    return urls


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )
    # Réduire le bruit des libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@click.command()
@click.option("--one-month-only", is_flag=True, help="Ne stocker que les annonces avec contract monthly")
@click.option("--max-pages", type=int, default=None, help="Limiter le nombre de pages (test)")
@click.option("--province", type=str, default=None, help="Filtrer par province (slug, ex: bangkok)")
@click.option("--export", type=click.Choice(["csv", "json"]), default=None, help="Exporter après le scan")
@click.option("--output", type=str, default=".", help="Répertoire d'export")
@click.option("--scrape-details/--no-scrape-details", default=True, help="Scraper les pages individuelles")
@click.option("--verbose", is_flag=True, help="Logs détaillés")
@click.option("--init-db-only", is_flag=True, help="Créer les tables et quitter")
@click.option("--update-provinces", is_flag=True, help="Mettre à jour la liste des provinces")
@click.option(
    "--include-locations", is_flag=True,
    help="Scraper aussi /en/short-term-rental/<province> pour toutes les provinces "
         "(ex: ko-samui). Ces annonces sont gardées si elles ont un Short-Term Rental "
         "Contract (1, 3 ou 6 mois) — voir has_short_term_contract.",
)
def main(
    one_month_only: bool,
    max_pages: Optional[int],
    province: Optional[str],
    export: Optional[str],
    output: str,
    scrape_details: bool,
    verbose: bool,
    init_db_only: bool,
    update_provinces: bool,
    include_locations: bool,
) -> None:
    setup_logging(verbose)
    log = logging.getLogger(__name__)

    # Vérifier la connexion DB
    if not check_connection():
        console.print("[red]❌ Impossible de se connecter à la base de données.[/red]")
        console.print(f"DATABASE_URL: {settings.database_url}")
        sys.exit(1)

    # Initialiser les tables
    init_db()
    console.print("[green]✅ Base de données initialisée[/green]")

    if init_db_only:
        return

    # Mettre à jour les provinces
    if update_provinces:
        console.print("🗺️  Mise à jour des provinces...")
        provinces = asyncio.run(scrape_provinces())
        with get_session() as session:
            for prov in provinces:
                upsert_province(session, prov)
        console.print(f"   {len(provinces)} provinces mises à jour")
        return

    # Lancer le scan
    scan_time = datetime.now(timezone.utc)

    with get_session() as session:
        scan_log = ScanLog(started_at=scan_time, status="running")
        session.add(scan_log)
        session.flush()
        scan_log_id = scan_log.id

    stats = {
        "pages_scanned": 0,
        "listings_found": 0,
        "new": 0,
        "updated": 0,
        "price_changed": 0,
        "removed": 0,
        "monthly": 0,
        "errors": 0,
    }

    console.print(f"\n[bold cyan]🚀 Scan démarré — {scan_time.strftime('%Y-%m-%d %H:%M:%S UTC')}[/bold cyan]")
    if one_month_only:
        console.print("[yellow]   Mode: Contract monthly uniquement[/yellow]")
    if max_pages:
        console.print(f"[yellow]   Mode: {max_pages} pages maximum[/yellow]")
    if province:
        console.print(f"[yellow]   Province: {province}[/yellow]")

    seen_slugs: set[str] = set()
    listings_to_process: list[ListingRaw] = []

    # — Phase 1: Scraping de la liste —
    console.print("\n[bold]Phase 1: Scraping des pages de liste...[/bold]")

    async def run_list_scraper():
        nonlocal listings_to_process
        page_count = 0
        async for listing_raw in scrape_all_listings(
            max_pages=max_pages,
            province_slug=province,
        ):
            listings_to_process.append(listing_raw)
            # Compter les pages (approximatif)
            if len(listings_to_process) % 40 == 0:
                page_count += 1
                console.print(f"   Page ~{page_count}: {len(listings_to_process)} annonces collectées")
        return page_count

    page_count = asyncio.run(run_list_scraper())
    stats["listings_found"] = len(listings_to_process)
    stats["pages_scanned"] = page_count
    console.print(f"   [green]✅ {len(listings_to_process)} annonces collectées[/green]")

    # Filtrer si --one-month-only
    if one_month_only:
        before = len(listings_to_process)
        listings_to_process = [l for l in listings_to_process if l.has_monthly_contract == "true"]
        console.print(f"   Filtre monthly: {before} -> {len(listings_to_process)} annonces")

    # — Phase 1b: Scraping des pages par lieu (--include-locations) —
    if include_locations:
        console.print("\n[bold]Phase 1b: Scraping des pages par lieu (short-term-rental)...[/bold]")

        known_slugs_1b = {l.slug for l in listings_to_process}
        added_1b = 0
        skipped_1b = 0

        async def run_location_scraper():
            nonlocal added_1b, skipped_1b
            async for listing_raw in scrape_all_location_listings(
                max_pages_per_location=max_pages,
            ):
                if listing_raw.slug in known_slugs_1b:
                    continue
                known_slugs_1b.add(listing_raw.slug)
                if not has_short_term_contract(listing_raw):
                    skipped_1b += 1
                    continue
                listings_to_process.append(listing_raw)
                added_1b += 1

        asyncio.run(run_location_scraper())
        stats["listings_found"] += added_1b
        console.print(
            f"   [green]✅ {added_1b} annonces supplémentaires[/green] "
            f"({skipped_1b} ignorées: pas de Short-Term Rental Contract)"
        )

    # — Phase 2: Scraping des pages détail —
    detail_map: dict[str, "ListingDetail"] = {}

    if scrape_details and listings_to_process:
        with get_session() as session:
            # source_id ("Listing no") n'est renseigné que par le parseur
            # de page détail: c'est le seul marqueur fiable d'un scrape
            # détail réussi. `description` ne convient pas, elle est
            # regénérée pour toute annonce par build_contact_description.
            last_detail_scrape = {
                slug: detail_scraped_at
                for slug, detail_scraped_at in session.query(
                    Listing.slug, Listing.detail_scraped_at
                ).filter(Listing.source_id.isnot(None))
            }

        urls_to_scrape = select_detail_urls(
            listings_to_process,
            last_detail_scrape,
            scan_time,
            settings.detail_refresh_days,
        )
        skipped = len(listings_to_process) - len(urls_to_scrape)

        console.print(f"\n[bold]Phase 2: Scraping détail...[/bold]")
        console.print(
            f"   {skipped} skippées (détail scrapé il y a moins de "
            f"{settings.detail_refresh_days} jours)"
        )
        console.print(f"   {len(urls_to_scrape)} à scraper")

        BATCH_SIZE = 20
        for i in range(0, len(urls_to_scrape), BATCH_SIZE):
            batch = urls_to_scrape[i:i + BATCH_SIZE]
            console.print(f"   Batch {i // BATCH_SIZE + 1}/{max(1, (len(urls_to_scrape) - 1) // BATCH_SIZE + 1)}: {len(batch)} pages")
            results = asyncio.run(scrape_details_batch(batch))
            detail_map.update(results)
            errors_in_batch = sum(1 for v in results.values() if v is None)
            if errors_in_batch:
                stats["errors"] += errors_in_batch
                console.print(f"   [yellow]⚠️  {errors_in_batch} erreurs dans ce batch[/yellow]")

        console.print(f"   [green]✅ {len(detail_map)} pages détail récupérées[/green]")
    else:
        console.print("\n[dim]Phase 2: Skippée (--no-scrape-details)[/dim]")

    # — Phase 3: Persistance en base —
    console.print("\n[bold]Phase 3: Mise à jour de la base de données...[/bold]")

    with get_session() as session:
        for listing_raw in listings_to_process:
            try:
                seen_slugs.add(listing_raw.slug)

                # Fusionner avec les données détail
                detail = detail_map.get(listing_raw.url)
                listing_full = _merge_listing(listing_raw, detail)

                change_type, db_listing = upsert_listing(session, listing_full, scan_time)

                if db_listing and listing_full.has_monthly_contract == "true":
                    stats["monthly"] += 1

                if change_type == "NEW":
                    stats["new"] += 1
                elif change_type == "UPDATED":
                    stats["updated"] += 1
                elif change_type == "PRICE_CHANGED":
                    stats["price_changed"] += 1

            except Exception as e:
                log.error(f"Erreur traitement {listing_raw.slug}: {e}")
                stats["errors"] += 1
                continue

        # Détecter les annonces supprimées (seulement si scan complet).
        # --max-pages tronque la collecte: sans cette garde, toutes les
        # annonces non vues voient missing_scan_count augmenter et le
        # catalogue entier bascule en "removed" au bout de 3 runs.
        if not one_month_only and not province and not max_pages:
            removed = mark_removed_listings(session, seen_slugs, scan_time)
            stats["removed"] = removed
        else:
            console.print(
                "[yellow]   Scan partiel: détection des suppressions ignorée[/yellow]"
            )

        # Mettre à jour le scan log
        scan_log = session.query(ScanLog).filter_by(id=scan_log_id).first()
        if scan_log:
            scan_log.finished_at = datetime.now(timezone.utc)
            scan_log.pages_scanned = stats["pages_scanned"]
            scan_log.listings_found = stats["listings_found"]
            scan_log.new_count = stats["new"]
            scan_log.updated_count = stats["updated"]
            scan_log.price_changed_count = stats["price_changed"]
            scan_log.removed_count = stats["removed"]
            scan_log.monthly_count = stats["monthly"]
            scan_log.error_count = stats["errors"]
            scan_log.status = "completed"

    # — Rapport final —
    console.print("\n")
    table = Table(title="📊 Résultats du scan", show_header=True)
    table.add_column("Métrique", style="cyan")
    table.add_column("Valeur", style="bold white", justify="right")

    table.add_row("Scan démarré", scan_time.strftime("%Y-%m-%d %H:%M:%S UTC"))
    table.add_row("Pages scannées", str(stats["pages_scanned"]))
    table.add_row("Annonces trouvées", str(stats["listings_found"]))
    table.add_row("Nouvelles [NEW]", f"[green]{stats['new']}[/green]")
    table.add_row("Mises à jour [UPDATED]", f"[yellow]{stats['updated']}[/yellow]")
    table.add_row("Prix changés [PRICE_CHANGED]", f"[magenta]{stats['price_changed']}[/magenta]")
    table.add_row("Supprimées [REMOVED]", f"[red]{stats['removed']}[/red]")
    table.add_row("Avec Contract monthly", f"[cyan]{stats['monthly']}[/cyan]")
    table.add_row("Erreurs", f"[red]{stats['errors']}[/red]")

    console.print(table)

    # — Export optionnel —
    if export:
        console.print(f"\n📁 Export en {export.upper()}...")
        with get_session() as session:
            path = export_listings(
                session,
                format=export,
                output_path=output,
                monthly_only=one_month_only,
            )
        console.print(f"   [green]✅ Exporté: {path}[/green]")


def _merge_listing(listing_raw, detail) -> ListingFull:
    """Fusionne ListingRaw et ListingDetail en ListingFull."""
    from src.models.schemas import ListingFull

    raw_data = listing_raw.model_dump()
    detail_data = detail.model_dump() if detail else {}

    # Les champs du détail enrichissent le raw (sans écraser)
    merged = {**raw_data, **detail_data}
    return ListingFull(**merged)


if __name__ == "__main__":
    main()