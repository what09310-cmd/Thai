"""
Tests de non-régression pour la passe de correction de bugs.

Un test par défaut corrigé, dans l'ordre des couches: parseur, API,
détection de changements.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from scrapling.parser import Selector
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.auth import check_credentials
from src.api.main import app, get_db
from src.config import settings
from src.database.models import Base, Listing
from src.models.schemas import ListingFull
from src.parser.detail_parser import (
    _clean_line_id,
    _extract_contract_row_values,
    _has_listing_payload,
)
from src.tracker.change_detector import mark_removed_listings, upsert_listing
from scripts.run_scraper import select_detail_urls

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


# ── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def session():
    """Session SQLAlchemy sur une base SQLite en mémoire (jamais renthub.db)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture
def client(session):
    """TestClient dont get_db pointe sur la base en mémoire.

    Sans gestionnaire de contexte: le hook de démarrage (init_db + ALTER
    TABLE) ne doit pas s'exécuter sur la vraie base.
    """
    app.dependency_overrides[get_db] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _listing_full(**overrides) -> ListingFull:
    data = {
        "name": "Test Listing",
        "url": "https://www.renthub.in.th/en/test-listing",
        "slug": "test-listing",
        "address": "Sukhumvit Watthana Bangkok",
    }
    data.update(overrides)
    return ListingFull(**data)


def _add_listings(session, count: int, updated_at: datetime | None = None) -> None:
    for i in range(count):
        session.add(
            Listing(
                slug=f"listing-{i}",
                name=f"Listing {i}",
                url=f"https://www.renthub.in.th/en/listing-{i}",
                status="active",
                source_updated_at=updated_at,
            )
        )
    session.commit()


# ── Parseur ─────────────────────────────────────────────────────────

def test_contract_row_ignores_month_label():
    """Le numéro de mois du libellé ne doit pas être lu comme un prix.

    `parse_price_range("Contract 3 month 8,000 THB/Month")` capture le 3
    comme premier nombre: le libellé doit être retiré avant l'extraction.
    Ce test couvre aussi la régression Scrapling qui rendait la fonction
    muette (`.text` d'un <tr> ne rend que son texte direct, soit "").
    """
    html = (
        "<table>"
        "<tr><td>Contract 1 month</td><td>Contract 3 month</td><td>Contract 6 month</td></tr>"
        "<tr><td>Contract 1 month</td><td>8,500 THB/Month</td></tr>"
        "<tr><td>Contract 3 month</td><td>8,000 THB/Month</td></tr>"
        "<tr><td>Contract 6 month</td><td>7,500 THB/Month</td></tr>"
        "</table>"
    )
    header_row = Selector(html).css("tr")[0]

    assert _extract_contract_row_values(header_row) == {
        "1": 8500,
        "3": 8000,
        "6": 7500,
    }


def test_line_id_placeholder_leaves_the_field_empty():
    """RentHub affiche "Unavailable" à la place du LINE ID quand l'annonce
    n'en a pas: ce n'est pas un contact, la case doit rester vide plutôt
    que d'afficher le placeholder. Le lien line.me, lui, porte l'id
    percent-encodé — il est décodé, pas stocké tel quel."""
    assert _clean_line_id("Unavailable") is None
    assert _clean_line_id("-") is None
    assert _clean_line_id("   ") is None
    assert _clean_line_id(None) is None

    assert _clean_line_id("%40zimple_asset") == "@zimple_asset"
    assert _clean_line_id("@bsquare61") == "@bsquare61"


def test_delisted_page_is_not_an_authoritative_source():
    """Une annonce retirée de RentHub ne renvoie ni 404 ni page vide: le
    site sert la page d'accueil. Son __NEXT_DATA__ est valide mais sans
    clé `listing` — le prendre pour la source ferait effacer contact et
    charges déjà en base (apply_detail_fields écrase alors même par None).
    """
    listing_page = {"props": {"pageProps": {"listing": {"id": "401"}}}}
    home_page = {"props": {"pageProps": {"listings": [], "blogs": []}}}

    assert _has_listing_payload(listing_page) is True
    assert _has_listing_payload(home_page) is False
    assert _has_listing_payload({"props": {"pageProps": {"listing": None}}}) is False
    assert _has_listing_payload(None) is False


