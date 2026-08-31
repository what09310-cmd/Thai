"""
Scrape les pages individuelles d'annonces pour enrichir les données.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from src.scraper.http_client import ScraperClient
from src.parser.detail_parser import parse_detail_page
from src.models.schemas import ListingDetail

log = logging.getLogger(__name__)


async def scrape_detail(url: str, client: ScraperClient) -> Optional[ListingDetail]:
    """Scrape et parse une page d'annonce individuelle."""
    html = await client.get(url)
    if not html:
        return None
    return parse_detail_page(html, url)


async def scrape_details_batch(
    urls: list[str],
) -> dict[str, Optional[ListingDetail]]:
    """
    Scrape un lot d'URLs de pages détail.
    Retourne {url: ListingDetail | None}.
    """
    results: dict[str, Optional[ListingDetail]] = {}

    async with ScraperClient() as client:
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
