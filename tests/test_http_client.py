"""
Tests de la couche reseau du scraper.

Ce module n'avait aucune couverture: la garde anti-SSRF ajoutee ici
(validation de l'hote a chaque saut de redirection) est justement le genre
de changement qu'on ne peut pas livrer sans preuve.
"""
from __future__ import annotations

import httpx
import pytest

from src.config import settings
from src.scraper.http_client import ScraperClient, is_allowed_url

LISTING_URL = "https://www.renthub.in.th/en/some-listing"


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    """REQUEST_DELAY vaut 2 s en configuration reelle."""
    monkeypatch.setattr(settings, "request_delay", 0.0)


async def _client_with(handler) -> ScraperClient:
    """ScraperClient dont le transport est simule (aucun reseau)."""
    client = ScraperClient()
    await client.__aenter__()
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    return client


# ── is_allowed_url ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "url",
    [
        "https://www.renthub.in.th/en/x",
        "https://renthub.in.th/en/x",
        "http://www.renthub.in.th/en/x",
    ],
)
def test_renthub_urls_are_allowed(url):
    assert is_allowed_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/admin",
        "http://169.254.169.254/latest/meta-data/",   # metadonnees cloud
        "http://postgres:5432/",                       # reseau docker-compose
        "https://evil.example.com/renthub.in.th",
        "https://renthub.in.th.evil.com/x",            # suffixe trompeur
        "file:///etc/passwd",
        "https://user@renthub.in.th.evil.com/x",
    ],
)
def test_foreign_urls_are_rejected(url):
    assert is_allowed_url(url) is False


# ── Requetes ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_refuses_a_foreign_url_without_touching_the_network():
    """Une URL empoisonnee en base ne doit meme pas partir sur le reseau."""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text="<html>secret</html>")

    client = await _client_with(handler)
    try:
        assert await client.get("http://169.254.169.254/latest/meta-data/") is None
        assert calls == []
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_redirect_inside_the_domain_is_followed():
    """Le site redirige legitimement (slash final, http->https)."""
    def handler(request):
        if request.url.path == "/en/old":
            return httpx.Response(301, headers={"location": "/en/new"})
        return httpx.Response(200, text="<html>page</html>")

    client = await _client_with(handler)
    try:
        assert await client.get("https://www.renthub.in.th/en/old") == "<html>page</html>"
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_redirect_leaving_the_domain_is_not_followed():
    """Le coeur de la garde: `follow_redirects=True` faisait confiance a
    toute la chaine sur la foi de sa premiere URL."""
    reached = []

    def handler(request):
        reached.append(request.url.host)
        if request.url.host == "www.renthub.in.th":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/"})
        return httpx.Response(200, text="SECRET")

    client = await _client_with(handler)
    try:
        assert await client.get(LISTING_URL) == ""
        assert reached == ["www.renthub.in.th"]
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_redirect_loop_is_bounded():
    """Une boucle de redirections ne doit pas tourner indefiniment."""
    hops = []

    def handler(request):
        hops.append(str(request.url))
        return httpx.Response(302, headers={"location": "/en/loop"})

    client = await _client_with(handler)
    try:
        assert await client.get("https://www.renthub.in.th/en/loop") == ""
        assert len(hops) <= 6
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_404_returns_empty_not_none():
    """Comportement preexistant a preserver: 404 -> "" (annonce retiree),
    tandis que None signale un echec technique."""
    client = await _client_with(lambda request: httpx.Response(404))
    try:
        assert await client.get(LISTING_URL) == ""
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_429_is_replayed_in_place_after_retry_after(monkeypatch):
    """Un 429 attend Retry-After puis rejoue l'URL dans la boucle: lever une
    exception ici faisait attendre tenacity une seconde fois (4 a 30 s)."""
    import src.scraper.http_client as http_client

    waits: list[float] = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(http_client.asyncio, "sleep", fake_sleep)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, text="<html>ok</html>")

    client = await _client_with(handler)
    try:
        assert await client.get(LISTING_URL) == "<html>ok</html>"
    finally:
        await client._client.aclose()
    assert calls == ["/en/some-listing", "/en/some-listing"]
    assert waits == [7]
