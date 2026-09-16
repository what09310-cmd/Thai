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

import src.api.auth as auth_module
from scripts.run_scraper import select_detail_urls
from src.api.auth import SESSION_COOKIE_NAME, check_credentials, create_session_token
from src.api.main import app, get_db
from src.config import settings
from src.database.models import Base, Listing, ListingHistory, ListingImage
from src.models.schemas import ListingFull
from src.normalizers.amenities import derive_amenities
from src.parser.detail_parser import (
    _clean_line_id,
    _extract_contacts,
    _has_listing_payload,
)
from src.tracker.change_detector import mark_removed_listings, upsert_listing

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


# ── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def session():
    """Session SQLAlchemy sur une base SQLite en mémoire (jamais thaimonth.db)."""
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


def _next_data_html(whatsapp: str | None, wa_me_link: bool) -> str:
    listing = {
        "props": {
            "pageProps": {
                "listing": {
                    "contactInformation": [
                        {
                            "phone": [{"phoneNumber": "0812345678", "isMobilePhone": True}],
                            "lineId": "@landlord",
                            "whatsApp": whatsapp,
                        }
                    ]
                }
            }
        }
    }
    link = f'<a href="https://wa.me/{whatsapp}">WhatsApp</a>' if wa_me_link else ""
    return (
        f'<html><body>{link}'
        f'<script id="__NEXT_DATA__">{__import__("json").dumps(listing)}</script>'
        "</body></html>"
    )


def test_whatsapp_field_ignored_without_a_rendered_wa_me_link():
    """`contactInformation[0].whatsApp` du JSON est souvent renseigné par
    RentHub sans qu'aucun bouton WhatsApp ne soit affiché sur la page (sur
    un échantillon d'annonces actives, ~60% des `whatsApp` non vides
    n'avaient aucun lien wa.me réel) — dans ce cas ce n'est pas un vrai
    contact WhatsApp et il ne faut pas l'afficher comme tel."""
    soup_without_link = Selector(_next_data_html("66812345678", wa_me_link=False))
    phone, line_id, whatsapp, email = _extract_contacts(soup_without_link)
    assert whatsapp is None
    assert phone == "0812345678"
    assert line_id == "@landlord"

    soup_with_link = Selector(_next_data_html("66812345678", wa_me_link=True))
    _, _, whatsapp_with_link, _ = _extract_contacts(soup_with_link)
    assert whatsapp_with_link == "66812345678"


def test_derive_amenities_reads_thai_only_descriptions():
    """`derive_amenities` retombe sur la description texte libre quand la
    grille d'icônes est absente (`_extract_amenities_from_icons` renvoie
    None) — mais ses motifs n'étaient qu'en anglais. Une annonce rédigée
    entièrement en thaï (ex: source_id 70911, "ห้อง มีแอร์ ,ทีวี...") se
    retrouvait donc sans aucun équipement détecté, dont "Air Conditioner"
    affiché à tort comme absent dans la description reconstruite."""
    assert "Air Conditioner" in derive_amenities("ห้อง มีแอร์ ,ทีวี, ตู้เย็น")
    assert "Air Conditioner" in derive_amenities("เครื่องปรับอากาศพร้อม")
    assert "Air Conditioner" not in derive_amenities("ห้องนี้ไม่มีแอร์ มีพัดลม")


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
    assert client.get("/static/payant.html").status_code == 200

    # Seuls "/" (index.html) et vip.html sont protégés: les autres pages
    # passent, directement comme via /static.
    assert client.get("/static/premium.html").status_code == 200
    assert client.get("/premium.html").status_code == 200
    gated = client.get("/static/vip.html", follow_redirects=False)
    assert gated.status_code in (302, 307)
    assert gated.headers["location"] == "/login"


def test_login_is_rate_limited_after_repeated_failures(client):
    """Le couple identifiant/mot de passe est unique et partagé: sans
    throttling il est brute-forçable à la vitesse du réseau."""
    auth_module._failed_attempts.clear()

    for _ in range(auth_module._LOGIN_ATTEMPT_MAX):
        resp = client.post("/login", data={"username": "admin", "password": "wrong"})
        assert resp.status_code == 200

    blocked = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert blocked.status_code == 429

    # Même avec les bons identifiants, le verrou reste actif.
    still_blocked = client.post(
        "/login",
        data={"username": settings.site_username, "password": settings.site_password},
    )
    assert still_blocked.status_code == 429

    auth_module._failed_attempts.clear()


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


