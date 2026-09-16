"""
Tests de non-régression de la passe d'audit (sécurité, robustesse du scan).

Même convention que test_bugfix_regression.py: un test par défaut corrigé,
dans l'ordre des couches -- API, authentification, couche réseau, parseur,
persistance.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

import src.api.auth as auth_module
import src.api.main as main_module
from src.api.auth import SESSION_COOKIE_NAME, create_session_token, get_or_create_google_user
from src.api.main import _client_key, _forwarded_https
from src.api.rate_limit import ANONYMOUS_MAX_PER_MINUTE
from src.config import settings
from src.database.models import Listing, ListingHistory, User
from src.models.schemas import ListingFull, ListingRaw
from src.parser.list_parser import extract_last_page_json, parse_listing_page_json
from src.scraper.http_client import RETRY_AFTER_DEFAULT_S, RETRY_AFTER_MAX_S, _retry_after_seconds
from src.tracker.change_detector import upsert_listing

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_login_attempts():
    auth_module._failed_attempts.clear()
    yield
    auth_module._failed_attempts.clear()


def _request(headers: dict[str, str] | None = None, client=("10.0.0.1", 1234)) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({
        "type": "http", "method": "GET", "path": "/listings", "query_string": b"",
        "headers": raw_headers, "client": client, "scheme": "http", "server": ("test", 80),
    })


# ── Clé client derrière un proxy inverse ─────────────────────────────
#
# Derrière le tunnel Cloudflare du lanceur (uvicorn sans --proxy-headers),
# request.client.host vaut 127.0.0.1 pour tout le monde: un seul budget de
# 60 req/min pour tous les visiteurs, et cinq mots de passe faux saisis par
# n'importe qui verrouillaient /login pour tous. Sur Render/Docker,
# --forwarded-allow-ips="*" prenait la *première* adresse de
# X-Forwarded-For, celle que le client écrit lui-même.

def test_forwarded_for_is_ignored_without_a_declared_proxy(monkeypatch):
    """Sans proxy déclaré, l'en-tête est une donnée du client: on l'ignore."""
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)
    request = _request({"X-Forwarded-For": "203.0.113.9"})
    assert _client_key(request) == "10.0.0.1"


def test_forwarded_for_is_read_from_the_end_behind_a_proxy(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    # Le client a écrit "1.1.1.1", le proxy de confiance a ajouté la vraie adresse.
    assert _client_key(_request({"X-Forwarded-For": "1.1.1.1, 203.0.113.9"})) == "203.0.113.9"
    assert _client_key(_request({"X-Forwarded-For": "203.0.113.9"})) == "203.0.113.9"

    monkeypatch.setattr(settings, "trusted_proxy_hops", 2)
    assert _client_key(_request({"X-Forwarded-For": "1.1.1.1, 203.0.113.9, 10.9.9.9"})) == "203.0.113.9"


def test_forwarded_for_too_short_falls_back_to_the_socket_address(monkeypatch):
    """Accès direct malgré la configuration proxy: pas d'en-tête, ou trop court."""
    monkeypatch.setattr(settings, "trusted_proxy_hops", 2)
    assert _client_key(_request({"X-Forwarded-For": "203.0.113.9"})) == "10.0.0.1"
    assert _client_key(_request()) == "10.0.0.1"


def test_forwarded_proto_is_only_trusted_behind_a_proxy(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)
    assert _forwarded_https(_request({"X-Forwarded-Proto": "https"})) is False
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    assert _forwarded_https(_request({"X-Forwarded-Proto": "https"})) is True
    assert _forwarded_https(_request({"X-Forwarded-Proto": "http"})) is False
    assert _forwarded_https(_request()) is False


def test_rate_limit_is_per_visitor_behind_a_proxy(client, session, monkeypatch):
    """Deux visiteurs derrière le même tunnel ne partagent plus un budget."""
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    for _ in range(ANONYMOUS_MAX_PER_MINUTE):
        assert client.get("/listings", headers={"X-Forwarded-For": "203.0.113.1"}).status_code == 200
    assert client.get("/listings", headers={"X-Forwarded-For": "203.0.113.1"}).status_code == 429
    assert client.get("/listings", headers={"X-Forwarded-For": "203.0.113.2"}).status_code == 200


