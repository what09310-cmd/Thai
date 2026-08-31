"""
Client HTTP avec retry exponentiel, rate limiting et user-agent rotatif.
Respecte REQUEST_DELAY entre chaque requête.
Ne contourne aucun système anti-bot ou CAPTCHA.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from src.config import settings

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,th;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

_last_request_time: float = 0.0
_semaphore: Optional[asyncio.Semaphore] = None
_throttle_lock: Optional[asyncio.Lock] = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
    return _semaphore


def _get_throttle_lock() -> asyncio.Lock:
    global _throttle_lock
    if _throttle_lock is None:
        _throttle_lock = asyncio.Lock()
    return _throttle_lock


async def _throttle() -> None:
    """Respecte REQUEST_DELAY entre deux requêtes (global).

    Le verrou est indispensable: sans lui, deux tâches concurrentes lisent
    le même _last_request_time, calculent le même délai et repartent
    ensemble — REQUEST_DELAY se retrouve divisé par MAX_CONCURRENT_REQUESTS.
    """
    global _last_request_time
    async with _get_throttle_lock():
        now = time.monotonic()
        elapsed = now - _last_request_time
        delay = settings.request_delay
        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)
        _last_request_time = time.monotonic()


class ScraperClient:
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            headers=HEADERS,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            http2=True,
        )
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    async def get(self, url: str) -> Optional[str]:
        """
        Effectue une requête GET avec retry et throttling.
        Retourne le HTML ou None en cas d'échec définitif.
        """
        sem = _get_semaphore()
        async with sem:
            await _throttle()
            try:
                return await self._get_with_retry(url)
            except Exception as e:
                log.error(f"Échec définitif pour {url}: {e}")
                return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=4, max=30),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )
    async def _get_with_retry(self, url: str) -> str:
        assert self._client is not None
        response = await self._client.get(url)

        if response.status_code == 404:
            log.warning(f"404 pour {url}")
            return ""

        if response.status_code == 429:
            # Rate limited: attendre plus longtemps
            wait_time = int(response.headers.get("Retry-After", 60))
            log.warning(f"Rate limited (429), attente {wait_time}s")
            await asyncio.sleep(wait_time)
            raise httpx.TimeoutException(f"Rate limited, retrying after {wait_time}s")

        if response.status_code >= 500:
            raise httpx.NetworkError(f"Erreur serveur {response.status_code}")

        response.raise_for_status()
        return response.text
