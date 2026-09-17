"""Middlewares et helpers de securite de l'API: en-tetes (CSP), gate
d'authentification par page, plafond de debit, identite du client derriere
un proxy, lecture du cookie de session."""
from __future__ import annotations

import posixpath

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.api import rate_limit
from src.api.auth import SESSION_COOKIE_NAME, SessionInfo, read_session_token
from src.config import settings

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
            max_age = _CODE_MAX_AGE if request.url.path.endswith((".js", ".css")) else _STATIC_MAX_AGE
            response.headers.setdefault("Cache-Control", f"public, max-age={max_age}")
        return response


# Un jour pour les images et les JSON de coordonnees: assez pour ne pas
# retelecharger 3 MB d'images a chaque visite, assez court pour qu'un
# deploiement soit visible le lendemain sans purge. Cinq minutes pour le
# JS/CSS partage (frontend/js/): une page fraichement deployee ne doit pas
# tourner une journee avec un script de la version precedente.
_STATIC_MAX_AGE = 24 * 3600
_CODE_MAX_AGE = 5 * 60


# Seules ces pages exigent une session; tout le reste (autres pages, API,
# assets) est accessible sans login. index.html est le nom de fichier
# derriere "/", il faut donc le proteger aussi sous /static. La valeur dit
# le niveau requis: True = compte premium (ou admin), False = n'importe
# quel compte connecte.
PROTECTED_PATHS: dict[str, bool] = {}

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
    "/api/questionnaire",
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
