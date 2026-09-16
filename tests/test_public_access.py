"""Ce qu'un visiteur non authentifie peut obtenir de l'API.

`/listings*` et `/stats` sont publics: la vitrine `/test` les appelle sans
session. Publics ne veut pas dire complets -- ces tests fixent la frontiere
entre ce qui sert la demonstration et ce qui constitue le produit vendu.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.api.auth import SESSION_COOKIE_NAME, create_session_token
from src.database.models import Listing


def _add_listings(session, count: int) -> None:
    """`count` annonces actives, toutes porteuses de donnees precieuses."""
    base = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    for i in range(count):
        session.add(
            Listing(
                slug=f"listing-{i}",
                name=f"Listing {i}",
                url=f"https://www.renthub.in.th/en/listing-{i}",
                address=f"{i} Sukhumvit Road",
                latitude=13.7 + i / 1000,
                longitude=100.5 + i / 1000,
                province="Bangkok",
                district="Watthana",
                status="active",
                has_monthly_contract="true",
                contract_monthly_min=15000,
                source_updated_at=base - timedelta(minutes=i),
                first_seen_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        )
    session.commit()


def _authenticate(client) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, create_session_token())


# ── Volume ───────────────────────────────────────────────────────────

def test_anonymous_listings_get_the_full_catalogue(client, session):
    """Les cartes (carte-*.html) sont publiques: sans le catalogue entier
    et ses coordonnees, elles n'ont rien a afficher."""
    _add_listings(session, 32)
    body = client.get("/listings?limit=500").json()
    assert len(body) == 32
    first = [l["id"] for l in client.get("/listings?limit=12").json()]
    later = [l["id"] for l in client.get("/listings?limit=12&offset=12").json()]
    assert later != first


# ── Echantillon: contenu ─────────────────────────────────────────────

_PRECIOUS = ("address", "url")


def test_anonymous_listings_hide_address_and_source_url(client, session):
    """Adresse exacte et lien source constituent le produit vendu.

    Le lien RentHub en particulier: le livrer, c'est livrer la source de
    chaque annonce, donc tout le travail d'agregation.
    """
    _add_listings(session, 1)
    listing = client.get("/listings").json()[0]
    for field in _PRECIOUS:
        assert listing[field] is None, field
    # Ce qui reste doit suffire a une vitrine et a une carte: une position,
    # mais floutee (voir test_anonymous_gps_is_fuzzed_deterministically).
    assert listing["latitude"] is not None
    assert listing["longitude"] is not None
    assert listing["location_approx"] is True
    assert listing["province"] == "Bangkok"
    assert listing["district"] == "Watthana"
    assert listing["contract_monthly_min"] == 15000


def test_authenticated_listings_keep_address_and_source_url(client, session):
    _add_listings(session, 1)
    _authenticate(client)
    listing = client.get("/listings").json()[0]
    for field in _PRECIOUS:
        assert listing[field] is not None, field
    assert listing["location_approx"] is False


def test_anonymous_gps_is_fuzzed_deterministically(client, session):
    """Facon Airbnb: le point anonyme est decale de 100 a 300 m, toujours
    du meme cote pour une annonce donnee (sinon on retrouve le vrai point
    en moyennant quelques rechargements)."""
    import math

    from src.api.main import FUZZ_MAX_M, FUZZ_MIN_M

    _add_listings(session, 1)
    anon = client.get("/listings").json()[0]
    again = client.get("/listings").json()[0]
    _authenticate(client)
    real = client.get("/listings").json()[0]

    assert (anon["latitude"], anon["longitude"]) == (again["latitude"], again["longitude"])
    dy = (anon["latitude"] - real["latitude"]) * 111_320
    dx = (anon["longitude"] - real["longitude"]) * 111_320 * math.cos(math.radians(real["latitude"]))
    dist = math.hypot(dx, dy)
    assert FUZZ_MIN_M - 2 <= dist <= FUZZ_MAX_M + 2, dist


def test_anonymous_listing_detail_hides_the_same_fields(client, session):
    """`/listings/{id}` est la porte la plus discrete: un identifiant suffit."""
    _add_listings(session, 1)
    listing_id = client.get("/listings").json()[0]["id"]
    detail = client.get(f"/listings/{listing_id}").json()
    for field in _PRECIOUS:
        assert detail[field] is None, field


# ── /stats ───────────────────────────────────────────────────────────

_BUSINESS_METRICS = (
    "by_province", "updated_today", "price_changed_today",
    "total_removed", "last_scan",
)


def test_anonymous_stats_hide_business_metrics(client, session):
    """Repartition geographique et rythme de mise a jour sont des indicateurs
    de croissance: ils renseignent un concurrent, pas un prospect."""
    _add_listings(session, 3)
    body = client.get("/stats").json()
    for key in _BUSINESS_METRICS:
        assert key not in body, key
    # Les pastilles du hero doivent rester alimentees.
    assert body["total_active"] == 3
    assert body["monthly_contract_count"] == 3
    assert "new_today" in body


def test_authenticated_stats_are_complete(client, session):
    _add_listings(session, 3)
    _authenticate(client)
    body = client.get("/stats").json()
    for key in _BUSINESS_METRICS:
        assert key in body, key
    assert body["by_province"] == [{"province": "Bangkok", "count": 3}]


# ── Debit ────────────────────────────────────────────────────────────

def test_data_endpoints_are_rate_limited(client, session):
    """Une route publique bornee reste aspirable a la vitesse du reseau."""
    _add_listings(session, 1)
    from src.api.rate_limit import ANONYMOUS_MAX_PER_MINUTE

    statuses = [client.get("/listings").status_code for _ in range(ANONYMOUS_MAX_PER_MINUTE + 1)]
    assert statuses[:ANONYMOUS_MAX_PER_MINUTE] == [200] * ANONYMOUS_MAX_PER_MINUTE
    assert statuses[-1] == 429


def test_rate_limited_response_says_when_to_retry(client, session):
    _add_listings(session, 1)
    from src.api.rate_limit import ANONYMOUS_MAX_PER_MINUTE

    for _ in range(ANONYMOUS_MAX_PER_MINUTE):
        client.get("/listings")
    response = client.get("/listings")
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1


def test_health_is_never_rate_limited(client):
    """Le lanceur sonde /health une fois par seconde pendant une minute
    (renthub.ps1): le compter dans le budget ferait echouer le demarrage."""
    from src.api.rate_limit import ANONYMOUS_MAX_PER_MINUTE

    statuses = {client.get("/health").status_code for _ in range(ANONYMOUS_MAX_PER_MINUTE + 10)}
    assert statuses == {200}


def test_authenticated_clients_get_a_larger_budget(client, session):
    """Le frontend connecte pagine tout le catalogue par pages de 500:
    lui appliquer le budget anonyme casserait le chargement des cartes."""
    _add_listings(session, 1)
    _authenticate(client)
    from src.api.rate_limit import ANONYMOUS_MAX_PER_MINUTE

    statuses = {
        client.get("/listings").status_code
        for _ in range(ANONYMOUS_MAX_PER_MINUTE + 5)
    }
    assert statuses == {200}
