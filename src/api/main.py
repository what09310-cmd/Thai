"""
API REST FastAPI pour le tracker RentHub.

Endpoints de donnees (publics, plafonnes en debit):
  GET /listings          catalogue pagine, filtrable
  GET /listings/{id}     fiche complete
  GET /stats             compteurs du hero (forme reduite sans compte premium)
  GET /health

Comptes: GET/POST /login, GET/POST /register, /auth/google[/callback],
POST /logout, GET /me. Pages: "/" et les .html de frontend/.

Les routes /listings/new|updated|price-changed|monthly, /history/{id},
/provinces et POST /rental-requests ont ete retirees: aucune page ne les
appelait, et chaque route publique est de la surface d'attaque a defendre.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import posixpath
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import desc, distinct, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SASession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from src.api import auth as auth_module
from src.api import rate_limit
from src.api.auth import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    SessionInfo,
    authenticate_user,
    check_credentials,
    create_session_token,
    get_or_create_google_user,
    get_user_by_email,
    is_login_rate_limited,
    normalize_email,
    password_problem,
    read_session_token,
    register_failed_login,
    register_successful_login,
    register_user,
    token_for_user,
)
from src.config import DEFAULT_SECRET_KEY, DEFAULT_SITE_PASSWORD, settings
from src.database.models import Listing, ListingHistory, ListingImage, ScanLog, User
from src.database.session import SessionLocal, init_db

log = logging.getLogger(__name__)

app = FastAPI(
    title="RentHub Tracker API",
    description="API de suivi des annonces RentHub (Short-term Monthly, Thaïlande)",
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
        # Les assets (images de provinces, coordonnees de quartiers, JS/CSS
        # partages) sont immuables entre deux deploiements: sans cet en-tete
        # le navigateur les retelechargeait a chaque visite. Les pages HTML
        # servies sur leur route directe ("/", /payant.html...) ne sont pas
        # concernees: elles passent par le gate d'authentification.
        if request.url.path.startswith(_STATIC_PREFIX) and response.status_code == 200:
            response.headers.setdefault("Cache-Control", f"public, max-age={_STATIC_MAX_AGE}")
        return response


# Un jour: assez pour ne pas retelecharger 3 MB d'images a chaque visite,
# assez court pour qu'un deploiement soit visible le lendemain sans purge.
_STATIC_MAX_AGE = 24 * 3600


# Seules ces pages exigent une session; tout le reste (autres pages, API,
# assets) est accessible sans login. index.html est le nom de fichier
# derriere "/", il faut donc le proteger aussi sous /static. La valeur dit
# le niveau requis: True = compte premium (ou admin), False = n'importe
# quel compte connecte.
PROTECTED_PATHS = {
    "/": False,
    "/index.html": False,
    "/vip.html": True,
}

# Un compte connecte mais non premium qui demande une page premium est
# envoye vers l'offre, pas vers le login qu'il a deja passe.
_UPSELL_URL = "/premium.html"

_STATIC_PREFIX = "/static/"

# Routes servant des donnees, par opposition aux pages et aux assets. Ce
# sont elles qu'on plafonne en debit: /health est sonde une fois par
# seconde au demarrage par le lanceur (renthub.ps1) et /login tient deja
# son propre compteur (src/api/auth.py).
_RATE_LIMITED_PREFIXES = (
    "/listings",
    "/stats",
)


def _matches_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    """Prefixe compare sur des segments entiers.

    `path.startswith("/listings")` faisait aussi passer /listings-admin ou
    /listingsanything: la premiere route ainsi nommee serait nee sans
    authentification, sans que personne ne s'en apercoive.
    """
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def _is_public(path: str) -> bool:
    """Determine si un chemin est accessible sans cookie de session.

    Seules les pages de PROTECTED_PATHS exigent une session. Le montage
    /static sert tout le repertoire `frontend/`: une page protegee atteinte
    par ce biais suit la meme regle que sa route directe, sinon
    /static/index.html contourne purement et simplement le login.

    Le chemin est normalise AVANT d'etre juge, parce que StaticFiles lui
    applique `os.path.normpath` ensuite: "/static/index.html/" ou les
    variantes NTFS "index.html." et "index.html%20" servaient index.html
    sans passer par le login.
    """
    if path.startswith(_STATIC_PREFIX):
        name = posixpath.normpath(path[len(_STATIC_PREFIX):]).rstrip(". ")
        if not name or name.startswith(("/", "../")) or name in ("..", "."):
            return False
        # Comparaison insensible a la casse: NTFS sert index.HTML comme index.html.
        return f"/{name.lower()}" not in PROTECTED_PATHS
    return path not in PROTECTED_PATHS


def _required_level(path: str) -> bool:
    """Niveau exige par une page protegee (voir PROTECTED_PATHS)."""
    if path.startswith(_STATIC_PREFIX):
        name = posixpath.normpath(path[len(_STATIC_PREFIX):]).rstrip(". ")
        return PROTECTED_PATHS.get(f"/{name.lower()}", True)
    return PROTECTED_PATHS.get(path, True)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _is_public(path):
            return await call_next(request)
        info = _session(request)
        if info is None:
            return RedirectResponse(url="/login")
        if _required_level(path) and not info.is_premium:
            return RedirectResponse(url=_UPSELL_URL)
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


_AUTH_PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0;
         padding: 16px; box-sizing: border-box; }}
  .card {{ background: #1e293b; padding: 2rem; border-radius: 8px; width: 100%; max-width: 320px; }}
  h2 {{ margin-top: 0; }}
  label {{ display: block; margin-top: 0.75rem; }}
  input {{ width: 100%; padding: 0.6rem; margin-top: 0.4rem; border-radius: 4px; border: 1px solid #334155;
           background: #0f172a; color: #e2e8f0; box-sizing: border-box; }}
  button, .google {{ display: block; width: 100%; padding: 0.6rem; border: none; border-radius: 4px;
            margin-top: 1rem; background: #3b82f6; color: white; cursor: pointer; font-weight: 600;
            text-align: center; text-decoration: none; box-sizing: border-box; font-size: 1rem; }}
  .google {{ background: #fff; color: #1f2937; }}
  .sep {{ text-align: center; color: #64748b; margin: 1rem 0 0; font-size: 0.85rem; }}
  .alt {{ margin-top: 1.25rem; font-size: 0.9rem; color: #94a3b8; text-align: center; }}
  .alt a {{ color: #93c5fd; }}
  .error {{ color: #f87171; margin-bottom: 0.5rem; }}
</style>
</head>
<body>
  <div class="card">
    <h2>{title}</h2>
    {error}
    <form method="post" action="{action}">
      <label for="username">Email{admin_hint}</label>
      <input type="text" id="username" name="username" autofocus required autocomplete="{username_autocomplete}">
      <label for="password">Mot de passe</label>
      <input type="password" id="password" name="password" required autocomplete="{password_autocomplete}" minlength="{min_length}">
      <button type="submit">{submit}</button>
    </form>
    {google}
    <div class="alt">{alt}</div>
  </div>
</body>
</html>"""

