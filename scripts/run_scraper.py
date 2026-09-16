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
import logging
import sys
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
from src.database.models import Listing, ScanLog
from src.database.session import check_connection, get_session, init_db
from src.filters.contract import has_short_term_contract
from src.models.schemas import ListingDetail, ListingFull, ListingRaw
from src.scraper.detail_scraper import scrape_details_batch
from src.scraper.http_client import ScraperClient
from src.scraper.list_scraper import scrape_all_listings, scrape_all_location_listings
from src.tracker.change_detector import load_existing, mark_removed_listings, upsert_listing
from src.tracker.exporter import export_listings

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


async def collect_unique_listings(source, on_new_page=None) -> tuple[list, int, int]:
    """Collecte les annonces d'un flux (annonce, url_de_page), sans doublon.

    Retourne (annonces, nombre_de_pages, doublons_ignores).

    Une meme annonce revient regulierement sur deux pages de la pagination
    (l'ordre du site bouge entre deux requetes, certaines fiches sont mises
    en avant): ~19% des cartes collectees sur un echantillon de deux pages.
    Traitee deux fois dans le meme scan, elle etait upsertee deux fois avec
    des donnees differentes -- chaque passage annulait le precedent et
    ecrivait sa propre ligne PRICE_CHANGED. On garde la premiere occurrence,
    comme le fait deja la phase 1b pour les pages par lieu.
    """
    listings: list = []
    seen: set[str] = set()
    pages: set[str] = set()
    duplicates = 0

    async for listing_raw, page_url in source:
        if listing_raw.slug in seen:
            duplicates += 1
            continue
        seen.add(listing_raw.slug)
        listings.append(listing_raw)
        if page_url not in pages:
            pages.add(page_url)
            if on_new_page is not None:
                on_new_page(len(pages), len(listings))

    return listings, len(pages), duplicates


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
@click.option(
    "--include-locations", is_flag=True,
    help="Scraper aussi /en/short-term-rental/<province> pour toutes les provinces "
         "(ex: ko-samui). Ces annonces sont gardées si elles ont un Short-Term Rental "
         "Contract (1, 3 ou 6 mois) — voir has_short_term_contract.",
)
@click.option(
    "--locations-provinces", type=str, default=None,
    help="Avec --include-locations, restreint le scraping par lieu à ces provinces "
         "(slugs séparés par des virgules, ex: bangkok,phuket) au lieu des 77. "
         "Pour un scan fréquent qui ne revoit que les plus gros marchés — le scan "
         "complet (sans cette option) reste nécessaire pour ne perdre aucune annonce "
         "1/3/6 mois des petites provinces.",
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
    include_locations: bool,
    locations_provinces: Optional[str],
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
        "gone": 0,
    }

    console.print(f"\n[bold cyan]🚀 Scan démarré — {scan_time.strftime('%Y-%m-%d %H:%M:%S UTC')}[/bold cyan]")
    if one_month_only:
        console.print("[yellow]   Mode: Contract monthly uniquement[/yellow]")
    if max_pages:
        console.print(f"[yellow]   Mode: {max_pages} pages maximum[/yellow]")
    if province:
        console.print(f"[yellow]   Province: {province}[/yellow]")

    locations_provinces_list = (
        [p.strip() for p in locations_provinces.split(",") if p.strip()]
        if locations_provinces
        else None
    )
    if locations_provinces_list:
        console.print(
            f"[yellow]   Lieux restreints à: {', '.join(locations_provinces_list)}[/yellow]"
        )

    seen_slugs: set[str] = set()
    listings_to_process: list[ListingRaw] = []

    try:
        _run_scan(
            console=console,
            log=log,
            scan_log_id=scan_log_id,
            scan_time=scan_time,
            stats=stats,
            seen_slugs=seen_slugs,
            listings_to_process=listings_to_process,
            one_month_only=one_month_only,
            max_pages=max_pages,
            province=province,
            scrape_details=scrape_details,
            include_locations=include_locations,
            locations_provinces=locations_provinces_list,
        )
    except BaseException as exc:
        # Sans cette reprise, un scan interrompu (reseau, Ctrl-C, plantage)
        # laissait sa ligne ScanLog en "running" pour toujours: le statut
        # "failed" du modele n'etait jamais ecrit, /stats ne retenant que
        # les scans "completed", l'echec restait invisible.
        _mark_scan_failed(scan_log_id, exc)
        raise

    _print_report(console, scan_time, stats)

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


def _mark_scan_failed(scan_log_id: int, exc: BaseException) -> None:
    try:
        with get_session() as session:
            scan_log = session.query(ScanLog).filter_by(id=scan_log_id).first()
            if scan_log and scan_log.status == "running":
                scan_log.status = "failed"
                scan_log.finished_at = datetime.now(timezone.utc)
                scan_log.error_message = f"{type(exc).__name__}: {exc}"[:2000]
    except Exception:
        # Ne jamais masquer l'erreur d'origine par une erreur de journalisation.
        logging.getLogger(__name__).exception("Impossible de marquer le scan en échec")


