"""Comptes utilisateurs: inscription, connexion, niveaux d'acces.

Trois niveaux cohabitent sur le meme cookie (src/api/auth.py): visiteur,
compte connecte (role "user"), compte premium/administrateur. Ces tests
fixent ce que chacun obtient -- en particulier qu'un simple compte gratuit
ne recoit ni les pages ni les champs vendus.
"""
from __future__ import annotations

import pytest

import src.api.auth as auth_module
from src.api.auth import (
    ROLE_PREMIUM,
    ROLE_USER,
    SESSION_COOKIE_NAME,
    create_session_token,
    read_session_token,
    token_for_user,
    verify_password,
)
from src.database.models import Listing, User


@pytest.fixture(autouse=True)
def _reset_login_attempts():
    auth_module._failed_attempts.clear()
    yield
    auth_module._failed_attempts.clear()


def _add_listing(session) -> None:
    session.add(
        Listing(
            slug="s", name="S", url="https://www.renthub.in.th/en/s", status="active",
            address="1 Sukhumvit Road", phone="0817322385", province="Bangkok",
        )
    )
    session.commit()


# ── Inscription / connexion ─────────────────────────────────────────

def test_register_creates_a_user_and_logs_in(client, session):
    resp = client.post(
        "/register", data={"username": "Alice@Example.com", "password": "correct-horse"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/123"
    assert SESSION_COOKIE_NAME in resp.headers["set-cookie"]

    user = session.query(User).one()
    assert user.email == "alice@example.com"  # normalise
    assert user.password_hash != "correct-horse"
    assert verify_password("correct-horse", user.password_hash)
    assert user.is_premium is False

    info = read_session_token(client.cookies.get(SESSION_COOKIE_NAME))
    assert info.user_id == user.id and info.role == ROLE_USER


def test_register_rejects_weak_password_and_duplicate_email(client, session):
    short = client.post("/register", data={"username": "a@b.co", "password": "court"})
    assert short.status_code == 422

    assert client.post(
        "/register", data={"username": "a@b.co", "password": "long-enough"}, follow_redirects=False
    ).status_code == 303
    dup = client.post("/register", data={"username": "A@B.CO", "password": "long-enough"})
    assert dup.status_code == 409
    assert session.query(User).count() == 1


def test_login_with_email_and_password(client, session):
    client.post("/register", data={"username": "a@b.co", "password": "long-enough"}, follow_redirects=False)
    client.cookies.clear()

    bad = client.post("/login", data={"username": "a@b.co", "password": "wrong"})
    assert bad.status_code == 200 and "incorrect" in bad.text

    ok = client.post("/login", data={"username": "a@b.co", "password": "long-enough"}, follow_redirects=False)
    assert ok.status_code == 303
    assert read_session_token(client.cookies.get(SESSION_COOKIE_NAME)).role == ROLE_USER


def test_admin_login_still_works_and_is_premium(client):
    from src.config import settings

    ok = client.post(
        "/login",
        data={"username": settings.site_username, "password": settings.site_password},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    info = read_session_token(client.cookies.get(SESSION_COOKIE_NAME))
    assert info.is_admin and info.is_premium


def test_register_shares_the_login_rate_limit(client):
    for _ in range(auth_module._LOGIN_ATTEMPT_MAX):
        client.post("/login", data={"username": "x@y.z", "password": "wrong"})
    blocked = client.post("/register", data={"username": "new@y.z", "password": "long-enough"})
    assert blocked.status_code == 429


# ── Niveaux d'acces ─────────────────────────────────────────────────

def test_free_account_reaches_index_but_not_vip(client, session):
    user = User(email="free@x.io"); session.add(user); session.commit()
    client.cookies.set(SESSION_COOKIE_NAME, token_for_user(user))

    assert client.get("/", follow_redirects=False).status_code == 200
    vip = client.get("/vip.html", follow_redirects=False)
    assert vip.status_code in (302, 307)
    assert vip.headers["location"] == "/premium.html"
    # Meme regle via le montage statique.
    assert client.get("/static/vip.html", follow_redirects=False).headers["location"] == "/premium.html"


def test_premium_account_reaches_vip(client, session):
    user = User(email="vip@x.io", is_premium=True); session.add(user); session.commit()
    client.cookies.set(SESSION_COOKIE_NAME, token_for_user(user))
    assert read_session_token(client.cookies.get(SESSION_COOKIE_NAME)).role == ROLE_PREMIUM
    assert client.get("/vip.html", follow_redirects=False).status_code == 200


def test_dashboard_123_requires_premium(client, session):
    """`/123` (le tableau de bord, PROTECTED_PATHS["/123"] = True dans
    security.py): un visiteur anonyme est renvoye au login, un compte
    gratuit est renvoye vers l'offre premium (meme regle que /vip.html)."""
    anon = client.get("/123", follow_redirects=False)
    assert anon.status_code in (302, 307)
    assert anon.headers["location"] == "/login"

    free = User(email="free@x.io"); session.add(free); session.commit()
    client.cookies.set(SESSION_COOKIE_NAME, token_for_user(free))
    dashboard = client.get("/123", follow_redirects=False)
    assert dashboard.status_code in (302, 307)
    assert dashboard.headers["location"] == "/premium.html"

    premium = User(email="premium@x.io", is_premium=True); session.add(premium); session.commit()
    client.cookies.set(SESSION_COOKIE_NAME, token_for_user(premium))
    assert client.get("/123", follow_redirects=False).status_code == 200


def test_free_account_gets_the_public_view_of_listings(client, session):
    """Un compte gratuit voit ce que voit un visiteur: la vente porte sur
    les contacts, l'adresse et le lien source."""
    _add_listing(session)
    user = User(email="free@x.io"); session.add(user); session.commit()
    client.cookies.set(SESSION_COOKIE_NAME, token_for_user(user))
    listing = client.get("/listings").json()[0]
    assert listing["address"] is None and listing["url"] is None and listing["phone"] is None

    client.cookies.set(SESSION_COOKIE_NAME, create_session_token(user.id, ROLE_PREMIUM))
    listing = client.get("/listings").json()[0]
    assert listing["address"] == "1 Sukhumvit Road" and listing["phone"] == "0817322385"


def test_me_reports_who_is_connected(client, session):
    assert client.get("/me").json() == {"authenticated": False, "premium": False}
    user = User(email="free@x.io"); session.add(user); session.commit()
    client.cookies.set(SESSION_COOKIE_NAME, token_for_user(user))
    body = client.get("/me").json()
    assert body["email"] == "free@x.io" and body["premium"] is False


def test_tampered_role_is_rejected():
    token = create_session_token(42, ROLE_USER)
    uid, role, expiry, digest = token.split(".")
    assert read_session_token(f"{uid}.{ROLE_PREMIUM}.{expiry}.{digest}") is None


def test_google_login_is_404_when_not_configured(client, monkeypatch):
    # Ne depend pas du .env local: on force l'absence de configuration Google.
    from src.config import settings

    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")
    assert client.get("/auth/google", follow_redirects=False).status_code == 404
    assert "Google" not in client.get("/login").text
