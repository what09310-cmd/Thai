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
from urllib.parse import urlparse
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

# Hotes que ce client accepte de joindre. Les URLs scrapees viennent du
# contenu du site lui-meme et sont rejouees telles quelles par cinq scripts
# de maintenance (refix_*, refresh_*, rescrape_*): une URL empoisonnee, ou
# une simple redirection, suffisait a faire interroger 127.0.0.1,
# 169.254.169.254 (metadonnees cloud) ou `postgres:5432` sur le reseau
# interne de docker-compose, et a ranger la reponse en base.
#
# Le controle est ici, dans la couche reseau, plutot que chez chaque
# appelant: les deux scrapers et les cinq scripts en beneficient sans
# modification. `src/scraper/geo_discovery.py::is_valid_renthub_url` fait
# la meme verification pour son propre usage, sans passer par ce client.
ALLOWED_HOSTS = frozenset({"renthub.in.th", "www.renthub.in.th"})

# Les redirections sont suivies a la main (voir _get_with_retry) pour
# pouvoir revalider l'hote a *chaque* saut: `follow_redirects=True` faisait
# confiance a la chaine entiere sur la foi de sa premiere URL.
MAX_REDIRECTS = 5
_REDIRECT_CODES = (301, 302, 303, 307, 308)


def is_allowed_url(url: str) -> bool:
    """Vrai si l'URL vise RentHub en http(s)."""
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and parsed.hostname in ALLOWED_HOSTS


# `time.monotonic` est global au processus: il traverse sans probleme
# plusieurs boucles d'evenements successives, contrairement aux primitives
# asyncio ci-dessous.
_last_request_time: float = 0.0

# Sémaphore et verrou sont liés à la boucle d'événements qui les utilise en
# premier: réutilisés depuis une autre boucle (le scan appelle asyncio.run
# une fois par phase), ils lèvent "is bound to a different event loop" dès
# qu'il y a vraiment contention. Ils sont donc reconstruits au changement de
# boucle. Tant que le scraping restait strictement séquentiel, la contention
# n'arrivait jamais et le défaut passait inaperçu.
_primitives_loop: Optional[asyncio.AbstractEventLoop] = None
_semaphore: Optional[asyncio.Semaphore] = None
_throttle_lock: Optional[asyncio.Lock] = None


def _reset_primitives_if_loop_changed() -> None:
    global _primitives_loop, _semaphore, _throttle_lock
    loop = asyncio.get_running_loop()
    if _primitives_loop is not loop:
        _primitives_loop = loop
        _semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
        _throttle_lock = asyncio.Lock()


def _get_semaphore() -> asyncio.Semaphore:
    _reset_primitives_if_loop_changed()
    assert _semaphore is not None
    return _semaphore


def _get_throttle_lock() -> asyncio.Lock:
    _reset_primitives_if_loop_changed()
    assert _throttle_lock is not None
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
            follow_redirects=False,
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
        if not is_allowed_url(url):
            log.error(f"URL hors du domaine RentHub, requete refusee: {url}")
            return None
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
        current = url

        for _ in range(MAX_REDIRECTS + 1):
            response = await self._client.get(current)

            if response.status_code in _REDIRECT_CODES:
                location = response.headers.get("location", "")
                # Une Location relative est resolue contre l'URL courante,
                # comme le ferait le navigateur.
                target = str(httpx.URL(current).join(location)) if location else ""
                if not target or not is_allowed_url(target):
                    log.warning(
                        f"Redirection hors domaine ignoree: {current} -> "
                        f"{target or '(Location absente)'}"
                    )
                    return ""
                current = target
                continue

            if response.status_code == 404:
                log.warning(f"404 pour {current}")
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

        log.warning(f"Trop de redirections ({MAX_REDIRECTS}) pour {url}")
        return ""