_GOOGLE_BUTTON = (
    '<p class="sep">ou</p>'
    '<a class="google" href="/auth/google">Continuer avec Google</a>'
)


def _google_configured() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def _render_auth_page(mode: str, error: str = "") -> str:
    err = f'<div class="error">{error}</div>' if error else ""
    google = _GOOGLE_BUTTON if _google_configured() else ""
    if mode == "register":
        return _AUTH_PAGE.format(
            title="Créer un compte", error=err, action="/register", admin_hint="",
            username_autocomplete="email", password_autocomplete="new-password",
            min_length=auth_module.PASSWORD_MIN_LENGTH, submit="Créer mon compte", google=google,
            alt='Déjà un compte ? <a href="/login">Se connecter</a>',
        )
    return _AUTH_PAGE.format(
        title="Connexion", error=err, action="/login", admin_hint=" ou identifiant",
        username_autocomplete="username", password_autocomplete="current-password",
        min_length=1, submit="Entrer", google=google,
        alt='Pas encore de compte ? <a href="/register">Créer un compte</a>',
    )


oauth = OAuth()
if _google_configured():
    oauth.register(
        "google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


# — Dependency —

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _client_key(request: Request) -> str:
    """Adresse du client, cle des compteurs de debit et d'echecs de login.

    Derriere un proxy inverse (tunnel Cloudflare du lanceur, Render,
    docker-compose derriere nginx), `request.client.host` est l'adresse du
    proxy: tous les visiteurs partageaient alors un seul budget de 60
    requetes par minute -- les cartes cassaient des deux ou trois visiteurs
    simultanes -- et cinq mots de passe faux saisis par n'importe qui
    verrouillaient /login pour tout le monde pendant quinze minutes.

    `--proxy-headers --forwarded-allow-ips="*"` (Dockerfile, render.yaml)
    ne corrige pas cela: uvicorn prend alors la *premiere* adresse de
    X-Forwarded-For, c'est-a-dire celle que le client a ecrite lui-meme, et
    les deux plafonds se contournent en changeant l'en-tete a chaque
    requete. On lit donc l'en-tete depuis la fin, en remontant d'autant de
    sauts qu'il y a de proxys de confiance (TRUSTED_PROXY_HOPS): le dernier
    element a ete ecrit par le proxy qui nous parle, pas par le client.
    """
    hops = settings.trusted_proxy_hops
    if hops > 0:
        forwarded = [
            part.strip()
            for part in request.headers.get("x-forwarded-for", "").split(",")
            if part.strip()
        ]
        if len(forwarded) >= hops:
            return forwarded[-hops]
    return request.client.host if request.client else "unknown"


def _forwarded_https(request: Request) -> bool:
    """Vrai si un proxy de confiance annonce que le client parle en HTTPS.

    Le tunnel Cloudflare et Render terminent le TLS: l'application voit du
    http, et `request.url_for` fabrique des URLs en http:// -- que Google
    refuse comme URI de redirection OAuth. L'en-tete n'est cru que derriere
    un proxy declare (TRUSTED_PROXY_HOPS), sinon n'importe quel client
    pourrait le poser.
    """
    if settings.trusted_proxy_hops <= 0:
        return False
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return proto == "https"


def _session(request: Request) -> SessionInfo | None:
    return read_session_token(request.cookies.get(SESSION_COOKIE_NAME))


def _is_authenticated(request: Request) -> bool:
    """N'importe quel compte connecte (budget de requetes plus large)."""
    return _session(request) is not None


def _is_premium(request: Request) -> bool:
    """Compte premium ou administrateur: contacts, adresse et lien source."""
    info = _session(request)
    return info is not None and info.is_premium


def _login_response(token: str, url: str = "/") -> RedirectResponse:
    response = RedirectResponse(url=url, status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )
    return response


_TOO_MANY_ATTEMPTS = "Trop de tentatives, réessayez dans quelques minutes"


@app.get("/login", response_class=HTMLResponse)
def login_form():
    return _render_auth_page("login")


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: SASession = Depends(get_db),
):
    """Email + mot de passe d'un compte, ou SITE_USERNAME/SITE_PASSWORD.

    Un seul compteur d'echecs pour les deux: l'administrateur reste aussi
    protege du brute-force qu'avant l'arrivee des comptes.
    """
    client_key = _client_key(request)
    if is_login_rate_limited(client_key):
        return HTMLResponse(_render_auth_page("login", _TOO_MANY_ATTEMPTS), status_code=429)
    if check_credentials(username, password):
        register_successful_login(client_key)
        return _login_response(create_session_token())
    user = authenticate_user(db, username, password) if "@" in username else None
    if user is None:
        register_failed_login(client_key)
        return HTMLResponse(_render_auth_page("login", "Identifiant ou mot de passe incorrect"))
    register_successful_login(client_key)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    return _login_response(token_for_user(user))


