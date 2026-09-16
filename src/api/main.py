"""
API REST FastAPI de ThaiMonth.

Endpoints de donnees (publics, plafonnes en debit):
  GET /listings          catalogue pagine, filtrable
  GET /listings/{id}     fiche complete
  GET /stats             compteurs du hero (forme reduite sans compte premium)
  GET /health

Comptes (src/api/auth_routes.py): GET/POST /login, GET/POST /register,
/auth/google[/callback], POST /logout, GET /me. Middlewares et gate par
page: src/api/security.py. Pages: "/" et les .html de frontend/.

Les routes /listings/new|updated|price-changed|monthly, /history/{id},
/provinces et POST /rental-requests ont ete retirees: aucune page ne les
appelait, et chaque route publique est de la surface d'attaque a defendre.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import desc, distinct, func
from sqlalchemy.orm import Session as SASession
from starlette.middleware.sessions import SessionMiddleware

from src.api import auth_routes
from src.api.deps import get_db
from src.api.security import (
    AuthMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    _is_premium,
)
from src.config import DEFAULT_SECRET_KEY, DEFAULT_SITE_PASSWORD, settings
from src.database.models import Listing, ListingHistory, ListingImage, ScanLog
from src.database.serialize import listing_to_dict
from src.database.session import init_db

log = logging.getLogger(__name__)

app = FastAPI(
    title="ThaiMonth API",
    description="API ThaiMonth: suivi des locations courte durée en Thaïlande (source renthub.in.th)",
    version="1.0.0",
)

# Sans plafond, `offset` est le moyen le plus simple d'aspirer le catalogue
# depuis une route publique.
_MAX_OFFSET = 100_000

# Un visiteur non authentifie recoit le catalogue entier avec des
# coordonnees GPS *approximatives* (les cartes carte-*.html sont publiques
# et en ont besoin), mais sans contacts, adresse exacte ni lien source:
# voir _listing_to_response et _approximate_position.

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


app.add_middleware(AuthMiddleware)
app.add_middleware(RateLimitMiddleware)
# Le catalogue complet (index.html en charge ~1 200 annonces) pese ~3 MB de
# JSON, et une page HTML 80-110 KB: compresses, c'est 5 a 6 fois moins.
# Ni uvicorn ni Render ne compressent d'eux-memes.
app.add_middleware(GZipMiddleware, minimum_size=1000)
# Requis par authlib pour stocker le `state` anti-CSRF entre /auth/google et
# le callback. Cookie distinct de SESSION_COOKIE_NAME ("session"), sinon les
# deux s'ecrasent; vide en dehors du parcours OAuth.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="oauth_state",
    max_age=10 * 60,
    same_site="lax",
    https_only=settings.cookie_secure,
)
# Ajoute en dernier => middleware le plus externe: ses en-tetes couvrent
# aussi les redirections emises par AuthMiddleware.
app.add_middleware(SecurityHeadersMiddleware)


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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Demarrage: refus des secrets par defaut, puis creation des tables
    manquantes. Les migrations d'une base existante (colonnes, index) sont
    du ressort de scripts/migrate.py, pas du serveur."""
    _assert_secrets_configured()
    init_db()
    yield


app.router.lifespan_context = lifespan
app.include_router(auth_routes.router)


# — Response models —

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
_CONTACT_FIELDS = ("phone", "line_id", "whatsapp", "email", "line_verified")

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
_PRECIOUS_FIELDS = ("address", "url")


# Rayon du flou applique a la position d'un visiteur anonyme (metres),
# facon Airbnb: le point rendu est decale de FUZZ_MIN..FUZZ_MAX metres dans
# une direction fixe par annonce, et la carte dessine un cercle de
# FUZZ_CIRCLE_M de rayon autour, dans lequel se trouve toujours la vraie
# position. Le frontend doit garder FUZZ_CIRCLE_M >= FUZZ_MAX_M.
FUZZ_MIN_M = 100
FUZZ_MAX_M = 300
FUZZ_CIRCLE_M = 350


