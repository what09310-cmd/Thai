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
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Depends, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session as SASession
from sqlalchemy import func, desc, distinct, inspect, text as sa_text
from starlette.middleware.base import BaseHTTPMiddleware

from src.database.session import SessionLocal, init_db, engine
from src.database.models import (
    Listing, ListingHistory, ListingImage, Province, ScanLog, RentalRequest,
)
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


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """En-tetes de securite sur toute reponse.

    `frame-ancestors 'self'` (et non frame-src): les pages de detail
    *integrent* des iframes Google Maps, qu'il ne faut surtout pas bloquer.
    Ce qu'on interdit, c'est que l'application soit elle-meme encadree par
    un site tiers (clickjacking sur les pages publiques).
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response


app.add_middleware(SecurityHeadersMiddleware)


PUBLIC_PATHS = {
    "/login",
    "/health",
    "/payant.html",
    "/stats",
    "/test",
}
PUBLIC_PATH_PREFIXES = (
    "/listings",
)

_STATIC_PREFIX = "/static/"


def _is_public(path: str) -> bool:
    """Determine si un chemin est accessible sans cookie de session.

    Le montage /static sert tout le repertoire `frontend/`: on n'y laisse
    passer librement que les assets (logo, JSON de coordonnees). Une page
    .html atteinte par ce biais suit la meme regle que sa route directe,
    sinon /static/index.html contourne purement et simplement le login.
    """
    if path.startswith(_STATIC_PREFIX):
        name = path[len(_STATIC_PREFIX):]
        if not name.lower().endswith((".html", ".htm")):
            return True
        return f"/{name}" in PUBLIC_PATHS
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PATH_PREFIXES)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _is_public(path):
            return await call_next(request)
        if not verify_session_token(request.cookies.get(SESSION_COOKIE_NAME)):
            return RedirectResponse(url="/login")
        return await call_next(request)


app.add_middleware(AuthMiddleware)


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
    city: str
    duration: str
    budget: str
    conditions: Optional[str] = None


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


# — Helpers —

# Coordonnées de contact directes: visibles seulement des visiteurs
# authentifiés. `deposit`/`electric_price` restent publics (repris dans
# `description` via build_contact_description, affichés sur les pages
# publiques payant.html et /test).
_CONTACT_FIELDS = ("phone", "line_id", "whatsapp", "email")


def _listing_to_response(
    listing: Listing,
    images: Optional[list[str]] = None,
    include_contact: bool = False,
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
    if not include_contact:
        for field in _CONTACT_FIELDS:
            d[field] = None
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
    rows = (
        db.query(ListingImage.listing_id, ListingImage.image_url)
        .join(
            first_position,
            (ListingImage.listing_id == first_position.c.listing_id)
            & (ListingImage.position == first_position.c.position),
        )
        .order_by(desc(ListingImage.id))
        .all()
    )
    return {listing_id: url for listing_id, url in rows}


def _listings_to_response(
    db: SASession, listings: list[Listing], include_contact: bool = False
) -> list[dict]:
    """Serialise une liste d'annonces avec leur seule vignette."""
    thumbnails = _thumbnail_map(db, [l.id for l in listings])
    return [
        _listing_to_response(
            l,
            images=[thumbnails[l.id]] if l.id in thumbnails else [],
            include_contact=include_contact,
        )
        for l in listings
    ]


def _apply_filters(query, province, district, price_min, price_max, monthly, status):
    if province:
        query = query.filter(Listing.province.ilike(f"%{province}%"))
    if district:
        query = query.filter(Listing.district.ilike(f"%{district}%"))
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
    province: Optional[str] = Query(None),
    district: Optional[str] = Query(None),
    price_min: Optional[int] = Query(None),
    price_max: Optional[int] = Query(None),
    monthly: Optional[bool] = Query(None),
    status: Optional[str] = Query(None, description="active | removed"),
    updated_since: Optional[date] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: SASession = Depends(get_db),
):
    query = db.query(Listing)
    query = _apply_filters(query, province, district, price_min, price_max, monthly, status)

    if updated_since:
        query = query.filter(Listing.source_updated_at >= datetime.combine(updated_since, datetime.min.time()))

    # Departage par id: sans lui, deux annonces de meme source_updated_at
    # peuvent changer d'ordre entre deux requetes, et la pagination par
    # offset du frontend perd ou duplique des lignes.
    listings = (
        query.order_by(desc(Listing.source_updated_at), desc(Listing.id))
        .offset(offset)
        .limit(limit)
        .all()
    )

    return _listings_to_response(db, listings, include_contact=_is_authenticated(request))


@app.get("/listings/new", response_model=list[dict])
def get_new_listings(
    request: Request,
    since_hours: int = Query(24, ge=1, le=_MAX_SINCE_HOURS, description="Annonces nouvelles depuis N heures"),
    limit: int = Query(500, ge=1, le=500),
    db: SASession = Depends(get_db),
):
    """Annonces détectées pour la première fois dans les N dernières heures."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    listings = (
        db.query(Listing)
        .filter(Listing.first_seen_at >= cutoff)
        .order_by(desc(Listing.first_seen_at))
        .limit(limit)
        .all()
    )
    return _listings_to_response(db, listings, include_contact=_is_authenticated(request))


@app.get("/listings/updated", response_model=list[dict])
def get_updated_listings(
    request: Request,
    since_hours: int = Query(24, ge=1, le=_MAX_SINCE_HOURS),
    limit: int = Query(500, ge=1, le=500),
    db: SASession = Depends(get_db),
):
    from datetime import timedelta
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
    listings = (
        db.query(Listing)
        .filter(Listing.id.in_(listing_ids))
        .limit(limit)
        .all()
    )
    return _listings_to_response(db, listings, include_contact=_is_authenticated(request))


@app.get("/listings/price-changed", response_model=list[dict])
def get_price_changed_listings(
    request: Request,
    since_hours: int = Query(24, ge=1, le=_MAX_SINCE_HOURS),
    limit: int = Query(500, ge=1, le=500),
    db: SASession = Depends(get_db),
):
    from datetime import timedelta
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
    listings = (
        db.query(Listing)
        .filter(Listing.id.in_(listing_ids))
        .limit(limit)
        .all()
    )
    return _listings_to_response(db, listings, include_contact=_is_authenticated(request))


@app.get("/listings/monthly", response_model=list[dict])
def get_monthly_listings(
    request: Request,
    province: Optional[str] = Query(None),
    price_max: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: SASession = Depends(get_db),
):
    """Annonces avec Contract monthly disponible."""
    query = (
        db.query(Listing)
        .filter(
            Listing.has_monthly_contract == "true",
            Listing.status == "active",
        )
    )
    if province:
        query = query.filter(Listing.province.ilike(f"%{province}%"))
    if price_max:
        query = query.filter(
            Listing.contract_monthly_min <= price_max
        )
    listings = (
        query.order_by(Listing.contract_monthly_min)
        .offset(offset)
        .limit(limit)
        .all()
    )
    return _listings_to_response(db, listings, include_contact=_is_authenticated(request))


@app.get("/listings/{listing_id}", response_model=dict)
def get_listing(listing_id: int, request: Request, db: SASession = Depends(get_db)):
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Annonce non trouvée")
    return _listing_to_response(listing, include_contact=_is_authenticated(request))


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


@app.get("/stats", response_model=StatsResponse)
def get_stats(db: SASession = Depends(get_db)):
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
    total_removed = db.query(func.count(Listing.id)).filter(Listing.status == "removed").scalar()
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

    last_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    new_today = db.query(func.count(Listing.id)).filter(
        Listing.first_seen_at >= last_24h
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