def test_login_lock_is_per_visitor_behind_a_proxy(client, monkeypatch):
    """Les échecs d'un visiteur ne verrouillent plus la connexion des autres."""
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    attacker = {"X-Forwarded-For": "203.0.113.1"}
    for _ in range(auth_module._LOGIN_ATTEMPT_MAX):
        client.post("/login", data={"username": "admin", "password": "wrong"}, headers=attacker)
    assert client.post(
        "/login", data={"username": "admin", "password": "wrong"}, headers=attacker
    ).status_code == 429

    victim = {"X-Forwarded-For": "203.0.113.2"}
    ok = client.post(
        "/login",
        data={"username": settings.site_username, "password": settings.site_password},
        headers=victim, follow_redirects=False,
    )
    assert ok.status_code == 303


# ── Connexion Google: email non vérifié ──────────────────────────────
#
# `users.email` est unique. Un email non vérifié ne rattachait pas un compte
# existant (correct), mais servait à en *créer* un: IntegrityError, donc
# 500, dès qu'un compte à mot de passe portait déjà l'adresse -- et quand
# l'insertion passait, elle réservait l'adresse à quelqu'un qui n'en avait
# pas prouvé la propriété.

def test_unverified_google_email_never_links_nor_creates(session):
    existing = User(email="alice@example.com", password_hash="x")
    session.add(existing)
    session.commit()

    assert get_or_create_google_user(session, "sub-1", "alice@example.com", False) is None
    session.refresh(existing)
    assert existing.google_sub is None

    assert get_or_create_google_user(session, "sub-2", "nobody@example.com", False) is None
    assert session.query(User).count() == 1


def test_verified_google_email_links_the_existing_account(session):
    existing = User(email="alice@example.com", password_hash="x")
    session.add(existing)
    session.commit()

    user = get_or_create_google_user(session, "sub-1", "Alice@Example.com", True)
    assert user is not None and user.id == existing.id
    assert user.google_sub == "sub-1"


def test_google_account_known_by_sub_logs_in_regardless_of_email_verification(session):
    session.add(User(email="bob@example.com", google_sub="sub-bob"))
    session.commit()

    user = get_or_create_google_user(session, "sub-bob", "bob@example.com", False)
    assert user is not None and user.email == "bob@example.com"
    assert user.last_login_at is not None


# ── Inscription: course sur l'unicité de l'email ─────────────────────

def test_concurrent_registration_of_the_same_email_is_a_409_not_a_500(client, session, monkeypatch):
    """Le test d'unicité passe, puis la contrainte de la table refuse."""
    session.add(User(email="dup@example.com", password_hash="x"))
    session.commit()
    # Simule la fenêtre de course: l'autre inscription n'est pas encore visible.
    monkeypatch.setattr(main_module, "get_user_by_email", lambda db, email: None)

    resp = client.post("/register", data={"username": "dup@example.com", "password": "long-enough"})

    assert resp.status_code == 409
    assert session.query(User).count() == 1
    # La session est réutilisable après le rollback.
    assert session.query(User).filter_by(email="dup@example.com").one().password_hash == "x"


# ── Champs contact: line_verified ────────────────────────────────────

def test_anonymous_listings_hide_line_verified(client, session):
    """`line_verified` qualifie un contact: il suit `line_id`."""
    session.add(
        Listing(
            slug="s", name="S", url="https://www.renthub.in.th/en/s", status="active",
            line_id="@landlord", line_verified=True,
        )
    )
    session.commit()

    anon = client.get("/listings").json()[0]
    assert anon["line_id"] is None and anon["line_verified"] is None

    client.cookies.set(SESSION_COOKIE_NAME, create_session_token())
    authed = client.get("/listings").json()[0]
    assert authed["line_id"] == "@landlord" and authed["line_verified"] is True


# ── Couche réseau: Retry-After ───────────────────────────────────────

def test_retry_after_accepts_seconds_and_http_dates_and_is_bounded():
    assert _retry_after_seconds(None) == RETRY_AFTER_DEFAULT_S
    assert _retry_after_seconds("") == RETRY_AFTER_DEFAULT_S
    assert _retry_after_seconds("30") == 30
    assert _retry_after_seconds("0") == 1
    assert _retry_after_seconds("999999") == RETRY_AFTER_MAX_S
    assert _retry_after_seconds("garbage") == RETRY_AFTER_DEFAULT_S

    soon = datetime.now(timezone.utc) + timedelta(seconds=45)
    http_date = soon.strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert 40 <= _retry_after_seconds(http_date) <= 45
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert _retry_after_seconds(past) == 1


