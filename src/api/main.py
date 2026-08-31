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
from datetime import date, datetime, timezone
from typing import Optional

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Depends, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session as SASession
from sqlalchemy import func, desc, inspect, text as sa_text
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
    verify_session_token,
)

log = logging.getLogger(__name__)

app = FastAPI(
    title="RentHub Tracker API",
    description="API de suivi des annonces RentHub (Short-term Monthly, Thaïlande)",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # allow_origins="*" et allow_credentials=True sont incompatibles: le
    # navigateur refuse la reponse. Le frontend est servi par cette meme
    # application, le cookie de session n'a donc pas besoin du CORS.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


PUBLIC_PATHS = {
    "/login",
    "/health",
    "/carte-thailande.html",
    "/payant.html",
    "/premium.html",
    "/stats",
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


@app.get("/login", response_class=HTMLResponse)
def login_form():
    return _LOGIN_PAGE.format(error="")


@app.post("/login")
def login_submit(username: str = Form(...), password: str = Form(...)):
    if not check_credentials(username, password):
        return HTMLResponse(_LOGIN_PAGE.format(error='<div class="error">Identifiant ou mot de passe incorrect</div>'))
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        create_session_token(),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return response


@app.on_event("startup")
def _on_startup() -> None:
    init_db()
    inspector = inspect(engine)
    if "rental_requests" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("rental_requests")}
        if "city" not in cols:
            with engine.begin() as conn:
                conn.execute(sa_text("ALTER TABLE rental_requests ADD COLUMN city VARCHAR(50)"))


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

    class Config:
        from_attributes = True


class HistoryResponse(BaseModel):
    id: int
    changed_at: datetime
    change_type: str
    field_name: Optional[str]
    old_value: Optional[str]
    new_value: Optional[str]

    class Config:
        from_attributes = True


class ProvinceResponse(BaseModel):
    id: int
    name: str
    slug: str
    renthub_url: Optional[str]
    listing_count: Optional[int]
    active: bool
    last_scan: Optional[datetime]

    class Config:
        from_attributes = True


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

    class Config:
        from_attributes = True


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

def _listing_to_response(listing: Listing, images: Optional[list[str]] = None) -> dict:
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
    rows = (
        db.query(ListingImage.listing_id, ListingImage.image_url)
        .join(
            first_position,
            (ListingImage.listing_id == first_position.c.listing_id)
            & (ListingImage.position == first_position.c.position),
        )
        .all()
    )
    return {listing_id: url for listing_id, url in rows}


def _listings_to_response(db: SASession, listings: list[Listing]) -> list[dict]:
    """Serialise une liste d'annonces avec leur seule vignette."""
    thumbnails = _thumbnail_map(db, [l.id for l in listings])
    return [
        _listing_to_response(l, images=[thumbnails[l.id]] if l.id in thumbnails else [])
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
    province: Optional[str] = Query(None),
    district: Optional[str] = Query(None),
    price_min: Optional[int] = Query(None),
    price_max: Optional[int] = Query(None),
    monthly: Optional[bool] = Query(None),
    status: Optional[str] = Query(None, description="active | removed"),
    updated_since: Optional[date] = Query(None),
    limit: int = Query(50, le=500),
    offset: int = Query(0),
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

    return _listings_to_response(db, listings)


@app.get("/listings/new", response_model=list[dict])
def get_new_listings(
    since_hours: int = Query(24, description="Annonces nouvelles depuis N heures"),
    db: SASession = Depends(get_db),
):
    """Annonces détectées pour la première fois dans les N dernières heures."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    listings = (
        db.query(Listing)
        .filter(Listing.first_seen_at >= cutoff)
        .order_by(desc(Listing.first_seen_at))
        .all()
    )
    return _listings_to_response(db, listings)


@app.get("/listings/updated", response_model=list[dict])
def get_updated_listings(
    since_hours: int = Query(24),
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
        .subquery()
    )
    listings = (
        db.query(Listing)
        .filter(Listing.id.in_(listing_ids))
        .all()
    )
    return _listings_to_response(db, listings)


@app.get("/listings/price-changed", response_model=list[dict])
def get_price_changed_listings(
    since_hours: int = Query(24),
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
        .subquery()
    )
    listings = (
        db.query(Listing)
        .filter(Listing.id.in_(listing_ids))
        .all()
    )
    return _listings_to_response(db, listings)


@app.get("/listings/monthly", response_model=list[dict])
def get_monthly_listings(
    province: Optional[str] = Query(None),
    price_max: Optional[int] = Query(None),
    limit: int = Query(50, le=500),
    offset: int = Query(0),
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
    return _listings_to_response(db, listings)


@app.get("/listings/{listing_id}", response_model=dict)
def get_listing(listing_id: int, db: SASession = Depends(get_db)):
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Annonce non trouvée")
    return _listing_to_response(listing)


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

    today = datetime.now(timezone.utc).date()
    today_start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)

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

    updated_today = (
        db.query(func.count(ListingHistory.id.distinct()))
        .filter(
            ListingHistory.change_type == "UPDATED",
            ListingHistory.changed_at >= today_start,
        )
        .scalar()
    )
    price_changed_today = (
        db.query(func.count(ListingHistory.id.distinct()))
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

