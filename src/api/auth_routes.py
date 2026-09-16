"""Comptes: page de connexion/inscription, OAuth Google, /me, /logout.

Un seul compteur d'echecs pour le compte administrateur (SITE_USERNAME) et
les comptes email: voir src/api/auth.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SASession

from src.api import auth as auth_module
from src.api.auth import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    authenticate_user,
    check_credentials,
    create_session_token,
    get_or_create_google_user,
    get_user_by_email,
    is_login_rate_limited,
    normalize_email,
    password_problem,
    register_failed_login,
    register_successful_login,
    register_user,
    token_for_user,
)
from src.api.deps import get_db
from src.api.security import _client_key, _forwarded_https, _session
from src.config import settings
from src.database.models import User

log = logging.getLogger(__name__)
router = APIRouter()


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


def _login_response(token: str, url: str = "/123") -> RedirectResponse:
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


@router.get("/login", response_class=HTMLResponse)
def login_form():
    return _render_auth_page("login")


@router.post("/login")
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


@router.get("/register", response_class=HTMLResponse)
def register_form():
    return _render_auth_page("register")


@router.post("/register")
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


@router.get("/auth/google")
async def google_login(request: Request):
    if not _google_configured():
        raise HTTPException(status_code=404, detail="Connexion Google non configurée")
    redirect_uri = str(request.url_for("google_callback"))
    if (settings.cookie_secure or _forwarded_https(request)) and redirect_uri.startswith("http://"):
        # Derriere un tunnel/proxy TLS, l'app voit du http; Google, lui,
        # exige l'URI exacte declaree (https).
        redirect_uri = "https://" + redirect_uri[len("http://"):]
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/google/callback")
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


@router.get("/me")
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


@router.post("/logout")
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
