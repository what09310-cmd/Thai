"""
Scrape les pages individuelles d'annonces pour enrichir les données.
"""
from __future__ import annotations

import logging
from typing import Optional

from src.models.schemas import ListingDetail
from src.parser.detail_parser import parse_detail_page
from src.scraper.http_client import ScraperClient

log = logging.getLogger(__name__)


async def scrape_detail(url: str, client: ScraperClient) -> Optional[ListingDetail]:
    """Scrape et parse une page d'annonce individuelle."""
    html = await client.get(url)
    if html is None:
        return None  # echec technique (reseau, 5xx apres retries): a recompter
    if html == "":
        # 404 ou redirection hors domaine: la page n'existe plus. Ce n'est
        # pas une erreur de scan, et rien ne doit ecraser les champs deja
        # connus (has_structured_data reste False).
        return ListingDetail(page_gone=True)
    return parse_detail_page(html, url)


async def scrape_details_batch(
    urls: list[str],
    client: Optional[ScraperClient] = None,
) -> dict[str, Optional[ListingDetail]]:
    """
    Scrape un lot d'URLs de pages détail.
    Retourne {url: ListingDetail | None}.

    `client` permet de partager une connexion entre plusieurs lots: en
    ouvrir un par lot de 20 refaisait une poignée de main TCP+TLS+HTTP/2
    toutes les 20 pages. Sans argument, un client est créé pour l'appel.
    """
    if client is None:
        async with ScraperClient() as owned_client:
            return await scrape_details_batch(urls, owned_client)

    results: dict[str, Optional[ListingDetail]] = {}

    # Traitement séquentiel contrôlé par le semaphore du client
    for url in urls:
        log.debug(f"Scraping détail: {url}")
        detail = await scrape_detail(url, client)
        results[url] = detail
        if detail:
            log.debug(f"  -> source_id={detail.source_id}, amenities={len(detail.amenities)}")
        else:
            log.warning(f"  -> Échec scraping détail {url}")

    return results
