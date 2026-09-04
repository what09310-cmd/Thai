"""
Authentification par mot de passe unique + cookie de session signé.
"""
from __future__ import annotations

import hashlib
import hmac
import time

from src.config import settings

SESSION_COOKIE_NAME = "session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 jours

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


def _sign(expiry: int) -> str:
    payload = str(expiry)
    digest = hmac.new(_signing_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{digest}"


def create_session_token() -> str:
    expiry = int(time.time()) + SESSION_MAX_AGE_SECONDS
    return _sign(expiry)


# Au-dela, int(payload) leve ValueError (limite int<->str de Python 3.11+),
# ce qui remontait en 500 sur toute requete portant un tel cookie. Une
# expiration tient largement dans 20 chiffres.
_MAX_EXPIRY_DIGITS = 20


def verify_session_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    payload, _, _digest = token.partition(".")
    if not payload.isdigit() or len(payload) > _MAX_EXPIRY_DIGITS:
        return False
    try:
        expiry = int(payload)
    except ValueError:
        return False
    if not hmac.compare_digest(_sign(expiry), token):
        return False
    return expiry >= int(time.time())


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