def _approximate_position(listing_id: int, lat: float, lon: float) -> tuple[float, float]:
    """Decale une position de facon deterministe et imprevisible.

    Deterministe (derive de l'id via la cle secrete) pour qu'un client ne
    puisse pas retrouver le vrai point en moyennant plusieurs requetes;
    derive de SECRET_KEY pour qu'on ne puisse pas recalculer le decalage
    depuis l'id seul.
    """
    digest = hmac.new(
        settings.secret_key.encode(), f"geo:{listing_id}".encode(), hashlib.sha256
    ).digest()
    angle = int.from_bytes(digest[:4], "big") / 2**32 * 2 * math.pi
    dist = FUZZ_MIN_M + int.from_bytes(digest[4:8], "big") / 2**32 * (FUZZ_MAX_M - FUZZ_MIN_M)
    dlat = dist * math.cos(angle) / 111_320
    dlon = dist * math.sin(angle) / (111_320 * math.cos(math.radians(lat)))
    return round(lat + dlat, 5), round(lon + dlon, 5)


def _listing_to_response(
    listing: Listing,
    images: Optional[list[str]] = None,
    premium: bool = False,
) -> dict:
    d = listing_to_dict(listing)
    if images is None:
        ordered = sorted((i for i in listing.images if not i.excluded), key=lambda i: i.position)
        images = [img.image_url for img in ordered]
    d["images"] = images
    d["location_approx"] = False
    if not premium:
        for field in _CONTACT_FIELDS + _PRECIOUS_FIELDS:
            d[field] = None
        if d.get("latitude") is not None and d.get("longitude") is not None:
            d["latitude"], d["longitude"] = _approximate_position(
                listing.id, d["latitude"], d["longitude"]
            )
            d["location_approx"] = True
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
    db: SASession, listings: list[Listing], premium: bool = False
) -> list[dict]:
    """Serialise une liste d'annonces avec leur seule vignette."""
    thumbnails = _thumbnail_map(db, [l.id for l in listings])
    return [
        _listing_to_response(
            l,
            images=[thumbnails[l.id]] if l.id in thumbnails else [],
            premium=premium,
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
    premium = _is_premium(request)
    query = db.query(Listing)
    query = _apply_filters(query, province, district, price_min, price_max, monthly, status)

    if updated_since:
        # Minuit *local* (settings.tz) converti en UTC, comme les compteurs
        # "du jour" de /stats: les colonnes portent une heure murale UTC, et
        # un minuit naif etait lu comme minuit UTC, soit 7 h de decalage
        # sur la frontiere du jour a Bangkok.
        since = datetime.combine(
            updated_since, datetime.min.time(), tzinfo=ZoneInfo(settings.tz)
        ).astimezone(timezone.utc)
        query = query.filter(Listing.source_updated_at >= since)

    # Departage par id: sans lui, deux annonces de meme source_updated_at
    # peuvent changer d'ordre entre deux requetes, et la pagination par
    # offset du frontend perd ou duplique des lignes.
    listings = (
        query.order_by(desc(Listing.source_updated_at), desc(Listing.id))
        .offset(offset)
        .limit(limit)
        .all()
    )

    return _listings_to_response(db, listings, premium=premium)


@app.get("/listings/{listing_id}", response_model=dict)
def get_listing(listing_id: int, request: Request, db: SASession = Depends(get_db)):
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Annonce non trouvée")
    return _listing_to_response(listing, premium=_is_premium(request))


# Les compteurs ne bougent qu'au scan (une fois par jour): recalculer six a
# huit COUNT a chaque ouverture de la vitrine, qui appelle /stats au
# chargement, ne sert a rien. Deux entrees, publique et complete, chacune
# valable _STATS_CACHE_SECONDS. Un seul worker => un dict de module suffit,
# comme pour les compteurs de debit.
_STATS_CACHE_SECONDS = 60
_stats_cache: dict[str, tuple[float, BaseModel]] = {}


def reset_stats_cache() -> None:
    """Vide le cache (tests: chaque test repart d'une base vide)."""
    _stats_cache.clear()


def _today_start_utc() -> datetime:
    """Minuit *local* (settings.tz, Asia/Bangkok) exprime en UTC.

    Les colonnes portent une heure murale UTC (le type DATETIME de SQLite
    ecarte le decalage a l'ecriture): comparer minuit local tel quel
    melangeait deux horloges et decalait la frontiere du jour de 7 h; en
    UTC pur, les compteurs "du jour" repartaient de zero en milieu
    d'apres-midi pour l'utilisateur.
    """
    local_tz = ZoneInfo(settings.tz)
    today = datetime.now(local_tz).date()
    return datetime.combine(today, datetime.min.time(), tzinfo=local_tz).astimezone(timezone.utc)


def _count_active(db: SASession, *criteria) -> int:
    return db.query(func.count(Listing.id)).filter(Listing.status == "active", *criteria).scalar() or 0


def _count_changed_today(db: SASession, change_type: str, today_start: datetime) -> int:
    # count(distinct listing_id) et non count(distinct id): `id` est la cle
    # primaire de l'historique, donc `distinct` n'y dedoublonne rien. Un seul
    # changement de prix ecrit une ligne par champ touche (facteur ~6).
    return (
        db.query(func.count(distinct(ListingHistory.listing_id)))
        .filter(ListingHistory.change_type == change_type, ListingHistory.changed_at >= today_start)
        .scalar()
        or 0
    )


# response_model=None: la route rend deux formes selon la session
# (StatsResponse ou PublicStatsResponse), que FastAPI ne peut pas decrire
# par un modele unique sans rendre tous les champs optionnels.
@app.get("/stats", response_model=None)
def get_stats(request: Request, db: SASession = Depends(get_db)):
    premium = _is_premium(request)
    key = "full" if premium else "public"
    cached = _stats_cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    # `today_start` et non 24 h glissantes: les trois compteurs "du jour"
    # doivent mesurer la meme fenetre.
    today_start = _today_start_utc()
    public = PublicStatsResponse(
        total_active=_count_active(db),
        new_today=db.query(func.count(Listing.id)).filter(Listing.first_seen_at >= today_start).scalar() or 0,
        monthly_contract_count=_count_active(db, Listing.has_monthly_contract == "true"),
        three_month_contract_count=_count_active(
            db, Listing.contract_3_month_raw.isnot(None), Listing.contract_3_month_raw.notin_(["", "-"])
        ),
        six_month_contract_count=_count_active(
            db, Listing.contract_6_month_raw.isnot(None), Listing.contract_6_month_raw.notin_(["", "-"])
        ),
    )
    if not premium:
        # Sortie avant les agregats reserves aux comptes: `by_province` est
        # un GROUP BY sur toute la table, et les compteurs d'historique deux
        # COUNT DISTINCT de plus -- calcules puis jetes, avant cette garde.
        _stats_cache[key] = (time.monotonic() + _STATS_CACHE_SECONDS, public)
        return public

    by_province = (
        db.query(Listing.province, func.count(Listing.id).label("count"))
        .filter(Listing.status == "active")
        .group_by(Listing.province)
        .order_by(desc("count"))
        .all()
    )
    last_scan_row = (
        db.query(ScanLog.finished_at)
        .filter(ScanLog.status == "completed")
        .order_by(desc(ScanLog.finished_at))
        .first()
    )
    full = StatsResponse(
        **public.model_dump(),
        total_removed=db.query(func.count(Listing.id)).filter(Listing.status == "removed").scalar() or 0,
        updated_today=_count_changed_today(db, "UPDATED", today_start),
        price_changed_today=_count_changed_today(db, "PRICE_CHANGED", today_start),
        by_province=[{"province": r.province or "Unknown", "count": r.count} for r in by_province],
        last_scan=last_scan_row.finished_at if last_scan_row else None,
    )
    _stats_cache[key] = (time.monotonic() + _STATS_CACHE_SECONDS, full)
    return full


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Frontend statique ──────────────────────────────────────────────
# Doit être en dernier pour ne pas capturer les routes API

_FRONTEND_DIR = Path(__file__).parent.parent.parent / "frontend"

# Pages servies sur leur route courte ("/payant.html" en plus de
# "/static/payant.html"), en plus de "/" (index.html) et "/test"
# (test.html, la vitrine). Liste blanche explicite: tout autre nom rend
# 404 sans jamais toucher le disque.
_PAGES = (
    "premium.html", "carte-thailande.html", "carte-bangkok.html",
    "carte-pattaya.html", "carte-phuket.html", "payant.html", "vip.html",
)

if _FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_FRONTEND_DIR)), name="static")

    @app.get("/")
    def serve_frontend():
        return FileResponse(str(_FRONTEND_DIR / "index.html"))

    @app.get("/test")
    def serve_test():
        return FileResponse(str(_FRONTEND_DIR / "test.html"))

    @app.get("/{page}.html")
    def serve_page(page: str):
        name = f"{page}.html"
        if name not in _PAGES:
            raise HTTPException(status_code=404)
        return FileResponse(str(_FRONTEND_DIR / name))