def test_anonymous_listings_hide_direct_contact_fields(client, session):
    """`/listings` est public (payant.html et la vitrine /test en ont
    besoin sans connexion): les coordonnées de contact directes ne
    doivent donc pas fuiter vers un visiteur non authentifié, contrairement
    à `deposit`/`electric_price` qui sont déjà publiques via `description`
    (voir build_contact_description)."""
    upsert_listing(
        session,
        _listing_full(
            source_id="71261",
            phone="0817322385",
            line_id="secretpurse",
            whatsapp="+66817322385",
            email="owner@example.com",
            deposit="7500 Baht",
            has_structured_data=True,
        ),
        NOW,
    )
    session.commit()

    anon = client.get("/listings").json()[0]
    assert anon["phone"] is None
    assert anon["line_id"] is None
    assert anon["whatsapp"] is None
    assert anon["email"] is None
    assert anon["deposit"] == "7500 Baht"

    single = client.get(f"/listings/{anon['id']}").json()
    assert single["phone"] is None

    client.cookies.set(SESSION_COOKIE_NAME, create_session_token())
    authed = client.get("/listings").json()[0]
    assert authed["phone"] == "0817322385"
    assert authed["line_id"] == "secretpurse"
    assert authed["whatsapp"] == "+66817322385"
    assert authed["email"] == "owner@example.com"


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


# ── Effacement des contrats par une page sans offre court terme ──────
#
# Les pages /en/short-term-rental/<slug> (--include-locations) portent
# `price.monthly` mais pas `shortTerm.*.shortContract`. Sans garde, elles
# écrasaient par None les contrats déjà connus: 766 couples
# (annonce, champ) oscillaient valeur -> None -> valeur dans l'historique
# de renthub.db, et 136 annonces avaient perdu leur contrat 6 mois.

def _with_contracts(**overrides) -> ListingFull:
    data = {
        "from_structured_list": True,
        "has_monthly_contract": "true",
        "contract_monthly_raw": "7,500 THB/month",
        "contract_monthly_min": 7500,
        "contract_monthly_max": 7500,
        "contract_6_month_raw": "4,500 THB/month",
        "contract_6_month_min": 4500,
        "contract_6_month_max": 4500,
    }
    data.update(overrides)
    return _listing_full(**data)


def test_location_page_does_not_erase_known_contracts(session):
    upsert_listing(session, _with_contracts(), NOW)
    session.commit()

    # Même annonce revue via une page par lieu: aucun contrat, juste un prix.
    change_type, db_listing = upsert_listing(
        session,
        _listing_full(
            from_structured_list=False,
            has_monthly_contract="unknown",
            price_monthly_raw="6,500 THB/month",
            price_monthly_min=6500,
        ),
        NOW + timedelta(hours=1),
    )
    session.commit()

    assert db_listing.contract_monthly_min == 7500
    assert db_listing.contract_6_month_raw == "4,500 THB/month"
    assert db_listing.has_monthly_contract == "true"
    # Le prix mensuel, lui, est bien porté par la page par lieu.
    assert db_listing.price_monthly_min == 6500
    assert change_type == "PRICE_CHANGED"

    wiped = [
        h for h in db_listing.history
        if h.change_type == "PRICE_CHANGED" and h.new_value == "None"
    ]
    assert wiped == []


def test_structured_page_can_still_remove_a_contract(session):
    """"Plus de contrat 1 mois" reste une information réelle à enregistrer."""
    upsert_listing(session, _with_contracts(), NOW)
    session.commit()

    _, db_listing = upsert_listing(
        session,
        _listing_full(from_structured_list=True, has_monthly_contract="false"),
        NOW + timedelta(hours=1),
    )
    session.commit()

    assert db_listing.contract_monthly_min is None
    assert db_listing.has_monthly_contract == "false"


