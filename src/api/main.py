"""
API REST FastAPI pour le tracker RentHub.

Endpoints:
  GET /listings
  GET /listings/{id}
  GET /listings/new
  GET /listings/updated
  GET /listings/price-changed
  GET /listings/monthly
  GET /provinces
  GET /stats
  GET /history/{listing_id}
"""
from __future__ import annotations

import json
import logging
import posixpath
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Depends, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session as SASession
from sqlalchemy import func, desc, distinct, inspect, text as sa_text
from starlette.middleware.base import BaseHTTPMiddleware

from src.database.session import SessionLocal, init_db, engine
from src.database.models import (
    Listing, ListingHistory, ListingImage, Province, ScanLog, RentalRequest,
)
from src.api import rate_limit
from src.api.auth import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    check_credentials,
    create_session_token,
    is_login_rate_limited,
    register_failed_login,
    register_successful_login,
    verify_session_token,
)
from src.config import DEFAULT_SECRET_KEY, DEFAULT_SITE_PASSWORD, settings

log = logging.getLogger(__name__)

app = FastAPI(
    title="RentHub Tracker API",
    description="API de suivi des annonces RentHub (Short-term Monthly, Thaïlande)",
    version="1.0.0",
)

# Plafond des fenetres "depuis N heures" (un an). Sans borne, ?since_hours
# etait un moyen simple de demander tout le catalogue sur une route publique.
_MAX_SINCE_HOURS = 24 * 366

# Meme raison que _MAX_SINCE_HOURS: sans plafond, `offset` est le moyen le
# plus simple d'aspirer le catalogue depuis une route publique.
_MAX_OFFSET = 100_000

# Taille de l'echantillon servi a un visiteur non authentifie. La vitrine
# (/test, /payant.html) doit convaincre, pas livrer le catalogue: borner
# `limit` ET ignorer `offset` (voir _public_offset) sont indissociables,
# un plafond seul se contourne en bouclant sur les pages suivantes.
PUBLIC_DEMO_LIMIT = 12

# Le frontend est servi par cette meme application: aucune page tierce n'a
# besoin d'appeler l'API. allow_origins=["*"] n'ouvrait donc rien d'utile,
# seulement la consommation de /listings depuis n'importe quel site.
_ALLOWED_ORIGINS = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]

if _ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ALLOWED_ORIGINS,
        # allow_origins="*" et allow_credentials=True sont incompatibles: le
        # navigateur refuse la reponse. Le cookie de session voyage en
        # same-origin, il n'a pas besoin du CORS.
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )


# Origines relevees dans frontend/: Leaflet et son plugin markercluster
# (unpkg), les polices Google, l'iframe Google Maps des pages de detail, et
# les images d'annonces (bcdn.renthub.in.th) + les tuiles OpenStreetMap.
#
# `script-src` porte 'unsafe-inline': chaque page embarque ~2000 lignes de JS
# en ligne. Cette CSP ne contient donc PAS le XSS par script inline; ce
# qu'elle apporte est ailleurs -- `connect-src 'self'` bloque l'exfiltration
# vers un serveur tiers, `object-src 'none'` et `base-uri 'self'` ferment
# deux vecteurs classiques, `frame-ancestors` empeche le clickjacking.
# Passer aux nonces suppose d'extraire le JS des pages: voir la
# deduplication du frontend, hors perimetre de cette passe.
_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://unpkg.com",
    "style-src 'self' 'unsafe-inline' https://unpkg.com https://fonts.googleapis.com",
    "font-src 'self' data: https://fonts.gstatic.com",
    "img-src 'self' data: https:",
    "frame-src https://www.google.com https://maps.google.com",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "frame-ancestors 'self'",
])


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """En-tetes de securite sur toute reponse.

    `frame-ancestors 'self'` (et non frame-src): les pages de detail
    *integrent* des iframes Google Maps, qu'il ne faut surtout pas bloquer.
    Ce qu'on interdit, c'est que l'application soit elle-meme encadree par
    un site tiers (clickjacking sur les pages publiques).

    Ce middleware est enregistre *apres* AuthMiddleware, donc en position
    la plus externe: sans cela la RedirectResponse du gate d'auth sortait
    sans aucun de ces en-tetes, puisqu'elle ne traverse pas call_next.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Content-Security-Policy", _CSP)
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response


PUBLIC_PATHS = {
    "/login",
    "/logout",
    "/health",
    "/payant.html",
    "/stats",
    "/test",
}
PUBLIC_PATH_PREFIXES = (
    "/listings",
)

_STATIC_PREFIX = "/static/"

# Seuls ces types de fichiers sont servis sans session sous /static.
# Liste blanche et non liste noire: l'ancienne regle ne gardait que .html
# et .htm, donc tout autre fichier depose dans frontend/ (un .js, un .json
# de configuration, un .bak laisse par un editeur) devenait lisible par
# n'importe qui sans le moindre changement de code.
_PUBLIC_ASSET_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".json",
)


# Routes servant des donnees, par opposition aux pages et aux assets. Ce
# sont elles qu'on plafonne en debit: /health est sonde une fois par
# seconde au demarrage par le lanceur (renthub.ps1) et /login tient deja
# son propre compteur (src/api/auth.py).
_RATE_LIMITED_PREFIXES = (
    "/listings",
    "/stats",
    "/provinces",
    "/history",
    "/rental-requests",
)


def _matches_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    """Prefixe compare sur des segments entiers.

    `path.startswith("/listings")` faisait aussi passer /listings-admin ou
    /listingsanything: la premiere route ainsi nommee serait nee sans
    authentification, sans que personne ne s'en apercoive.
    """
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def _matches_public_prefix(path: str) -> bool:
    return _matches_prefix(path, PUBLIC_PATH_PREFIXES)


def _is_public(path: str) -> bool:
    """Determine si un chemin est accessible sans cookie de session.

    Le montage /static sert tout le repertoire `frontend/`: on n'y laisse
    passer librement que les assets (logo, JSON de coordonnees). Une page
    .html atteinte par ce biais suit la meme regle que sa route directe,
    sinon /static/index.html contourne purement et simplement le login.

    Le chemin est normalise AVANT d'etre juge, parce que StaticFiles lui
    applique `os.path.normpath` ensuite. Les deux etapes n'etaient pas
    d'accord: "/static/index.html/" ne finit pas par ".html", passait donc
    pour un asset public, puis normpath retirait le slash final et servait
    index.html. Un seul caractere ajoute suffisait a contourner le login
    sur toutes les pages protegees (variantes NTFS "index.html." et
    "index.html%20" incluses, les points et espaces finaux etant ignores
    a l'ouverture du fichier sous Windows).
    """
    if path.startswith(_STATIC_PREFIX):
        name = posixpath.normpath(path[len(_STATIC_PREFIX):]).rstrip(". ")
        if not name or name.startswith(("/", "../")) or name in ("..", "."):
            return False
        if f"/{name}" in PUBLIC_PATHS:
            return True
        return name.lower().endswith(_PUBLIC_ASSET_SUFFIXES)
    return path in PUBLIC_PATHS or _matches_public_prefix(path)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _is_public(path):
            return await call_next(request)
        if not verify_session_token(request.cookies.get(SESSION_COOKIE_NAME)):
            return RedirectResponse(url="/login")
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Plafond de requetes par IP sur les routes de donnees.

    Les bornes posees sur `limit`, `offset` et `since_hours` decident ce
    qu'une requete rend; elles ne disent rien du nombre de requetes. Sur
    une route publique, l'echantillon de la vitrine se reconstitue donc en
    bouclant -- et la page de login, elle, se force au meme rythme. Ce
    middleware ferme la boucle.

    Enregistre entre AuthMiddleware et SecurityHeadersMiddleware, donc
    execute apres ce dernier: le 429 sort avec les en-tetes de securite.
    """

    async def dispatch(self, request: Request, call_next):
        if not _matches_prefix(request.url.path, _RATE_LIMITED_PREFIXES):
            return await call_next(request)
        max_per_minute = (
            rate_limit.AUTHENTICATED_MAX_PER_MINUTE
            if _is_authenticated(request)
            else rate_limit.ANONYMOUS_MAX_PER_MINUTE
        )
        retry_after = rate_limit.register_hit(_client_key(request), max_per_minute)
        if retry_after is not None:
            return JSONResponse(
                {"detail": "Trop de requetes, reessayez dans un instant."},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)


app.add_middleware(AuthMiddleware)
app.add_middleware(RateLimitMiddleware)
# Ajoute en dernier => middleware le plus externe: ses en-tetes couvrent
# aussi les redirections emises par AuthMiddleware.
app.add_middleware(SecurityHeadersMiddleware)


_LOGIN_PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Connexion</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0;
         display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
  form {{ background: #1e293b; padding: 2rem; border-radius: 8px; width: 280px; }}
  input {{ width: 100%; padding: 0.6rem; margin-top: 0.5rem; margin-bottom: 1rem;
           border-radius: 4px; border: 1px solid #334155; background: #0f172a; color: #e2e8f0; box-sizing: border-box; }}
  button {{ width: 100%; padding: 0.6rem; border: none; border-radius: 4px;
            background: #3b82f6; color: white; cursor: pointer; font-weight: 600; }}
  .error {{ color: #f87171; margin-bottom: 1rem; }}
</style>
</head>
<body>
  <form method="post" action="/login">
    <h2>Connexion</h2>
    {error}
    <label for="username">Identifiant</label>
    <input type="text" id="username" name="username" autofocus required autocomplete="username">
    <label for="password">Mot de passe</label>
    <input type="password" id="password" name="password" required autocomplete="current-password">
    <button type="submit">Entrer</button>
  </form>
</body>
</html>"""


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _is_authenticated(request: Request) -> bool:
    return verify_session_token(request.cookies.get(SESSION_COOKIE_NAME))


@app.get("/login", response_class=HTMLResponse)
def login_form():
    return _LOGIN_PAGE.format(error="")


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    client_key = _client_key(request)
    if is_login_rate_limited(client_key):
        return HTMLResponse(
            _LOGIN_PAGE.format(
                error='<div class="error">Trop de tentatives, réessayez dans quelques minutes</div>'
            ),
            status_code=429,
        )
    if not check_credentials(username, password):
        register_failed_login(client_key)
        return HTMLResponse(_LOGIN_PAGE.format(error='<div class="error">Identifiant ou mot de passe incorrect</div>'))
    register_successful_login(client_key)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        create_session_token(),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )
    return response


@app.post("/logout")
def logout():
    """Efface le cookie de session.

    Le jeton ne porte ni identifiant de session ni nonce (src/api/auth.py):
    il n'existe donc pas de revocation cote serveur, et la seule facon
    d'invalider *tous* les jetons en circulation reste la rotation de
    SITE_PASSWORD, qui entre dans la cle de signature. Effacer le cookie
    couvre le cas courant: rendre la main sur un poste partage.
    """
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )
    return response


def _assert_secrets_configured() -> None:
    """Refuse de servir avec les secrets publiés dans le dépôt.

    `secret_key` signe le cookie de session (src/api/auth.py). Restée à la
    valeur du dépôt, elle permet à quiconque lit le code de forger un cookie
    valide et de contourner entièrement le login — le mot de passe n'y change
    rien. Le contrôle est ici, et non dans src/config.py, pour ne pas faire
    échouer l'import de `settings` chez pytest et dans les 21 scripts, qui ne
    servent aucun cookie.
    """
    faulty = []
    if settings.secret_key == DEFAULT_SECRET_KEY:
        faulty.append("SECRET_KEY")
    if settings.site_password == DEFAULT_SITE_PASSWORD:
        faulty.append("SITE_PASSWORD")
    if not faulty:
        return
    raise RuntimeError(
        "Refus de démarrer: "
        + " et ".join(faulty)
        + (" ont" if len(faulty) > 1 else " a")
        + " encore la valeur publiée dans .env.example. "
        "Générez une clé avec: python -c \"import secrets; print(secrets.token_urlsafe(48))\" "
        "puis renseignez-la dans .env."
    )


def _migrate_rental_requests_city() -> None:
    inspector = inspect(engine)
    if "rental_requests" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("rental_requests")}
    if "city" not in cols:
        with engine.begin() as conn:
            conn.execute(sa_text("ALTER TABLE rental_requests ADD COLUMN city VARCHAR(50)"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # @app.on_event est déprécié depuis FastAPI 0.93.
    _assert_secrets_configured()
    init_db()
    _migrate_rental_requests_city()
    yield


app.router.lifespan_context = lifespan


# — Dependency —

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# — Response models —

class ListingResponse(BaseModel):
    id: int
    source_id: Optional[str]
    name: str
    url: str
    address: Optional[str]
    subdistrict: Optional[str]
    district: Optional[str]
    province: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    price_monthly_raw: Optional[str]
    price_monthly_min: Optional[int]
    price_monthly_max: Optional[int]
    daily_price_raw: Optional[str]
    daily_price_min: Optional[int]
    daily_price_max: Optional[int]
    contract_monthly_raw: Optional[str]
    contract_monthly_min: Optional[int]
    contract_monthly_max: Optional[int]
    contract_3_month_raw: Optional[str]
    contract_3_month_min: Optional[int]
    contract_3_month_max: Optional[int]
    contract_6_month_raw: Optional[str]
    contract_6_month_min: Optional[int]
    contract_6_month_max: Optional[int]
    has_monthly_contract: str
    description: Optional[str]
    amenities: list
    room_types: list
    is_verified: bool
    has_promotion: bool
    images: list[str]
    status: str
    source_updated_at: Optional[datetime]
    first_seen_at: Optional[datetime]
    last_seen_at: Optional[datetime]
    last_scraped_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)


class HistoryResponse(BaseModel):
    id: int
    changed_at: datetime
    change_type: str
    field_name: Optional[str]
    old_value: Optional[str]
    new_value: Optional[str]

    model_config = ConfigDict(from_attributes=True)


class ProvinceResponse(BaseModel):
    id: int
    name: str
    slug: str
    renthub_url: Optional[str]
    listing_count: Optional[int]
    active: bool
    last_scan: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)


class RentalRequestCreate(BaseModel):
    # Bornes alignees sur les colonnes (models.py: String(50)). Sans elles,
    # SQLite stocke silencieusement une valeur de plusieurs Mo tandis que
    # Postgres leve une DataError non rattrapee, donc un 500.
    city: str = Field(min_length=1, max_length=50)
    duration: str = Field(min_length=1, max_length=50)
    budget: str = Field(min_length=1, max_length=50)
    conditions: Optional[str] = Field(None, max_length=2000)


class RentalRequestResponse(BaseModel):
    id: int
    city: str
    duration: str
    budget: str
    conditions: Optional[str]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class StatsResponse(BaseModel):
    total_active: int
    total_removed: int
    new_today: int
    updated_today: int
    price_changed_today: int
    monthly_contract_count: int
    three_month_contract_count: int
    six_month_contract_count: int
    by_province: list[dict]
    last_scan: Optional[datetime]


class PublicStatsResponse(BaseModel):
    """Ce que /stats rend a un visiteur non authentifie.

    Les seuls compteurs affiches par le hero de la vitrine. Les absents
    ne decrivent pas le catalogue mais l'activite: `by_province` donne la
    couverture geographique, `updated_today`/`price_changed_today` le
    rythme d'actualisation, `last_scan` la frequence de collecte,
    `total_removed` la rotation du parc. Un prospect n'en fait rien; un
    concurrent, si.
    """

    total_active: int
    new_today: int
    monthly_contract_count: int
    three_month_contract_count: int
    six_month_contract_count: int


# — Helpers —

# Coordonnées de contact directes: visibles seulement des visiteurs
# authentifiés. `deposit`/`electric_price` restent publics (repris dans
# `description` via build_contact_description, affichés sur les pages
# publiques payant.html et /test).
_CONTACT_FIELDS = ("phone", "line_id", "whatsapp", "email")

# Colonnes de suivi interne: aucun interet pour un client, et elles
# decrivent le fonctionnement du tracker (etat du hash, nombre de scans
# manques, identifiants cote source). `/listings` etant public, elles
# partaient a tout visiteur avec le reste de la ligne, `response_model`
# valant `list[dict]` -- FastAPI ne filtre alors rien.
_INTERNAL_FIELDS = (
    "content_hash", "missing_scan_count", "source", "source_id", "slug",
    "created_at", "updated_at", "last_scraped_at", "detail_scraped_at",
    "last_seen_at",
)


# Ce qui fait la valeur du catalogue, au-dela des coordonnees de contact:
# l'adresse exacte, la position GPS et surtout le lien vers l'annonce
# source. Livrer ce dernier, c'est livrer l'origine de chaque ligne, donc
# tout le travail d'agregation -- une vitrine n'en a pas besoin, elle
# montre quartier, prix et photos.
_PRECIOUS_FIELDS = ("address", "latitude", "longitude", "url")


def _public_limit(limit: int, authenticated: bool) -> int:
    """Plafonne la taille de page pour un visiteur non authentifie."""
    return limit if authenticated else min(limit, PUBLIC_DEMO_LIMIT)


def _public_offset(offset: int, authenticated: bool) -> int:
    """Neutralise la pagination pour un visiteur non authentifie.

    Indispensable au plafond de _public_limit: sans cela l'echantillon
    n'est que la premiere page d'une serie, et le catalogue s'aspire par
    tranches de PUBLIC_DEMO_LIMIT.
    """
    return offset if authenticated else 0


def _listing_to_response(
    listing: Listing,
    images: Optional[list[str]] = None,
    authenticated: bool = False,
) -> dict:
    d = {}
    for col in Listing.__table__.columns:
        val = getattr(listing, col.name)
        d[col.name] = val
    d["amenities"] = json.loads(listing.amenities) if listing.amenities else []
    d["room_types"] = json.loads(listing.room_types) if listing.room_types else []
    if images is None:
        ordered = sorted((i for i in listing.images if not i.excluded), key=lambda i: i.position)
        images = [img.image_url for img in ordered]
    d["images"] = images
    if not authenticated:
        for field in _CONTACT_FIELDS + _PRECIOUS_FIELDS:
            d[field] = None
    for field in _INTERNAL_FIELDS:
        d.pop(field, None)
    return d


def _thumbnail_map(db: SASession, listing_ids: list[int]) -> dict[int, str]:
    """URL de la premiere image non exclue de chaque annonce, en une requete.

    Les routes de liste n'affichent qu'une vignette: charger la relation
    complete ramenerait ~45 images par annonce pour n'en garder qu'une.
    """
    if not listing_ids:
        return {}
    first_position = (
        db.query(
            ListingImage.listing_id.label("listing_id"),
            func.min(ListingImage.position).label("position"),
        )
        .filter(
            ListingImage.listing_id.in_(listing_ids),
            # isnot(True) et non is_(False): une ligne dont excluded vaut
            # NULL etait visible avec l'ancien filtre Python (not None).
            ListingImage.excluded.isnot(True),
        )
        .group_by(ListingImage.listing_id)
        .subquery()
    )
    # Tri sur `id` avant de construire le dict: des positions dupliquees
    # existent en base (l'ancien calcul de position en produisait), et la
    # jointure rend alors deux lignes pour une meme annonce. Sans ordre
    # explicite, la vignette affichee changeait d'une requete a l'autre.
    # Le filtre `excluded` doit etre rappele ici: la sous-requete choisit
    # bien la plus petite position parmi les images non exclues, mais la
    # jointure ne porte que sur (listing_id, position). Or des positions
    # dupliquees existent en base (voir le commentaire ci-dessus), donc une
    # image exclue partageant cette position etait ramenee elle aussi, et
    # pouvait l'emporter dans le dict final -- exactement l'image qu'une
    # relecture manuelle avait ecartee.
    rows = (
        db.query(ListingImage.listing_id, ListingImage.image_url)
        .join(
            first_position,
            (ListingImage.listing_id == first_position.c.listing_id)
            & (ListingImage.position == first_position.c.position),
        )
        .filter(ListingImage.excluded.isnot(True))
        .order_by(desc(ListingImage.id))
        .all()
    )
    return {listing_id: url for listing_id, url in rows}


def _listings_to_response(
    db: SASession, listings: list[Listing], authenticated: bool = False
) -> list[dict]:
    """Serialise une liste d'annonces avec leur seule vignette."""
    thumbnails = _thumbnail_map(db, [l.id for l in listings])
    return [
        _listing_to_response(
            l,
            images=[thumbnails[l.id]] if l.id in thumbnails else [],
            authenticated=authenticated,
        )
        for l in listings
    ]


def _match_text(column, value):
    """Egalite insensible a la casse, jokers SQL neutralises.

    `ilike(f"%{value}%")` laissait `%` et `_` agir comme des jokers: sur une
    route publique, `?province=%` rendait tout le catalogue et un motif long
    forcait un balayage complet a chaque appel. Les valeurs stockees sont des
    libelles exacts ("Bangkok", "Bang Lamung"), et le frontend n'envoie que
    ceux-la: la correspondance exacte est donc aussi ce que veut l'appelant.
    """
    escaped = value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return column.ilike(escaped, escape="\\")


def _apply_filters(query, province, district, price_min, price_max, monthly, status):
    if province:
        query = query.filter(_match_text(Listing.province, province))
    if district:
        query = query.filter(_match_text(Listing.district, district))
    if price_min is not None:
        query = query.filter(
            (Listing.price_monthly_max >= price_min) |
            (Listing.price_monthly_max.is_(None))
        )
    if price_max is not None:
        query = query.filter(
            (Listing.price_monthly_min <= price_max) |
            (Listing.price_monthly_min.is_(None))
        )
    if monthly is not None:
        val = "true" if monthly else "false"
        query = query.filter(Listing.has_monthly_contract == val)
    if status:
        query = query.filter(Listing.status == status)
    return query


# — Endpoints —

@app.get("/listings", response_model=list[dict])
def get_listings(
    request: Request,
    province: Optional[str] = Query(None, max_length=100),
    district: Optional[str] = Query(None, max_length=100),
    price_min: Optional[int] = Query(None, ge=0, le=100_000_000),
    price_max: Optional[int] = Query(None, ge=0, le=100_000_000),
    monthly: Optional[bool] = Query(None),
    status: Optional[str] = Query(None, description="active | removed", max_length=20),
    updated_since: Optional[date] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    # Borne haute: `/listings` est public, et un offset non borne permet
    # d'iterer tout le catalogue page par page.
    offset: int = Query(0, ge=0, le=_MAX_OFFSET),
    db: SASession = Depends(get_db),
):
    authenticated = _is_authenticated(request)
    query = db.query(Listing)
    query = _apply_filters(query, province, district, price_min, price_max, monthly, status)

    if updated_since:
        query = query.filter(Listing.source_updated_at >= datetime.combine(updated_since, datetime.min.time()))

    # Departage par id: sans lui, deux annonces de meme source_updated_at
    # peuvent changer d'ordre entre deux requetes, et la pagination par
    # offset du frontend perd ou duplique des lignes.
    listings = (
        query.order_by(desc(Listing.source_updated_at), desc(Listing.id))
        .offset(_public_offset(offset, authenticated))
        .limit(_public_limit(limit, authenticated))
        .all()
    )

    return _listings_to_response(db, listings, authenticated=authenticated)


@app.get("/listings/new", response_model=list[dict])
def get_new_listings(
    request: Request,
    since_hours: int = Query(24, ge=1, le=_MAX_SINCE_HOURS, description="Annonces nouvelles depuis N heures"),
    limit: int = Query(500, ge=1, le=500),
    db: SASession = Depends(get_db),
):
    """Annonces détectées pour la première fois dans les N dernières heures."""
    from datetime import timedelta
    authenticated = _is_authenticated(request)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    listings = (
        db.query(Listing)
        .filter(Listing.first_seen_at >= cutoff)
        .order_by(desc(Listing.first_seen_at))
        .limit(_public_limit(limit, authenticated))
        .all()
    )
    return _listings_to_response(db, listings, authenticated=authenticated)


@app.get("/listings/updated", response_model=list[dict])
def get_updated_listings(
    request: Request,
    since_hours: int = Query(24, ge=1, le=_MAX_SINCE_HOURS),
    limit: int = Query(500, ge=1, le=500),
    db: SASession = Depends(get_db),
):
    from datetime import timedelta
    authenticated = _is_authenticated(request)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    listing_ids = (
        db.query(ListingHistory.listing_id)
        .filter(
            ListingHistory.change_type == "UPDATED",
            ListingHistory.changed_at >= cutoff,
        )
        .distinct()
        .scalar_subquery()
    )
    # Un `limit` sans `order_by` laisse le moteur choisir *quelles* lignes
    # il rend: deux appels identiques pouvaient renvoyer des sous-ensembles
    # differents. Meme tri que /listings, departage par id compris.
    listings = (
        db.query(Listing)
        .filter(Listing.id.in_(listing_ids))
        .order_by(desc(Listing.source_updated_at), desc(Listing.id))
        .limit(_public_limit(limit, authenticated))
        .all()
    )
    return _listings_to_response(db, listings, authenticated=authenticated)


@app.get("/listings/price-changed", response_model=list[dict])
def get_price_changed_listings(
    request: Request,
    since_hours: int = Query(24, ge=1, le=_MAX_SINCE_HOURS),
    limit: int = Query(500, ge=1, le=500),
    db: SASession = Depends(get_db),
):
    from datetime import timedelta
    authenticated = _is_authenticated(request)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    listing_ids = (
        db.query(ListingHistory.listing_id)
        .filter(
            ListingHistory.change_type == "PRICE_CHANGED",
            ListingHistory.changed_at >= cutoff,
        )
        .distinct()
        .scalar_subquery()
    )
    # Un `limit` sans `order_by` laisse le moteur choisir *quelles* lignes
    # il rend: deux appels identiques pouvaient renvoyer des sous-ensembles
    # differents. Meme tri que /listings, departage par id compris.
    listings = (
        db.query(Listing)
        .filter(Listing.id.in_(listing_ids))
        .order_by(desc(Listing.source_updated_at), desc(Listing.id))
        .limit(_public_limit(limit, authenticated))
        .all()
    )
    return _listings_to_response(db, listings, authenticated=authenticated)


@app.get("/listings/monthly", response_model=list[dict])
def get_monthly_listings(
    request: Request,
    province: Optional[str] = Query(None, max_length=100),
    price_max: Optional[int] = Query(None, ge=0, le=100_000_000),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0, le=_MAX_OFFSET),
    db: SASession = Depends(get_db),
):
    """Annonces avec Contract monthly disponible."""
    authenticated = _is_authenticated(request)
    query = (
        db.query(Listing)
        .filter(
            Listing.has_monthly_contract == "true",
            Listing.status == "active",
        )
    )
    if province:
        query = query.filter(_match_text(Listing.province, province))
    if price_max:
        query = query.filter(
            Listing.contract_monthly_min <= price_max
        )
    listings = (
        query.order_by(Listing.contract_monthly_min)
        .offset(_public_offset(offset, authenticated))
        .limit(_public_limit(limit, authenticated))
        .all()
    )
    return _listings_to_response(db, listings, authenticated=authenticated)


@app.get("/listings/{listing_id}", response_model=dict)
def get_listing(listing_id: int, request: Request, db: SASession = Depends(get_db)):
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Annonce non trouvée")
    return _listing_to_response(listing, authenticated=_is_authenticated(request))


@app.get("/history/{listing_id}", response_model=list[HistoryResponse])
def get_listing_history(listing_id: int, db: SASession = Depends(get_db)):
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Annonce non trouvée")
    history = (
        db.query(ListingHistory)
        .filter(ListingHistory.listing_id == listing_id)
        .order_by(desc(ListingHistory.changed_at))
        .all()
    )
    return history


@app.post("/rental-requests", response_model=RentalRequestResponse, status_code=201)
def create_rental_request(payload: RentalRequestCreate, db: SASession = Depends(get_db)):
    req = RentalRequest(
        city=payload.city,
        duration=payload.duration,
        budget=payload.budget,
        conditions=payload.conditions,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


@app.get("/provinces", response_model=list[ProvinceResponse])
def get_provinces(db: SASession = Depends(get_db)):
    provinces = db.query(Province).filter(Province.active.is_(True)).order_by(Province.name).all()
    return provinces


# response_model=None: la route rend deux formes selon la session
# (StatsResponse ou PublicStatsResponse), que FastAPI ne peut pas decrire
# par un modele unique sans rendre tous les champs optionnels.
@app.get("/stats", response_model=None)
def get_stats(request: Request, db: SASession = Depends(get_db)):
    from datetime import timedelta, date as date_type

    # "Aujourd'hui" au sens de l'utilisateur, donc dans settings.tz
    # (Asia/Bangkok): en UTC, la journee basculait avec 7 h de decalage et
    # les compteurs "du jour" repartaient de zero en milieu d'apres-midi.
    # ...puis reconverti en UTC: le type DATETIME de SQLite ecarte le
    # decalage a l'ecriture, donc les colonnes portent une heure murale UTC.
    # Comparer minuit *local* tel quel melangeait deux horloges et decalait
    # la frontiere du jour de 7 h.
    local_tz = ZoneInfo(settings.tz)
    today = datetime.now(local_tz).date()
    today_start = datetime.combine(
        today, datetime.min.time(), tzinfo=local_tz
    ).astimezone(timezone.utc)

    total_active = db.query(func.count(Listing.id)).filter(Listing.status == "active").scalar()
    monthly_count = db.query(func.count(Listing.id)).filter(
        Listing.has_monthly_contract == "true",
        Listing.status == "active",
    ).scalar()
    three_month_count = db.query(func.count(Listing.id)).filter(
        Listing.contract_3_month_raw.isnot(None),
        Listing.contract_3_month_raw.notin_(["", "-"]),
        Listing.status == "active",
    ).scalar()
    six_month_count = db.query(func.count(Listing.id)).filter(
        Listing.contract_6_month_raw.isnot(None),
        Listing.contract_6_month_raw.notin_(["", "-"]),
        Listing.status == "active",
    ).scalar()

    # `today_start` (minuit local, calcule plus haut) et non 24 h glissantes:
    # les trois compteurs "du jour" de cette reponse doivent mesurer la meme
    # fenetre. new_today utilisait `now - 24h` pendant que updated_today et
    # price_changed_today partaient de minuit, si bien que les trois chiffres
    # presentes cote a cote ne parlaient pas du meme intervalle.
    new_today = db.query(func.count(Listing.id)).filter(
        Listing.first_seen_at >= today_start
    ).scalar()

    # count(distinct listing_id) et non count(distinct id): `id` est la clé
    # primaire de l'historique, donc `distinct` n'y dédoublonne rien et on
    # comptait des lignes. Un seul changement de prix en écrit une par champ
    # touché, ce qui gonflait le chiffre d'environ un facteur 6.
    updated_today = (
        db.query(func.count(distinct(ListingHistory.listing_id)))
        .filter(
            ListingHistory.change_type == "UPDATED",
            ListingHistory.changed_at >= today_start,
        )
        .scalar()
    )
    price_changed_today = (
        db.query(func.count(distinct(ListingHistory.listing_id)))
        .filter(
            ListingHistory.change_type == "PRICE_CHANGED",
            ListingHistory.changed_at >= today_start,
        )
        .scalar()
    )

    if not _is_authenticated(request):
        # Sortie avant les agregats reserves aux comptes: `by_province`
        # est un GROUP BY sur toute la table, execute a chaque ouverture
        # de la vitrine s'il restait ici.
        return PublicStatsResponse(
            total_active=total_active or 0,
            new_today=new_today or 0,
            monthly_contract_count=monthly_count or 0,
            three_month_contract_count=three_month_count or 0,
            six_month_contract_count=six_month_count or 0,
        )

    total_removed = db.query(func.count(Listing.id)).filter(Listing.status == "removed").scalar()

    # Par province
    by_province = (
        db.query(Listing.province, func.count(Listing.id).label("count"))
        .filter(Listing.status == "active")
        .group_by(Listing.province)
        .order_by(desc("count"))
        .all()
    )
    by_province_list = [
        {"province": r.province or "Unknown", "count": r.count}
        for r in by_province
    ]

    # Dernier scan
    last_scan_row = (
        db.query(ScanLog.finished_at)
        .filter(ScanLog.status == "completed")
        .order_by(desc(ScanLog.finished_at))
        .first()
    )
    last_scan = last_scan_row.finished_at if last_scan_row else None

    return StatsResponse(
        total_active=total_active or 0,
        total_removed=total_removed or 0,
        new_today=new_today or 0,
        updated_today=updated_today or 0,
        price_changed_today=price_changed_today or 0,
        monthly_contract_count=monthly_count or 0,
        three_month_contract_count=three_month_count or 0,
        six_month_contract_count=six_month_count or 0,
        by_province=by_province_list,
        last_scan=last_scan,
    )


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Frontend statique ──────────────────────────────────────────────
# Doit être en dernier pour ne pas capturer les routes API

_FRONTEND_DIR = Path(__file__).parent.parent.parent / "frontend"

if _FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_FRONTEND_DIR)), name="static")

    @app.get("/")
    def serve_frontend():
        return FileResponse(str(_FRONTEND_DIR / "index.html"))
    @app.get("/premium.html")
    def serve_premium():
        return FileResponse(str(_FRONTEND_DIR / "premium.html"))
    @app.get("/carte.html")
    def serve_carte():
        return FileResponse(str(_FRONTEND_DIR / "carte.html"))
    @app.get("/carte-phuket.html")
    def serve_carte_phuket():
        return FileResponse(str(_FRONTEND_DIR / "carte-phuket.html"))
    @app.get("/carte-thailande.html")
    def serve_carte_thailande():
        return FileResponse(str(_FRONTEND_DIR / "carte-thailande.html"))
    @app.get("/carte-bangkok.html")
    def serve_carte_bangkok():
        return FileResponse(str(_FRONTEND_DIR / "carte-bangkok.html"))
    @app.get("/carte-pattaya.html")
    def serve_carte_pattaya():
        return FileResponse(str(_FRONTEND_DIR / "carte-pattaya.html"))
    @app.get("/payant.html")
    def serve_payant():
        return FileResponse(str(_FRONTEND_DIR / "payant.html"))
    @app.get("/vip.html")
    def serve_vip():
        return FileResponse(str(_FRONTEND_DIR / "vip.html"))
    @app.get("/test")
    def serve_test():
        return FileResponse(str(_FRONTEND_DIR / "test.html"))

