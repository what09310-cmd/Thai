from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

from src.models.schemas import ListingRaw
from src.parser.list_parser import extract_last_page, parse_listing_page
from src.scraper.http_client import ScraperClient

log = logging.getLogger(__name__)

BASE_URL = "https://www.renthub.in.th"
BROWSE_URL = f"{BASE_URL}/en/browse/short-term-monthly"

# Les 77 provinces officielles de Thaïlande -> slug utilisé par renthub.
# (la page /en/browse/provinces mélange aussi les quartiers de Bangkok,
# on ne garde que les vraies provinces)
THAI_PROVINCES = {
    "Amnat Charoen": "amnat-charoen", "Ang Thong": "angthong", "Bangkok": "bangkok",
    "Bueng Kan": "bueng-kan", "Buri Ram": "buri-ram", "Chachoengsao": "chachoengsao",
    "Chai Nat": "chainat", "Chaiyaphum": "chaiyaphum", "Chanthaburi": "chanthaburi",
    "Chiang Mai": "chiang-mai", "Chiang Rai": "chiang-rai", "Chonburi": "chonburi",
    "Chumphon": "chumphon", "Kalasin": "kalasin", "Kamphaeng Phet": "kamphaeng-phet",
    "Kanchanaburi": "kanchanaburi", "Khon Kaen": "khon-kaen", "Krabi": "krabi",
    "Lampang": "lamphang", "Lamphun": "lamphun", "Loei": "loei", "Lopburi": "lopburi",
    "Mae Hong Son": "mae-hong-son", "Maha Sarakham": "maha-sarakham", "Mukdahan": "mukdahan",
    "Nakhon Nayok": "nakhon-nayok", "Nakhon Pathom": "nakhon-pathom", "Nakhon Phanom": "nakhon-phanom",
    "Nakhon Ratchasima": "nakhon-ratchasima", "Nakhon Sawan": "nakhon-sawan",
    "Nakhon Si Thammarat": "nakhon-sri-thammarat", "Nan": "nan", "Narathiwat": "narathiwat",
    "Nong Bua Lam Phu": "nong-bua-lam-phu", "Nong Khai": "nongkai", "Nonthaburi": "nonthaburi",
    "Pathum Thani": "pathumthani", "Pattani": "pattani", "Phang Nga": "phangnga",
    "Phatthalung": "phatthalung", "Phayao": "phayao", "Phetchabun": "phetchabun",
    "Phetchaburi": "petchburi", "Phichit": "phichit", "Phitsanulok": "phitsanulok",
    "Phra Nakhon Si Ayutthaya": "phra-nakhon-sri-ayutthaya", "Phrae": "phrae", "Phuket": "phuket",
    "Prachin Buri": "prachinburi", "Prachuap Khiri Khan": "prachaubkirikhan", "Ranong": "ranong",
    "Ratchaburi": "ratchburi", "Rayong": "rayong", "Roi Et": "roi-et", "Sa Kaeo": "srakaeo",
    "Sakon Nakhon": "sakon-nakhon", "Samut Prakan": "samut-prakarn", "Samut Sakhon": "samut-sakhon",
    "Samut Songkhram": "samut-songkram", "Saraburi": "saraburi", "Satun": "satun",
    "Si Sa Ket": "si-sa-ket", "Sing Buri": "singburi", "Songkhla": "songkhla", "Sukhothai": "sukhothai",
    "Suphan Buri": "suphanburi", "Surat Thani": "surat-thani", "Surin": "surin", "Tak": "tak",
    "Trang": "trang", "Trat": "trat", "Ubon Ratchathani": "ubon-ratchathani", "Udon Thani": "udon-thani",
    "Uthai Thani": "uthai-thani", "Uttaradit": "uttaradit", "Yala": "yala", "Yasothon": "yasothon",
}