def _print_report(console: Console, scan_time: datetime, stats: dict) -> None:
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
    table.add_row("Pages détail disparues (404)", f"[dim]{stats['gone']}[/dim]")

    console.print(table)


def _run_scan(
    *,
    console: Console,
    log,
    scan_log_id: int,
    scan_time: datetime,
    stats: dict,
    seen_slugs: set,
    listings_to_process: list,
    one_month_only: bool,
    max_pages: Optional[int],
    province: Optional[str],
    scrape_details: bool,
    include_locations: bool,
    locations_provinces: Optional[list[str]] = None,
) -> None:
    """Corps du scan, isolé pour que main() puisse marquer l'échec."""

    # — Phase 1: Scraping de la liste —
    console.print("\n[bold]Phase 1: Scraping des pages de liste...[/bold]")

    # Le nombre de pages vient des URLs reellement lues, et non d'une regle
    # de trois sur le nombre d'annonces: `len % 40` supposait 40 annonces par
    # page et se decalait des qu'une page en rendait un autre nombre.
    async def run_list_scraper():
        return await collect_unique_listings(
            scrape_all_listings(max_pages=max_pages, province_slug=province),
            on_new_page=lambda page_no, count: console.print(
                f"   Page {page_no}: {count} annonces collectées"
            ),
        )

    collected, page_count, duplicates = asyncio.run(run_list_scraper())
    listings_to_process.extend(collected)
    if duplicates:
        console.print(f"   [dim]{duplicates} doublon(s) de pagination ignoré(s)[/dim]")
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
                provinces=locations_provinces,
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
    detail_map: dict[str, ListingDetail] = {}

    if scrape_details and listings_to_process:
        with get_session() as session:
            # detail_scraped_at n'est pose que par un scrape detail reussi
            # (source_id lu) ou par une page disparue (404): c'est le
            # marqueur fiable. `description` ne convient pas, elle est
            # regeneree pour toute annonce par build_contact_description.
            last_detail_scrape = dict(
                session.query(Listing.slug, Listing.detail_scraped_at)
            )

        urls_to_scrape = select_detail_urls(
            listings_to_process,
            last_detail_scrape,
            scan_time,
            settings.detail_refresh_days,
        )
        skipped = len(listings_to_process) - len(urls_to_scrape)

        console.print("\n[bold]Phase 2: Scraping détail...[/bold]")
        console.print(
            f"   {skipped} skippées (détail scrapé il y a moins de "
            f"{settings.detail_refresh_days} jours)"
        )
        console.print(f"   {len(urls_to_scrape)} à scraper")

        BATCH_SIZE = 20
        total_batches = max(1, (len(urls_to_scrape) - 1) // BATCH_SIZE + 1)

        async def run_detail_scraper():
            """Un seul client HTTP pour tous les lots.

            Un asyncio.run (et donc un client httpx) par lot refaisait une
            poignee de main TCP+TLS+HTTP/2 toutes les 20 pages, et forcait
            la reconstruction des primitives asyncio partagees.
            """
            async with ScraperClient() as client:
                for i in range(0, len(urls_to_scrape), BATCH_SIZE):
                    batch = urls_to_scrape[i:i + BATCH_SIZE]
                    console.print(
                        f"   Batch {i // BATCH_SIZE + 1}/{total_batches}: {len(batch)} pages"
                    )
                    results = await scrape_details_batch(batch, client)
                    detail_map.update(results)
                    # None = echec technique (compte comme erreur); page_gone =
                    # annonce retiree cote site, qui n'est pas un echec.
                    errors_in_batch = sum(1 for v in results.values() if v is None)
                    gone_in_batch = sum(1 for v in results.values() if v is not None and v.page_gone)
                    if errors_in_batch:
                        stats["errors"] += errors_in_batch
                        console.print(
                            f"   [yellow]⚠️  {errors_in_batch} erreurs dans ce batch[/yellow]"
                        )
                    if gone_in_batch:
                        stats["gone"] += gone_in_batch
                        console.print(f"   [dim]{gone_in_batch} page(s) détail disparue(s) (404)[/dim]")

        asyncio.run(run_detail_scraper())

        console.print(f"   [green]✅ {len(detail_map)} pages détail récupérées[/green]")
    else:
        console.print("\n[dim]Phase 2: Skippée (--no-scrape-details)[/dim]")

    # — Phase 3: Persistance en base —
    console.print("\n[bold]Phase 3: Mise à jour de la base de données...[/bold]")

    with get_session() as session:
        _persist_listings(
            session, listings_to_process, detail_map, scan_time, stats, seen_slugs, log
        )

        # Détecter les annonces supprimées (seulement si scan complet).
        # --max-pages tronque la collecte, et --locations-provinces ne revoit
        # qu'une partie des provinces par lieu: sans cette garde, les annonces
        # courte-durée des provinces non couvertes ce run-là verraient
        # missing_scan_count augmenter et basculeraient en "removed" à tort
        # au bout de 3 scans restreints.
        if not one_month_only and not province and not max_pages and not locations_provinces:
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


# Nombre d'annonces persistees entre deux commits de la phase 3. Borne ce
# qu'un rollback (voir _persist_listings) peut defaire.
PERSIST_COMMIT_EVERY = 100


def _persist_listings(
    session,
    listings_to_process: list,
    detail_map: dict,
    scan_time: datetime,
    stats: dict,
    seen_slugs: set,
    log,
) -> None:
    """Phase 3: upsert de chaque annonce collectee, en isolant les echecs.

    Une exception levee pendant le flush (contrainte violee, valeur trop
    longue pour Postgres...) laisse la session SQLAlchemy dans un etat
    "rollback en attente": chaque operation suivante releve la meme
    erreur. Avec un simple `continue`, toutes les annonces restantes
    passaient donc en erreur, puis mark_removed_listings et la mise a jour
    du ScanLog echouaient a leur tour -- le scan entier etait perdu pour
    une seule annonce.

    Chaque annonce est flushee aussitot, pour que l'erreur lui soit
    attribuee (et non au commit final, sans coupable). En cas d'echec, la
    session est remise en etat par un rollback: il defait au plus les
    annonces du lot courant, dont l'ecriture est reprise au scan suivant;
    un commit tous les PERSIST_COMMIT_EVERY borne cette perte.
    """
    # Compteurs du lot en cours, reportes dans `stats` seulement une fois le
    # lot ecrit: un rollback ne doit pas laisser dans le rapport des NEW ou
    # PRICE_CHANGED qui n'ont jamais atteint la base.
    pending = {"new": 0, "updated": 0, "price_changed": 0, "monthly": 0}

    # Une requete pour toutes les annonces connues (images comprises), au
    # lieu d'un SELECT par slug. `expire_on_commit=False`: sans cela, chaque
    # commit intermediaire expirait ces objets et le SELECT par annonce
    # revenait par la porte de derriere. Un rollback les expire quand meme,
    # ce qui ne coute qu'un rechargement du lot en cours.
    session.expire_on_commit = False
    existing = load_existing(session, [l.slug for l in listings_to_process])

    def _flush_pending() -> None:
        for key, value in pending.items():
            stats[key] += value
            pending[key] = 0

    since_commit = 0
    for listing_raw in listings_to_process:
        seen_slugs.add(listing_raw.slug)
        try:
            # Fusionner avec les données détail
            detail = detail_map.get(listing_raw.url)
            listing_full = _merge_listing(listing_raw, detail)

            change_type, db_listing = upsert_listing(session, listing_full, scan_time, existing)
            session.flush()
            if change_type == "NEW" and db_listing is not None:
                existing[listing_raw.slug] = db_listing

            if db_listing and listing_full.has_monthly_contract == "true":
                pending["monthly"] += 1

            if change_type == "NEW":
                pending["new"] += 1
            elif change_type == "UPDATED":
                pending["updated"] += 1
            elif change_type == "PRICE_CHANGED":
                pending["price_changed"] += 1

        except Exception as e:
            log.error(f"Erreur traitement {listing_raw.slug}: {e}")
            stats["errors"] += 1
            if since_commit:
                log.warning(
                    "%d annonce(s) du lot courant seront reprises au prochain scan",
                    since_commit,
                )
            session.rollback()
            for key in pending:
                pending[key] = 0
            since_commit = 0
            continue

        since_commit += 1
        if since_commit >= PERSIST_COMMIT_EVERY:
            session.commit()
            _flush_pending()
            since_commit = 0

    # Le dernier lot est commite par l'appelant (get_session), avec la
    # detection des suppressions et le ScanLog.
    _flush_pending()


def _merge_listing(listing_raw, detail) -> ListingFull:
    """Fusionne ListingRaw et ListingDetail en ListingFull.

    Attention: `{**raw, **detail}` fait *primer* le détail, y compris
    lorsqu'il porte une valeur par défaut. Le résultat n'est correct que
    parce que les deux modèles n'ont aujourd'hui aucun champ en commun
    (voir tests/test_bugfix_regression.py::test_raw_and_detail_schemas_stay_disjoint,
    qui verrouille cette propriété).

    Ajouter à ListingDetail un champ déjà porté par ListingRaw suffirait à
    casser silencieusement les invariants de scan: `from_structured_list`
    retomberait à False sur toute annonce dont la page détail a été lue, et
    apply_contract_fields cesserait de reconnaître les sources autoritaires.
    Un tel ajout impose de fusionner champ par champ.
    """
    raw_data = listing_raw.model_dump()
    detail_data = detail.model_dump() if detail else {}

    merged = {**raw_data, **detail_data}
    return ListingFull(**merged)


if __name__ == "__main__":
    main()