@app.get("/register", response_class=HTMLResponse)
def register_form():
    return _render_auth_page("register")


@app.post("/register")
def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: SASession = Depends(get_db),
):
    """Le compteur de /login s'applique aussi ici: sinon la creation de
    compte servirait a enumerer les emails inscrits sans limite."""
    client_key = _client_key(request)
    if is_login_rate_limited(client_key):
        return HTMLResponse(_render_auth_page("register", _TOO_MANY_ATTEMPTS), status_code=429)
    email = normalize_email(username)
    if "@" not in email or len(email) > 320:
        return HTMLResponse(_render_auth_page("register", "Adresse email invalide"), status_code=422)
    problem = password_problem(password)
    if problem:
        return HTMLResponse(_render_auth_page("register", problem), status_code=422)
    if get_user_by_email(db, email) is not None:
        register_failed_login(client_key)
        return HTMLResponse(
            _render_auth_page("register", "Un compte existe déjà avec cet email"), status_code=409
        )
    try:
        user = register_user(db, email, password)
    except IntegrityError:
        # Deux inscriptions simultanees du meme email: la seconde passe le
        # test d'unicite ci-dessus puis echoue sur la contrainte de la
        # table. Sans ce rattrapage, elle sortait en 500.
        db.rollback()
        register_failed_login(client_key)
        return HTMLResponse(
            _render_auth_page("register", "Un compte existe déjà avec cet email"), status_code=409
        )
    return _login_response(token_for_user(user))