def test_line_id_rejects_phone_numbers():
    """Un numéro de téléphone n'est pas un LINE ID: la case doit rester
    vide. Un pseudo reste accepté avec ou sans arobase — l'arobase ne
    concerne que les comptes officiels, les comptes personnels n'en ont
    pas ("secretpurse")."""
    assert _clean_line_id("0613963159") is None
    assert _clean_line_id("081-234-5678") is None
    assert _clean_line_id("~0620477711") is None
    # Libellé recopié dans le champ par le bailleur.
    assert _clean_line_id("Line ID : 0890215335") is None
    assert _clean_line_id("Phone0971538717") is None

    assert _clean_line_id("secretpurse") == "secretpurse"
    assert _clean_line_id("jazzandlek2327") == "jazzandlek2327"
    assert _clean_line_id("25apartment") == "25apartment"
    assert _clean_line_id("@serenade") == "@serenade"
    assert _clean_line_id("Line:@zimple_asset") == "@zimple_asset"


# ── Authentification ────────────────────────────────────────────────

def test_login_rejects_non_ascii_password():
    """hmac.compare_digest sur des str lève TypeError dès un caractère
    non-ASCII: le mot de passe accentué doit être refusé, pas planter."""
    assert check_credentials(settings.site_username, "mot-de-passé") is False


def test_static_html_requires_session(client):
    """Le montage /static ne doit pas servir de porte dérobée."""
    # Page protégée atteinte via /static: redirection vers le login.
    protected = client.get("/static/index.html", follow_redirects=False)
    assert protected.status_code in (302, 307)
    assert protected.headers["location"] == "/login"

    # Les assets restent publics (le frontend en a besoin sur les pages
    # publiques), tout comme les pages explicitement publiques.
    assert client.get("/static/logo.png").status_code == 200
    assert client.get("/static/premium.html").status_code == 200


# ── API ─────────────────────────────────────────────────────────────

def test_listings_pagination_is_stable(client, session):
    """À source_updated_at identique, deux pages ne doivent ni se
    recouvrir ni perdre de lignes (le frontend pagine toute la base)."""
    _add_listings(session, 6, updated_at=NOW)

    first = client.get("/listings?limit=3&offset=0").json()
    second = client.get("/listings?limit=3&offset=3").json()

    ids_first = [l["id"] for l in first]
    ids_second = [l["id"] for l in second]

    assert len(ids_first) == 3 and len(ids_second) == 3
    assert not set(ids_first) & set(ids_second)
    assert len(set(ids_first) | set(ids_second)) == 6


def test_listing_response_carries_thumbnail(client, session):
    """La vignette doit survivre au passage de selectinload à _thumbnail_map."""
    upsert_listing(
        session,
        _listing_full(images=["https://cdn/a.jpg", "https://cdn/b.jpg"]),
        NOW,
    )
    session.commit()

    payload = client.get("/listings").json()

    assert len(payload) == 1
    assert payload[0]["images"] == ["https://cdn/a.jpg"]


# ── Détection de changements ────────────────────────────────────────

def test_image_positions_stay_unique(session):
    """Les positions d'images ne doivent pas se dupliquer d'un scan à
    l'autre: c'est leur ordre qui décide de la vignette affichée."""
    upsert_listing(session, _listing_full(images=["a.jpg", "b.jpg", "c.jpg"]), NOW)
    session.commit()

    upsert_listing(
        session,
        _listing_full(images=["a.jpg", "b.jpg", "c.jpg", "d.jpg", "e.jpg"]),
        NOW,
    )
    session.commit()

    listing = session.query(Listing).filter_by(slug="test-listing").one()
    positions = sorted(img.position for img in listing.images)

    assert len(listing.images) == 5
    assert positions == [0, 1, 2, 3, 4]


def test_structured_detail_clears_stale_contact_and_fees(session):
    """Régression: le dépôt "This is not verified listing..." et le
    line_id "Unavailable" hérités de l'ancien parseur regex ne pouvaient
    plus être effacés — les champs détail n'étaient écrasés que par une
    valeur non vide. Un scrape dont le JSON __NEXT_DATA__ a été lu fait
    autorité, y compris quand il ne renvoie rien pour ces champs."""
    upsert_listing(
        session,
        _listing_full(
            source_id="71261",
            deposit=(
                "This is not verified listing. Please be careful when asked "
                "to transfer cash deposit without visiting the place."
            ),
            line_id="Unavailable",
        ),
        NOW,
    )
    session.commit()

    upsert_listing(
        session,
        _listing_full(
            source_id="71261",
            deposit=None,
            line_id=None,
            phone="0817322385",
            has_structured_data=True,
        ),
        NOW,
    )
    session.commit()

    listing = session.query(Listing).filter_by(slug="test-listing").one()
    assert listing.deposit is None
    assert listing.line_id is None
    assert listing.phone == "0817322385"
    assert "not verified" not in listing.description


