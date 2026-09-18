"""Comptes: page de connexion/inscription, OAuth Google, /me, /logout.

Un seul compteur d'echecs pour le compte administrateur (SITE_USERNAME) et
les comptes email: voir src/api/auth.py.
"""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape as _html_escape

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
    create_password_reset_token,
    create_session_token,
    get_or_create_google_user,
    get_user_by_email,
    get_user_by_reset_token,
    is_login_rate_limited,
    normalize_email,
    password_problem,
    register_failed_login,
    register_successful_login,
    register_user,
    reset_password,
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
      <input type="hidden" name="next" value="{next_url}">
      <label for="username">Email{admin_hint}</label>
      <input type="text" id="username" name="username" autofocus required autocomplete="{username_autocomplete}">
      <label for="password">Mot de passe</label>
      <input type="password" id="password" name="password" required autocomplete="{password_autocomplete}" minlength="{min_length}">
      {forgot_link}
      <button type="submit">{submit}</button>
    </form>
    {google}
    <div class="alt">{alt}</div>
  </div>
</body>
</html>"""

_FORGOT_LINK = '<div class="alt" style="margin-top:0.5rem;text-align:right;"><a href="/forgot-password">Mot de passe oublié ?</a></div>'

_MESSAGE_PAGE = """<!doctype html>
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
  button {{ display: block; width: 100%; padding: 0.6rem; border: none; border-radius: 4px;
            margin-top: 1rem; background: #3b82f6; color: white; cursor: pointer; font-weight: 600;
            text-align: center; box-sizing: border-box; font-size: 1rem; }}
  .alt {{ margin-top: 1.25rem; font-size: 0.9rem; color: #94a3b8; text-align: center; }}
  .alt a {{ color: #93c5fd; }}
  .error {{ color: #f87171; margin-bottom: 0.5rem; }}
  .info {{ color: #86efac; margin-bottom: 0.5rem; }}
  p.hint {{ color: #94a3b8; font-size: 0.85rem; margin-top: 0; }}
</style>
</head>
<body>
  <div class="card">
    <h2>{title}</h2>
    {hint}
    {message}
    {form}
    <div class="alt">{alt}</div>
  </div>
</body>
</html>"""

_FORGOT_FORM = """<form method="post" action="/forgot-password">
      <label for="username">Email</label>
      <input type="text" id="username" name="username" autofocus required autocomplete="email">
      <button type="submit">Envoyer le lien</button>
    </form>"""

_RESET_FORM = """<form method="post" action="/reset-password">
      <input type="hidden" name="token" value="{token}">
      <label for="password">Nouveau mot de passe</label>
      <input type="password" id="password" name="password" autofocus required autocomplete="new-password" minlength="{min_length}">
      <button type="submit">Changer le mot de passe</button>
    </form>"""


def _render_forgot_page(message: str = "") -> str:
    return _MESSAGE_PAGE.format(
        title="Mot de passe oublié",
        hint='<p class="hint">Indiquez l\'email de votre compte, un lien de réinitialisation vous sera envoyé.</p>',
        message=message, form=_FORGOT_FORM,
        alt='<a href="/login">Retour à la connexion</a>',
    )


def _render_reset_page(token: str, message: str = "") -> str:
    return _MESSAGE_PAGE.format(
        title="Nouveau mot de passe", hint="", message=message,
        form=_RESET_FORM.format(token=token, min_length=auth_module.PASSWORD_MIN_LENGTH),
        alt='<a href="/login">Retour à la connexion</a>',
    )


def _render_reset_invalid_page() -> str:
    return _MESSAGE_PAGE.format(
        title="Lien invalide",
        hint='<div class="error">Ce lien de réinitialisation est invalide ou a expiré.</div>',
        message="", form="",
        alt='<a href="/forgot-password">Demander un nouveau lien</a>',
    )


def _send_reset_email(email: str, reset_url: str) -> None:
    """Envoie le lien par SMTP; sans SMTP_HOST configuré, le lien part dans
    les logs (utile en dev local, meme repli que Google/Stripe quand leurs
    identifiants sont vides)."""
    if not settings.smtp_host:
        log.warning("SMTP non configuré, lien de réinitialisation pour %s: %s", email, reset_url)
        return
    message = EmailMessage()
    message["Subject"] = "Réinitialisation de votre mot de passe ThaiMonth"
    message["From"] = settings.smtp_from
    message["To"] = email
    message.set_content(
        "Vous avez demandé la réinitialisation de votre mot de passe ThaiMonth.\n\n"
        f"Ce lien est valable une heure :\n{reset_url}\n\n"
        "Si vous n'êtes pas à l'origine de cette demande, ignorez cet email."
    )
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
    except OSError:
        log.exception("Echec d'envoi de l'email de reinitialisation à %s", email)


def _google_configured() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def _safe_next(next_url: str) -> str:
    """Cantonne `next` a un chemin relatif du site: un `next` absolu ou
    protocol-relative (`//evil.com`) transformerait la connexion en
    redirection ouverte."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return ""


def _render_auth_page(mode: str, error: str = "", next_url: str = "") -> str:
    err = f'<div class="error">{error}</div>' if error else ""
    next_url = _safe_next(next_url)
    next_qs = f"?next={_html_escape(next_url)}" if next_url else ""
    google = (
        f'<p class="sep">ou</p><a class="google" href="/auth/google{next_qs}">Continuer avec Google</a>'
        if _google_configured() else ""
    )
    next_attr = _html_escape(next_url)
    if mode == "register":
        return _AUTH_PAGE.format(
            title="Créer un compte", error=err, action="/register", admin_hint="", next_url=next_attr,
            username_autocomplete="email", password_autocomplete="new-password",
            min_length=auth_module.PASSWORD_MIN_LENGTH, submit="Créer mon compte", google=google,
            forgot_link="", alt=f'Déjà un compte ? <a href="/login{next_qs}">Se connecter</a>',
        )
    return _AUTH_PAGE.format(
        title="Connexion", error=err, action="/login", admin_hint=" ou identifiant", next_url=next_attr,
        username_autocomplete="username", password_autocomplete="current-password",
        min_length=1, submit="Entrer", google=google, forgot_link=_FORGOT_LINK,
        alt=f'Pas encore de compte ? <a href="/register{next_qs}">Créer un compte</a>',
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
def login_form(next: str = ""):
    return _render_auth_page("login", next_url=next)


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
    db: SASession = Depends(get_db),
):
    """Email + mot de passe d'un compte, ou SITE_USERNAME/SITE_PASSWORD.

    Un seul compteur d'echecs pour les deux: l'administrateur reste aussi
    protege du brute-force qu'avant l'arrivee des comptes.
    """
    client_key = _client_key(request)
    dest = _safe_next(next) or "/123"
    if is_login_rate_limited(client_key):
        return HTMLResponse(_render_auth_page("login", _TOO_MANY_ATTEMPTS, next), status_code=429)
    if check_credentials(username, password):
        register_successful_login(client_key)
        return _login_response(create_session_token(), url=dest)
    user = authenticate_user(db, username, password) if "@" in username else None
    if user is None:
        register_failed_login(client_key)
        return HTMLResponse(_render_auth_page("login", "Identifiant ou mot de passe incorrect", next))
    register_successful_login(client_key)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    return _login_response(token_for_user(user), url=dest)


@router.get("/register", response_class=HTMLResponse)
def register_form(next: str = ""):
    return _render_auth_page("register", next_url=next)


@router.post("/register")
def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
    db: SASession = Depends(get_db),
):
    """Le compteur de /login s'applique aussi ici: sinon la creation de
    compte servirait a enumerer les emails inscrits sans limite."""
    client_key = _client_key(request)
    dest = _safe_next(next) or "/123"
    if is_login_rate_limited(client_key):
        return HTMLResponse(_render_auth_page("register", _TOO_MANY_ATTEMPTS, next), status_code=429)
    email = normalize_email(username)
    if "@" not in email or len(email) > 320:
        return HTMLResponse(_render_auth_page("register", "Adresse email invalide", next), status_code=422)
    problem = password_problem(password)
    if problem:
        return HTMLResponse(_render_auth_page("register", problem, next), status_code=422)
    if get_user_by_email(db, email) is not None:
        register_failed_login(client_key)
        return HTMLResponse(
            _render_auth_page("register", "Un compte existe déjà avec cet email", next), status_code=409
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
            _render_auth_page("register", "Un compte existe déjà avec cet email", next), status_code=409
        )
    return _login_response(token_for_user(user), url=dest)


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_form():
    return _render_forgot_page()


@router.post("/forgot-password")
def forgot_password_submit(
    request: Request,
    username: str = Form(...),
    db: SASession = Depends(get_db),
):
    """Meme reponse, compte trouve ou non: sinon la page enumere les emails
    inscrits. Partage le compteur de /login pour ne pas devenir un moyen
    illimite de spammer un email de reinitialisation."""
    client_key = _client_key(request)
    if is_login_rate_limited(client_key):
        return HTMLResponse(
            _render_forgot_page(f'<div class="error">{_TOO_MANY_ATTEMPTS}</div>'), status_code=429
        )
    register_failed_login(client_key)
    email = normalize_email(username)
    user = get_user_by_email(db, email) if "@" in email else None
    if user is not None and user.password_hash is not None:
        token = create_password_reset_token(db, user)
        reset_url = str(request.url_for("reset_password_form")) + f"?token={token}"
        _send_reset_email(user.email, reset_url)
    message = (
        '<div class="info">Si un compte existe avec cet email, un lien de '
        "réinitialisation vient de lui être envoyé.</div>"
    )
    return HTMLResponse(_render_forgot_page(message))


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_form(token: str = "", db: SASession = Depends(get_db)):
    if get_user_by_reset_token(db, token) is None:
        return HTMLResponse(_render_reset_invalid_page(), status_code=400)
    return _render_reset_page(token)


@router.post("/reset-password")
def reset_password_submit(
    token: str = Form(...),
    password: str = Form(...),
    db: SASession = Depends(get_db),
):
    user = get_user_by_reset_token(db, token)
    if user is None:
        return HTMLResponse(_render_reset_invalid_page(), status_code=400)
    problem = password_problem(password)
    if problem:
        return HTMLResponse(
            _render_reset_page(token, f'<div class="error">{problem}</div>'), status_code=422
        )
    reset_password(db, user, password)
    return _login_response(token_for_user(user))


@router.get("/auth/google")
async def google_login(request: Request, next: str = "", billing_session_id: str = ""):
    if not _google_configured():
        raise HTTPException(status_code=404, detail="Connexion Google non configurée")
    # Porte par la session `oauth_state` (SessionMiddleware) le temps de
    # l'aller-retour chez Google, la redirect_uri ne pouvant pas embarquer
    # de parametre supplementaire sans que Google la rejette.
    request.session["post_login_next"] = _safe_next(next)
    request.session["billing_session_id"] = billing_session_id
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
    billing_session_id = request.session.pop("billing_session_id", "")
    if billing_session_id:
        from src.api import billing_routes
        return billing_routes.attach_subscription_after_google(db, billing_session_id, user)
    dest = request.session.pop("post_login_next", "") or "/123"
    return _login_response(token_for_user(user), url=dest)


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