async def scrape_all_listings(
    max_pages: Optional[int] = None,
    province_slug: Optional[str] = None,
) -> AsyncIterator[tuple[ListingRaw, str]]:
    """
    Scrape toutes les annonces de la page /en/browse/short-term-monthly.

    province_slug filtre sur une province (ex: "bangkok"), via
    /en/browse/short-term-monthly/<province_slug>.

    Rend des couples (annonce, url_de_la_page): l'appelant a besoin de la
    page d'origine pour compter les pages réellement lues, que le nombre
    d'annonces par page ne permet pas de déduire.
    """

    async with ScraperClient() as client:

        base = (
            f"{BROWSE_URL}/{province_slug}"
            if province_slug
            else BROWSE_URL
        )
        base = base.rstrip("/")

        # ==========================================
        # PAGE 1
        # ==========================================

        log.info(f"Scraping page 1: {base}")

        html = await client.get(base)

        if not html:
            log.error(
                f"Impossible de charger la page 1 : {base}"
            )
            return

        # Le marqueur de pagination doit inclure le slug de province,
        # sinon les liens .../short-term-monthly/<slug>/2 ne matchent pas
        # et le repli HTML ne détecte qu'une seule page.
        last_page = extract_last_page(
            html,
            path_marker=(
                f"short-term-monthly/{province_slug}"
                if province_slug
                else "short-term-monthly"
            ),
        )

        log.info(
            f"Dernière page détectée: {last_page}"
        )

        limit = (
            min(last_page, max_pages)
            if max_pages
            else last_page
        )

        listings = parse_listing_page(html)

        log.info(
            f"Page 1: {len(listings)} annonces"
        )

        for listing in listings:
            yield listing, base

        # ==========================================
        # PAGES SUIVANTES
        # ==========================================

        for page_num in range(2, limit + 1):

            url = f"{base}/{page_num}"

            log.info(
                f"Scraping page "
                f"{page_num}/{limit}: {url}"
            )

            html = await client.get(url)

            # Ne pas arrêter tout le scraping
            if not html:

                log.warning(
                    f"Page {page_num} inaccessible : {url}"
                )

                continue

            listings = parse_listing_page(html)

            # Page suspecte
            if not listings:

                log.warning(
                    f"Page {page_num} chargée mais "
                    f"0 annonce trouvée : {url}"
                )

            log.info(
                f"Page {page_num}: "
                f"{len(listings)} annonces"
            )

            for listing in listings:
                yield listing, url


async def scrape_location_listings(
    location_url: str,
    max_pages: Optional[int] = None,
) -> AsyncIterator[ListingRaw]:
    """
    Scrape les annonces d'une page "par lieu" du type
    /en/short-term-rental/<slug>, paginée en /<slug>/2, /<slug>/3, ...
    Ces pages n'ont presque jamais de contrat structuré (1/3/6 mois),
    juste un prix THB/month générique.
    """
    async with ScraperClient() as client:

        base = location_url.rstrip("/")
        path_marker = base.replace(BASE_URL, "").lstrip("/")

        html = await client.get(base)

        if not html:
            log.warning(f"Impossible de charger : {base}")
            return

        last_page = extract_last_page(html, path_marker=path_marker)
        limit = min(last_page, max_pages) if max_pages else last_page

        listings = parse_listing_page(html)
        log.info(f"{path_marker} page 1: {len(listings)} annonces")

        for listing in listings:
            yield listing

        for page_num in range(2, limit + 1):

            url = f"{base}/{page_num}"
            html = await client.get(url)

            if not html:
                log.warning(f"Page {page_num} inaccessible : {url}")
                continue

            listings = parse_listing_page(html)
            log.info(f"{path_marker} page {page_num}: {len(listings)} annonces")

            for listing in listings:
                yield listing


async def scrape_all_location_listings(
    max_pages_per_location: Optional[int] = None,
    provinces: Optional[list[str]] = None,
) -> AsyncIterator[ListingRaw]:
    """
    Parcourt les provinces thaïlandaises via /en/short-term-rental/<slug>
    et scrape celles qui ont du contenu (les autres sont simplement
    ignorées).

    `provinces`, si fourni, restreint le parcours à ces slugs (ex:
    ["bangkok"]) au lieu des 77 provinces de THAI_PROVINCES — pour les
    scans fréquents où seul le plus gros marché doit être revu à chaque
    fois, la couverture complète restant réservée à un scan périodique
    sans ce filtre.
    """
    items = THAI_PROVINCES.items()
    if provinces is not None:
        wanted = set(provinces)
        items = [(name, slug) for name, slug in items if slug in wanted]

    for name, slug in items:
        url = f"{BASE_URL}/en/short-term-rental/{slug}"
        count = 0
        async for listing in scrape_location_listings(url, max_pages=max_pages_per_location):
            count += 1
            yield listing
        log.info(f"{name}: {count} annonces via short-term-rental")