def test_detail_fields_survive_a_list_only_scan(session):
    """Un scan qui ne lit que la page de liste (aucune donnée détail) ne
    doit pas effacer le contact et les charges déjà en base."""
    upsert_listing(
        session,
        _listing_full(
            source_id="71261",
            deposit="7500 Baht",
            phone="0817322385",
            has_structured_data=True,
        ),
        NOW,
    )
    session.commit()

    upsert_listing(session, _listing_full(), NOW)
    session.commit()

    listing = session.query(Listing).filter_by(slug="test-listing").one()
    assert listing.deposit == "7500 Baht"
    assert listing.phone == "0817322385"


def test_detail_scraped_at_only_moves_on_a_detail_scrape(session):
    """`last_scraped_at` avance à chaque scan; `detail_scraped_at` ne doit
    avancer que quand la page individuelle a réellement été lue — c'est
    lui qui décide du re-scrape détail (select_detail_urls)."""
    upsert_listing(session, _listing_full(source_id="71261"), NOW)
    session.commit()

    listing = session.query(Listing).filter_by(slug="test-listing").one()
    assert listing.detail_scraped_at is not None
    after_detail = listing.detail_scraped_at

    later = NOW + timedelta(days=1)
    upsert_listing(session, _listing_full(), later)
    session.commit()
    session.refresh(listing)

    assert listing.detail_scraped_at == after_detail
    assert listing.last_scraped_at != after_detail


def test_select_detail_urls_refreshes_stale_details():
    """Une annonce déjà connue doit repasser par sa page détail une fois
    le scrape détail périmé. Sans cela, contact/dépôt/charges restaient
    figés sur la valeur du tout premier scan (y compris celle produite
    par une version buggée du parseur)."""
    listings = [
        _listing_full(slug="fresh", url="https://www.renthub.in.th/en/fresh"),
        _listing_full(slug="stale", url="https://www.renthub.in.th/en/stale"),
        _listing_full(slug="never", url="https://www.renthub.in.th/en/never"),
    ]
    last_detail_scrape = {
        "fresh": NOW - timedelta(days=1),
        # naïf: c'est ce que rend SQLite pour un DateTime(timezone=True)
        "stale": (NOW - timedelta(days=30)).replace(tzinfo=None),
        "never": None,
    }

    urls = select_detail_urls(listings, last_detail_scrape, NOW, refresh_days=7)

    assert urls == [
        "https://www.renthub.in.th/en/stale",
        "https://www.renthub.in.th/en/never",
    ]


def test_address_kept_when_scan_returns_none(session):
    """Un scan qui ne remonte pas d'adresse ne doit pas effacer celle
    déjà en base (comme pour district/province/latitude/longitude)."""
    upsert_listing(session, _listing_full(address="Sukhumvit Watthana Bangkok"), NOW)
    session.commit()

    upsert_listing(session, _listing_full(address=None), NOW)
    session.commit()

    listing = session.query(Listing).filter_by(slug="test-listing").one()
    assert listing.address == "Sukhumvit Watthana Bangkok"


def test_partial_scan_does_not_mark_removed(session):
    """Un scan qui n'a vu qu'une fraction du catalogue ne doit rien
    marquer: sinon un run tronqué finit par supprimer toute la base."""
    _add_listings(session, 10)

    removed = mark_removed_listings(session, {"listing-0", "listing-1"}, NOW)

    assert removed == 0
    assert all(l.missing_scan_count == 0 for l in session.query(Listing).all())


def test_full_scan_still_marks_removed(session, monkeypatch):
    """Le filet de sécurité ne doit pas désactiver la détection quand le
    scan a bien couvert le catalogue."""
    monkeypatch.setattr(settings, "removed_after_missing_scans", 1)
    _add_listings(session, 10)
    seen = {f"listing-{i}" for i in range(9)}  # 90% du catalogue

    removed = mark_removed_listings(session, seen, NOW)

    assert removed == 1
    missing = session.query(Listing).filter_by(slug="listing-9").one()
    assert missing.status == "removed"