@app.get("/auth/google")
async def google_login(request: Request):
    if not _google_configured():
        raise HTTPException(status_code=404, detail="Connexion Google non configurée")
    redirect_uri = str(request.url_for("google_callback"))
    if (settings.cookie_secure or _forwarded_https(request)) and redirect_uri.startswith("http://"):
        # Derriere un tunnel/proxy TLS, l'app voit du http; Google, lui,
        # exige l'URI exacte declaree (https).
        redirect_uri = "https://" + redirect_uri[len("http://"):]
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/google/callback")
async def google_callback(request: Request, db: SASession = Depends(get_db)):
    if not _google_configured():
        raise HTTPException(status_code=404, detail="Connexion Google non configurée")
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as exc:
        log.warning("Echec OAuth Google: %s", exc)
        return HTMLResponse(
            _render_auth_page("login", "La connexion Google a échoué, réessayez"), status_code=400
        )
    info = token.get("userinfo") or {}
    sub, email = info.get("sub"), info.get("email")
    if not sub or not email:
        return HTMLResponse(
            _render_auth_page("login", "Google n'a pas fourni d'adresse email"), status_code=400
        )
    user = get_or_create_google_user(db, sub, email, bool(info.get("email_verified")))
    if user is None:
        return HTMLResponse(
            _render_auth_page(
                "login",
                "Google n'a pas vérifié cette adresse email : "
                "connectez-vous avec votre mot de passe ou créez un compte",
            ),
            status_code=403,
        )
    return _login_response(token_for_user(user))


@app.get("/me")
def me(request: Request, db: SASession = Depends(get_db)):
    """Qui est connecte, pour que le frontend adapte ses menus."""
    info = _session(request)
    if info is None:
        return {"authenticated": False, "premium": False}
    email = settings.site_username if info.is_admin else None
    if not info.is_admin:
        user = db.get(User, info.user_id)
        email = user.email if user else None
    return {"authenticated": True, "premium": info.is_premium, "admin": info.is_admin, "email": email}


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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Demarrage: refus des secrets par defaut, puis creation des tables
    manquantes. Les migrations d'une base existante (colonnes, index) sont
    du ressort de scripts/migrate.py, pas du serveur."""
    _assert_secrets_configured()
    init_db()
    yield


app.router.lifespan_context = lifespan


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