# ── UPDATED fantôme au cycle de péremption des pages détail ──────────
#
# `description` et `amenities` ne sont renseignés que quand la page détail
# a été lue dans ce run. Hasher l'objet scrapé faisait basculer le hash à
# chaque passage de la péremption (detail_refresh_days): 840 annonces de
# renthub.db avaient plus d'une entrée UPDATED.

def test_skipping_the_detail_page_does_not_emit_a_phantom_update(session):
    with_detail = _with_contracts(
        source_id="9560",
        amenities=["Air Conditioner", "Parking"],
        deposit="2 months",
        has_structured_data=True,
    )
    upsert_listing(session, with_detail, NOW)
    session.commit()

    # Scan suivant: page détail sautée (détail scrapé il y a moins de
    # detail_refresh_days), donc ni amenities ni contacts dans le ListingFull.
    change_type, db_listing = upsert_listing(
        session, _with_contracts(), NOW + timedelta(days=1)
    )
    session.commit()

    assert change_type == "UNCHANGED"
    assert [h for h in db_listing.history if h.change_type == "UPDATED"] == []
    assert db_listing.deposit == "2 months"


def test_a_real_content_change_is_still_detected(session):
    upsert_listing(session, _with_contracts(), NOW)
    session.commit()

    change_type, db_listing = upsert_listing(
        session, _with_contracts(name="Nouveau nom"), NOW + timedelta(days=1)
    )
    session.commit()

    assert change_type == "UPDATED"
    assert db_listing.name == "Nouveau nom"


# ── is_verified dans le repli HTML ───────────────────────────────────

def test_not_verified_banner_does_not_mark_a_listing_verified():
    """RentHub écrit "This is not verified listing" sur les annonces non vérifiées."""
    from src.parser.list_parser import _is_verified

    assert _is_verified("This is not verified listing") is False
    assert _is_verified("Non-verified listing") is False
    assert _is_verified("Verified listing") is True
    assert _is_verified("Studio 30 sqm") is False


# ── Bornes des paramètres de requête sur les routes publiques ────────
#
# /listings* est public (PUBLIC_PATH_PREFIXES). `limit` n'avait pas de borne
# basse: SQLite traite LIMIT -1 comme "pas de limite", donc ?limit=-1
# rendait le catalogue entier en une requête non authentifiée.

@pytest.mark.parametrize(
    "query",
    ["limit=-1", "limit=0", "offset=-1", "limit=501"],
)
def test_listings_rejects_out_of_range_pagination(client, query):
    assert client.get(f"/listings?{query}").status_code == 422


def test_removed_routes_stay_removed(client):
    """Routes retirees (aucune page ne les appelait): chaque route publique
    est de la surface d'attaque, elles ne doivent pas reapparaitre."""
    for path in (
        "/listings/new", "/listings/updated", "/listings/price-changed",
        "/listings/monthly", "/history/1", "/provinces", "/carte.html",
    ):
        assert client.get(path).status_code in (404, 422), path
    assert client.post("/rental-requests", json={}).status_code in (404, 405)