# ── Parseur de liste: robustesse au JSON ─────────────────────────────

def _list_page_html(listings, pagination=None) -> str:
    import json

    props = {"listings": listings}
    if pagination is not None:
        props["pagination"] = pagination
    payload = {"props": {"pageProps": props}}
    return f'<html><body><script id="__NEXT_DATA__">{json.dumps(payload)}</script></body></html>'


def _card(slug: str, **extra) -> dict:
    return {"slug": slug, "name": slug.title(), "price": {"monthly": {"type": "AMOUNT", "minPrice": 5000, "maxPrice": 6000}}, **extra}


def test_a_malformed_json_card_does_not_break_the_page():
    """Une carte hors format était une exception non rattrapée: le scan
    entier tombait pour une seule carte."""
    html = _list_page_html([
        _card("good-1"),
        {"slug": "bad-price", "name": "Bad", "price": {"monthly": {"type": "AMOUNT", "minPrice": "cinq mille"}}},
        "not-even-a-dict",
        {"slug": "bad-district", "name": "Bad", "district": ["a", "list"]},
        _card("good-2"),
    ])

    listings = parse_listing_page_json(html)

    assert [l.slug for l in listings] == ["good-1", "good-2"]
    assert listings[0].price_monthly_min == 5000


def test_total_pages_is_validated_before_use():
    assert extract_last_page_json(_list_page_html([], {"totalPages": 3})) == 3
    assert extract_last_page_json(_list_page_html([], {"totalPages": "3"})) == 3
    assert extract_last_page_json(_list_page_html([], {"totalPages": "trois"})) is None
    assert extract_last_page_json(_list_page_html([], {"totalPages": None})) is None
    assert extract_last_page_json(_list_page_html([], {"totalPages": 0})) is None
    assert extract_last_page_json(_list_page_html([], {"totalPages": True})) is None
    assert extract_last_page_json(_list_page_html([])) is None


# ── Persistance: dates en UTC ────────────────────────────────────────

def test_aware_source_dates_are_stored_in_utc(session):
    """Le repli HTML produit des dates Asia/Bangkok; SQLite écarte le fuseau
    à l'écriture, donc elles étaient stockées avec 7 h d'avance."""
    bangkok = datetime(2026, 3, 1, 9, 30, tzinfo=ZoneInfo("Asia/Bangkok"))
    _, db_listing = upsert_listing(
        session,
        ListingFull(name="X", url="https://www.renthub.in.th/en/x", slug="x", source_updated_at=bangkok),
        NOW,
    )
    session.commit()
    session.refresh(db_listing)

    stored = db_listing.source_updated_at
    if stored.tzinfo is None:  # SQLite rend des datetimes naïfs
        stored = stored.replace(tzinfo=timezone.utc)
    assert stored == bangkok.astimezone(timezone.utc)
    assert stored.hour == 2  # 09:30 à Bangkok = 02:30 UTC


# ── Persistance: une annonce en échec ne perd plus le scan ───────────
#
# Après un flush en erreur, la session SQLAlchemy refuse toute opération
# jusqu'au rollback: avec un simple `continue`, chaque annonce suivante
# échouait, puis la détection des suppressions et le ScanLog -- le scan
# entier était perdu pour une seule annonce.

def _raw(slug: str) -> ListingRaw:
    return ListingRaw(name=slug, url=f"https://www.renthub.in.th/en/{slug}", slug=slug)


def test_a_failing_listing_does_not_poison_the_rest_of_the_scan(session, monkeypatch):
    import scripts.run_scraper as runner

    real_upsert = runner.upsert_listing

    def upsert_with_one_broken_row(db, listing, scan_time, existing=None):
        result = real_upsert(db, listing, scan_time, existing)
        if listing.slug == "broken":
            # Ligne invalide (listing_id NOT NULL): l'erreur sort au flush,
            # exactement comme une contrainte violée en production.
            db.add(ListingHistory(listing_id=None, change_type="NEW", changed_at=scan_time))
        return result

    monkeypatch.setattr(runner, "upsert_listing", upsert_with_one_broken_row)
    monkeypatch.setattr(runner, "PERSIST_COMMIT_EVERY", 2)

    stats = {"new": 0, "updated": 0, "price_changed": 0, "monthly": 0, "errors": 0}
    seen: set[str] = set()
    listings = [_raw("a"), _raw("b"), _raw("broken"), _raw("c"), _raw("d")]

    runner._persist_listings(session, listings, {}, NOW, stats, seen, runner.logging.getLogger("t"))
    session.commit()

    slugs = {l.slug for l in session.query(Listing).all()}
    # a et b ont été commités avant l'échec; c et d après; "broken" a été défait.
    assert slugs == {"a", "b", "c", "d"}
    assert stats["errors"] == 1
    assert stats["new"] == 4
    assert seen == {"a", "b", "broken", "c", "d"}


