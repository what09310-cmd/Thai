"""
Authentification: comptes utilisateurs (email + mot de passe, Google) et
compte administrateur du site (SITE_USERNAME/SITE_PASSWORD), tous portes
par le meme cookie de session signe.

Le jeton est `<user_id>.<role>.<expiry>.<hmac>`. Il se verifie sans base de
donnees (AuthMiddleware n'a pas de session SQLAlchemy sous la main), ce qui
a une consequence: un passage en premium ne prend effet qu'a la prochaine
connexion, sauf si la route qui le realise re-emet le cookie.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import bcrypt
from sqlalchemy.orm import Session as SASession

from src.config import settings
from src.database.models import User

SESSION_COOKIE_NAME = "session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 jours

# Le compte administrateur (SITE_USERNAME/SITE_PASSWORD) n'a pas de ligne
# dans `users`: il vit dans .env et porte l'identifiant 0.
ADMIN_USER_ID = 0

ROLE_USER = "user"
ROLE_PREMIUM = "premium"
ROLE_ADMIN = "admin"
_ROLES = (ROLE_USER, ROLE_PREMIUM, ROLE_ADMIN)

# bcrypt ignore silencieusement tout au-dela de 72 octets: on refuse plutot
# que de laisser croire que la fin du mot de passe compte.
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_BYTES = 72

# — Rate limiting sur /login —
# Un seul couple identifiant/mot de passe partagé: sans throttling, il est
# brute-forçable à la vitesse du réseau. Compteur en mémoire (process
# unique), suffisant pour ce déploiement à un seul worker.
_LOGIN_ATTEMPT_WINDOW_SECONDS = 15 * 60
_LOGIN_ATTEMPT_MAX = 5
# Plafond du nombre d'IP suivies simultanement. Le compteur n'etait purge
# que pour une IP qui revenait: en faisant tourner l'adresse source, on
# faisait croitre ce dictionnaire sans limite.
_TRACKED_CLIENTS_MAX = 10_000

_failed_attempts: dict[str, list[float]] = {}


def _prune_all(now: float) -> None:
    """Retire toutes les entrees expirees, pas seulement celle qu'on consulte."""
    cutoff = now - _LOGIN_ATTEMPT_WINDOW_SECONDS
    for key in [k for k, v in _failed_attempts.items() if not v or v[-1] < cutoff]:
        _failed_attempts.pop(key, None)


def _prune(client_key: str, now: float) -> list[float]:
    cutoff = now - _LOGIN_ATTEMPT_WINDOW_SECONDS
    attempts = [t for t in _failed_attempts.get(client_key, []) if t >= cutoff]
    if attempts:
        _failed_attempts[client_key] = attempts
    else:
        _failed_attempts.pop(client_key, None)
    return attempts


def is_login_rate_limited(client_key: str) -> bool:
    return len(_prune(client_key, time.time())) >= _LOGIN_ATTEMPT_MAX


def register_failed_login(client_key: str) -> None:
    now = time.time()
    attempts = _prune(client_key, now)
    attempts.append(now)
    _failed_attempts[client_key] = attempts
    if len(_failed_attempts) > _TRACKED_CLIENTS_MAX:
        _prune_all(now)


def register_successful_login(client_key: str) -> None:
    _failed_attempts.pop(client_key, None)


def _signing_key() -> bytes:
    """Cle de signature liee au mot de passe courant.

    Le jeton ne porte que son expiration: sans ce liage, changer
    SITE_PASSWORD (typiquement apres une fuite) laissait valides pendant
    sept jours tous les cookies deja emis. Faire entrer le mot de passe dans
    la cle les invalide tous des le redemarrage.
    """
    return hashlib.sha256(
        settings.secret_key.encode() + b"|" + settings.site_password.encode()
    ).digest()


@dataclass(frozen=True)
class SessionInfo:
    user_id: int
    role: str
    expiry: int

    @property
    def is_premium(self) -> bool:
        return self.role in (ROLE_PREMIUM, ROLE_ADMIN)

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