def test_oversized_session_cookie_is_rejected_without_error(client):
    """int() lève au-delà de 4300 chiffres: la charge doit être bornée avant.

    Seuls "/" et /vip.html passent par le garde d'authentification.
    """
    client.cookies.set(SESSION_COOKIE_NAME, "9" * 5000 + ".deadbeef")
    response = client.get("/vip.html", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/login"


def test_changing_the_password_invalidates_existing_sessions(monkeypatch):
    from src.api.auth import read_session_token

    token = create_session_token()
    assert read_session_token(token) is not None

    monkeypatch.setattr(settings, "site_password", "un-autre-mot-de-passe")
    assert read_session_token(token) is None


def test_security_headers_are_set(client):
    headers = client.get("/health").headers

    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in headers["Content-Security-Policy"]


def test_stats_counts_listings_not_history_rows(session, client):
    """Un seul changement de prix écrit une ligne par champ touché."""
    from src.database.models import ListingHistory

    session.add(
        Listing(
            slug="s", name="S", url="https://www.renthub.in.th/en/s", status="active",
        )
    )
    session.flush()
    for field in ("contract_monthly_min", "contract_monthly_max", "contract_6_month_min"):
        session.add(
            ListingHistory(
                listing_id=1,
                changed_at=datetime.now(timezone.utc),
                change_type="PRICE_CHANGED",
                field_name=field,
            )
        )
    session.commit()

    # `price_changed_today` ne figure que dans la réponse authentifiée:
    # le rythme d'actualisation n'est pas servi à la vitrine
    # (main.py::PublicStatsResponse).
    client.cookies.set(SESSION_COOKIE_NAME, create_session_token())
    assert client.get("/stats").json()["price_changed_today"] == 1


# ── Un scan interrompu doit être marqué "failed" ─────────────────────
#
# renthub.db portait 2 lignes ScanLog bloquées en "running" depuis des
# jours: le statut "failed" du modèle n'était jamais écrit, et /stats ne
# retenant que les scans "completed", l'échec restait invisible.

def test_interrupted_scan_is_marked_failed(session, monkeypatch):
    import scripts.run_scraper as runner
    from src.database.models import ScanLog

    scan_log = ScanLog(started_at=NOW, status="running")
    session.add(scan_log)
    session.commit()

    from contextlib import contextmanager

    @contextmanager
    def fake_session():
        yield session
        session.commit()

    monkeypatch.setattr(runner, "get_session", fake_session)
    runner._mark_scan_failed(scan_log.id, RuntimeError("réseau coupé"))

    refreshed = session.query(ScanLog).filter_by(id=scan_log.id).one()
    assert refreshed.status == "failed"
    assert refreshed.finished_at is not None
    assert "réseau coupé" in refreshed.error_message


def test_marking_a_failed_scan_never_masks_the_original_error(session, monkeypatch):
    """La journalisation de l'échec ne doit pas lever à son tour."""
    import scripts.run_scraper as runner

    def broken_session():
        raise RuntimeError("base injoignable")

    monkeypatch.setattr(runner, "get_session", broken_session)
    runner._mark_scan_failed(1, RuntimeError("erreur d'origine"))


# ── Doublons de pagination dans un même scan ─────────────────────────
#
# La même annonce revient régulièrement sur deux pages de la pagination
# (~19 % des cartes sur un échantillon de deux pages de production).
# Upsertée deux fois dans le même scan avec des données différentes, chaque
# passage annulait le précédent et écrivait sa propre ligne PRICE_CHANGED.

@pytest.mark.asyncio
async def test_pagination_duplicates_are_collected_once():
    from scripts.run_scraper import collect_unique_listings
    from src.models.schemas import ListingRaw

    def card(slug, price):
        return ListingRaw(
            name=slug,
            url=f"https://www.renthub.in.th/en/{slug}",
            slug=slug,
            price_monthly_min=price,
        )

    async def source():
        yield card("a", 5000), "page/1"
        yield card("b", 6000), "page/1"
        # "a" réapparaît en page 2 avec un prix différent.
        yield card("a", 9999), "page/2"
        yield card("c", 7000), "page/2"

    listings, pages, duplicates = await collect_unique_listings(source())

    assert [l.slug for l in listings] == ["a", "b", "c"]
    assert duplicates == 1
    assert pages == 2
    # La première occurrence gagne: le second prix ne doit pas s'imposer.
    assert listings[0].price_monthly_min == 5000


@pytest.mark.asyncio
async def test_page_count_does_not_assume_a_fixed_page_size():
    """L'ancien compteur faisait `len(annonces) % 40` et se décalait."""
    from scripts.run_scraper import collect_unique_listings
    from src.models.schemas import ListingRaw

    async def source():
        for page in range(1, 4):
            for i in range(7):  # 7 annonces par page, pas 40
                slug = f"p{page}-{i}"
                yield ListingRaw(
                    name=slug, url=f"https://x/{slug}", slug=slug
                ), f"page/{page}"

    listings, pages, duplicates = await collect_unique_listings(source())

    assert pages == 3
    assert len(listings) == 21
    assert duplicates == 0


# ── Sécurité: contournement du gate /static ─────────────────────────

@pytest.mark.parametrize(
    "path",
    [
        "/static/index.html/",     # normpath retire le slash, le test d'extension non
        "/static/vip.html/",
        "/static/index.HTML/",     # la casse ne doit pas non plus ouvrir la porte
    ],
)
def test_static_gate_survives_path_normalisation(client, path):
    """Une page protégée ne doit pas devenir publique par un suffixe.

    `_is_public` testait l'extension sur le chemin brut, alors que
    StaticFiles applique `os.path.normpath` ensuite: "index.html/" ne
    finit pas par ".html", passait donc pour un asset public, et normpath
    servait ensuite index.html. Un seul "/" ajouté suffisait à contourner
    le login sur *toutes* les pages protégées.
    """
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code in (302, 307), f"{path} a été servi sans session"
    assert resp.headers["location"] == "/login"


def test_everything_outside_protected_paths_is_public(client):
    """La regle est une liste de pages protegees, pas une liste publique:
    assets, pages secondaires et routes API sont servis sans session."""
    assert client.get("/static/anything.js", follow_redirects=False).status_code == 404
    assert client.get("/carte-bangkok.html", follow_redirects=False).status_code == 200
    assert client.get("/stats", follow_redirects=False).status_code == 200


def test_static_assets_and_public_pages_still_work(client):
    """Le durcissement ne doit pas casser ce qui était légitimement public."""
    assert client.get("/static/logo.png").status_code == 200
    assert client.get("/static/payant.html").status_code == 200
    assert client.get("/static/district_coords.json").status_code == 200


# ── Sécurité: bornes des paramètres publics ─────────────────────────

def test_listings_offset_is_bounded(client):
    """`/listings` est public: sans borne haute, offset permet d'itérer
    tout le catalogue page par page."""
    assert client.get("/listings?offset=999999999").status_code == 422


def test_like_wildcards_do_not_leak_into_the_filter(client, session):
    """`%` envoyé par un visiteur ne doit pas devenir un joker SQL.

    ilike(f"%{province}%") sans échappement transforme ?province=% en
    "tout le catalogue", et un motif long en balayage complet sur une
    route publique."""
    session.add(
        Listing(slug="p1", name="P1", url="https://x/1", province="Bangkok", status="active")
    )
    session.commit()

    assert client.get("/listings?province=%25").json() == []
    assert len(client.get("/listings?province=Bangkok").json()) == 1


def test_logout_clears_the_session(client):
    """Sans /logout, la seule révocation possible était la rotation du
    mot de passe du site.

    Seuls "/" et /vip.html passent par le garde d'authentification.
    """
    client.cookies.set(SESSION_COOKIE_NAME, create_session_token())
    assert client.get("/vip.html", follow_redirects=False).status_code == 200

    out = client.post("/logout", follow_redirects=False)
    assert out.status_code == 303
    assert out.headers["location"] == "/login"

    # C'est l'en-tete emis qui compte: le pot a cookies du client de test
    # garde le cookie pose a la main (domaine vide) a cote de celui que la
    # reponse supprime (domaine testserver), donc l'inspecter ne prouverait
    # rien sur le comportement du serveur.
    set_cookie = out.headers["set-cookie"]
    assert set_cookie.startswith(f'{SESSION_COOKIE_NAME}=""')
    assert "Max-Age=0" in set_cookie or "01 Jan 1970" in set_cookie

    # Et sans cookie, la page protegee redirige bien vers le login.
    client.cookies.clear()
    assert client.get("/vip.html", follow_redirects=False).status_code in (302, 307)


# ── Detection de changements: pistes d'audit ────────────────────────

def _history_kinds(session, listing_id: int) -> set[str]:
    return {
        h.change_type
        for h in session.query(ListingHistory).filter(
            ListingHistory.listing_id == listing_id
        )
    }


def test_a_price_change_does_not_hide_a_content_change(session):
    """Prix ET contenu modifies au meme scan: les deux doivent etre traces.

    La ligne UPDATED etait conditionnee a `not price_changed`, alors que
    `content_hash` etait ecrase dans tous les cas. Le changement de contenu
    disparaissait donc definitivement: au scan suivant le hash stocke est
    deja le nouveau, plus rien ne le signale.
    """
    _, listing = upsert_listing(
        session, _listing_full(name="Studio A", price_monthly_min=9000), NOW
    )
    session.commit()
    listing_id = listing.id

    later = NOW + timedelta(days=1)
    upsert_listing(
        session,
        _listing_full(name="Studio A entierement renove", price_monthly_min=12000),
        later,
    )
    session.commit()

    kinds = _history_kinds(session, listing_id)
    assert "PRICE_CHANGED" in kinds
    assert "UPDATED" in kinds, "le changement de contenu a ete perdu"


def test_an_unchanged_scan_still_writes_no_history(session):
    """Garde-fou de l'invariant central: en levant la condition
    `not price_changed`, on ne doit pas se mettre a ecrire des UPDATED
    fantomes quand rien ne bouge."""
    _, listing = upsert_listing(session, _listing_full(price_monthly_min=9000), NOW)
    session.commit()
    listing_id = listing.id
    before = session.query(ListingHistory).filter(
        ListingHistory.listing_id == listing_id
    ).count()

    upsert_listing(session, _listing_full(price_monthly_min=9000), NOW + timedelta(days=1))
    session.commit()

    after = session.query(ListingHistory).filter(
        ListingHistory.listing_id == listing_id
    ).count()
    assert after == before


def test_amenities_extraction_order_is_deterministic():
    """L'ordre des equipements entre dans le content_hash via json.dumps.

    Les deux sources sont ordonnees (ordre du DOM pour la grille d'icones,
    ordre d'insertion du dict pour la derivation textuelle). Ce test verrouille
    cette propriete: la rendre dependante d'un `set` ferait basculer le hash
    d'un scan a l'autre sans qu'aucune donnee n'ait change, exactement le
    genre d'UPDATED fantome que les invariants de scan combattent.
    """
    text = "Air conditioner, wifi, swimming pool, fitness, parking available"
    runs = [derive_amenities(text) for _ in range(5)]
    assert all(r == runs[0] for r in runs)
    assert runs[0], "l'echantillon doit produire des equipements"


# ── Fusion raw/detail ───────────────────────────────────────────────

def test_raw_and_detail_schemas_stay_disjoint():
    """`_merge_listing` fait `{**raw, **detail}`: le detail ECRASE le raw.

    C'est sans consequence tant que les deux modeles n'ont aucun champ en
    commun. Le jour ou l'un d'eux gagne un champ deja porte par l'autre --
    `from_structured_list` en particulier, qui decide si une source a le
    droit d'effacer les contrats -- une valeur par defaut du detail ecraserait
    silencieusement la valeur scrapee, et l'invariant de scan tomberait sans
    qu'aucun test ne bronche.
    """
    from src.models.schemas import ListingDetail, ListingRaw

    common = set(ListingRaw.model_fields) & set(ListingDetail.model_fields)
    assert common == set(), (
        f"Champs communs a ListingRaw et ListingDetail: {sorted(common)}. "
        "Revoir _merge_listing (scripts/run_scraper.py) avant d'ajouter ce champ."
    )


def test_excluded_image_never_becomes_the_thumbnail(client, session):
    """Une image ecartee a la relecture ne doit jamais s'afficher.

    _thumbnail_map ne filtrait `excluded` que dans la sous-requete calculant
    la plus petite position. La jointure externe, elle, ne portait que sur
    (listing_id, position): comme des positions dupliquees existent en base,
    l'image exclue partageant cette position revenait dans le resultat et
    pouvait l'emporter dans le dict final.
    """
    _, listing = upsert_listing(session, _listing_full(), NOW)
    session.flush()

    # Deux images en position 0 (doublon reel en base), la premiere ecartee.
    session.add_all([
        ListingImage(
            listing_id=listing.id, image_url="https://cdn/exclue.jpg",
            position=0, excluded=True,
        ),
        ListingImage(
            listing_id=listing.id, image_url="https://cdn/bonne.jpg",
            position=0, excluded=False,
        ),
    ])
    session.commit()

    payload = client.get("/listings").json()
    assert payload[0]["images"] == ["https://cdn/bonne.jpg"]