def test_a_rollback_forgets_the_uncommitted_counters(session, monkeypatch):
    """Les NEW du lot défait ne doivent pas figurer dans le rapport."""
    import scripts.run_scraper as runner

    def upsert_raising_on(db, listing, scan_time, existing=None):
        if listing.slug == "broken":
            raise IntegrityError("INSERT", {}, Exception("boom"))
        return upsert_listing(db, listing, scan_time, existing)

    monkeypatch.setattr(runner, "upsert_listing", upsert_raising_on)
    monkeypatch.setattr(runner, "PERSIST_COMMIT_EVERY", 100)

    stats = {"new": 0, "updated": 0, "price_changed": 0, "monthly": 0, "errors": 0}
    runner._persist_listings(
        session, [_raw("a"), _raw("broken"), _raw("c")], {}, NOW, stats, set(),
        runner.logging.getLogger("t"),
    )
    session.commit()

    assert {l.slug for l in session.query(Listing).all()} == {"c"}
    assert stats["new"] == 1
    assert stats["errors"] == 1


# ── Page détail disparue: ni une erreur, ni une page à redemander ────
#
# Le client HTTP rend "" pour un 404 (annonce retirée) et None pour un échec
# technique; scrape_detail confondait les deux en None, si bien qu'une page
# disparue comptait comme une erreur de scan et, faute de detail_scraped_at,
# était redemandée à chaque scan jusqu'à ce que la page de liste la retire.

def test_a_vanished_detail_page_is_not_an_error_and_is_not_refetched(session):
    import asyncio

    from src.models.schemas import ListingDetail
    from src.scraper.detail_scraper import scrape_detail

    class GoneClient:
        async def get(self, url):
            return ""

    class DownClient:
        async def get(self, url):
            return None

    gone = asyncio.run(scrape_detail("https://www.renthub.in.th/en/x", GoneClient()))
    assert isinstance(gone, ListingDetail) and gone.page_gone and not gone.has_structured_data
    assert asyncio.run(scrape_detail("https://www.renthub.in.th/en/x", DownClient())) is None

    # Persistée, la page disparue marque detail_scraped_at sans toucher aux
    # contacts déjà connus.
    upsert_listing(session, ListingFull(**_raw("x").model_dump(), phone="0812345678", source_id="1"), NOW)
    session.commit()
    later = NOW + timedelta(days=1)
    _, db_listing = upsert_listing(session, ListingFull(**_raw("x").model_dump(), page_gone=True), later)
    session.commit()
    session.refresh(db_listing)
    assert db_listing.phone == "0812345678"
    assert db_listing.detail_scraped_at.replace(tzinfo=timezone.utc) == later


def test_a_zero_coordinate_is_not_mistaken_for_a_missing_one(session):
    """`listing.latitude or db_listing.latitude` ignorait une coordonnée 0.0."""
    upsert_listing(session, ListingFull(**_raw("z").model_dump(), latitude=13.7, longitude=100.5), NOW)
    session.commit()
    _, db_listing = upsert_listing(
        session, ListingFull(**_raw("z").model_dump(), latitude=0.0, longitude=0.0), NOW
    )
    session.commit()
    session.refresh(db_listing)
    assert (db_listing.latitude, db_listing.longitude) == (0.0, 0.0)


def test_home_page_served_for_a_delisted_listing_counts_as_gone():
    """RentHub sert sa page d'accueil (JSON sans `listing`) pour une annonce
    retirée: c'est une page disparue, pas une fiche à gratter au regex."""
    from src.parser.detail_parser import parse_detail_page

    home = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        '{"props": {"pageProps": {"featured": []}}}</script>'
        "<p>Call 0812345678 now</p></body></html>"
    )
    detail = parse_detail_page(home, "https://www.renthub.in.th/en/gone")
    assert detail is not None and detail.page_gone
    assert detail.phone is None and detail.has_structured_data is False