def _sign(user_id: int, role: str, expiry: int) -> str:
    payload = f"{user_id}.{role}.{expiry}"
    digest = hmac.new(_signing_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{digest}"


def create_session_token(user_id: int = ADMIN_USER_ID, role: str = ROLE_ADMIN) -> str:
    """Sans argument: jeton du compte administrateur (SITE_USERNAME)."""
    if role not in _ROLES:
        raise ValueError(f"role inconnu: {role!r}")
    expiry = int(time.time()) + SESSION_MAX_AGE_SECONDS
    return _sign(user_id, role, expiry)


def token_for_user(user: User) -> str:
    return create_session_token(user.id, ROLE_PREMIUM if user.is_premium else ROLE_USER)


# Au-dela, int(payload) leve ValueError (limite int<->str de Python 3.11+),
# ce qui remontait en 500 sur toute requete portant un tel cookie. Une
# expiration (ou un identifiant) tient largement dans 20 chiffres.
_MAX_EXPIRY_DIGITS = 20


def read_session_token(token: str | None) -> SessionInfo | None:
    """Decode et verifie le jeton; None s'il est absent, altere ou expire."""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 4:
        return None
    uid_s, role, expiry_s, _digest = parts
    if role not in _ROLES:
        return None
    for number in (uid_s, expiry_s):
        if not number.isdigit() or len(number) > _MAX_EXPIRY_DIGITS:
            return None
    try:
        user_id, expiry = int(uid_s), int(expiry_s)
    except ValueError:
        return None
    if not hmac.compare_digest(_sign(user_id, role, expiry), token):
        return None
    if expiry < int(time.time()):
        return None
    return SessionInfo(user_id=user_id, role=role, expiry=expiry)


def check_credentials(username: str, password: str) -> bool:
    # compare_digest exige des bytes: sur des str, il leve TypeError des
    # qu'un caractere non-ASCII est saisi (mot de passe accentue).
    valid_username = hmac.compare_digest(
        username.encode("utf-8"), settings.site_username.encode("utf-8")
    )
    valid_password = hmac.compare_digest(
        password.encode("utf-8"), settings.site_password.encode("utf-8")
    )
    return valid_username and valid_password


# — Comptes utilisateurs —

def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        return False


def password_problem(password: str) -> str | None:
    """Message d'erreur si le mot de passe est irrecevable, sinon None."""
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Le mot de passe doit faire au moins {PASSWORD_MIN_LENGTH} caractères"
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        return "Le mot de passe est trop long"
    return None


def get_user_by_email(db: SASession, email: str) -> User | None:
    return db.query(User).filter(User.email == normalize_email(email)).first()


def register_user(db: SASession, email: str, password: str) -> User:
    """Cree le compte; l'appelant a deja verifie l'unicite et le mot de passe."""
    user = User(email=normalize_email(email), password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: SASession, email: str, password: str) -> User | None:
    user = get_user_by_email(db, email)
    if user is None:
        # Meme cout qu'une verification reelle, pour ne pas reveler par le
        # temps de reponse quels emails ont un compte.
        bcrypt.checkpw(b"x", _DUMMY_HASH)
        return None
    return user if verify_password(password, user.password_hash) else None


_DUMMY_HASH = bcrypt.hashpw(b"dummy", bcrypt.gensalt())


def get_or_create_google_user(
    db: SASession, sub: str, email: str, email_verified: bool
) -> User | None:
    """Retrouve le compte par `sub`, sinon par email (verifie), sinon le cree.

    Rattacher par email un compte existant suppose que Google atteste de
    l'adresse: sans `email_verified`, n'importe quel compte Google portant
    l'email d'un utilisateur s'emparerait de son compte.

    Un email non verifie ne sert pas non plus a *creer* un compte: `email`
    est unique dans `users`, donc l'insertion echouait en IntegrityError
    (un 500) des qu'un compte a mot de passe portait deja l'adresse, et
    quand elle passait, elle reservait l'adresse a quelqu'un qui n'en a
    pas prouve la propriete -- son vrai proprietaire ne pouvait plus
    s'inscrire, et un passage en premium "par email" lui aurait profite.
    Rend None dans ce cas: l'appelant invite a se connecter autrement.
    """
    user = db.query(User).filter(User.google_sub == sub).first()
    if user is None:
        if not email_verified:
            return None
        user = get_user_by_email(db, email)
        if user is not None:
            user.google_sub = sub
    if user is None:
        user = User(email=normalize_email(email), google_sub=sub)
        db.add(user)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)
    return user
