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


def _sign(expiry: int) -> str:
    payload = str(expiry)
    digest = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{digest}"


def create_session_token() -> str:
    expiry = int(time.time()) + SESSION_MAX_AGE_SECONDS
    return _sign(expiry)


def verify_session_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    payload, _, digest = token.partition(".")
    expected = _sign(int(payload)) if payload.isdigit() else None
    if expected is None or not hmac.compare_digest(expected, token):
        return False
    return int(payload) >= int(time.time())


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
