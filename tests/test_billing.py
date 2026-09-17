"""Abonnements Stripe: session Checkout, webhook, re-emission du cookie.

`stripe` n'est jamais appele en reseau ici (voir .claude/rules/testing.md):
`stripe.checkout.Session.create/retrieve`, `stripe.Subscription.retrieve` et
`stripe.Webhook.construct_event` sont remplaces par des doublures.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import src.api.billing_routes as billing_routes
from src.api.auth import SESSION_COOKIE_NAME, read_session_token
from src.config import settings
from src.database.models import Subscription, User


@pytest.fixture(autouse=True)
def _stripe_configured(monkeypatch):
    """Cles Stripe factices: sans elles, les routes repondent 404 (voir
    `_stripe_configured`), ce que teste explicitement le premier test."""
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_fake")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_fake")
    monkeypatch.setattr(settings, "stripe_price_flex", "price_flex")
    monkeypatch.setattr(settings, "stripe_price_essentiel", "price_essentiel")
    monkeypatch.setattr(settings, "stripe_price_serenite", "price_serenite")
    yield


def _register(client) -> None:
    client.post("/register", data={"username": "a@b.co", "password": "long-enough"}, follow_redirects=False)


def _fake_subscription(user_id: int = 1, status: str = "active") -> dict:
    return {
        "id": "sub_123",
        "customer": "cus_123",
        "status": status,
        "current_period_end": int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()),
    }


# ── POST /api/checkout/create-session ──────────────────────────────

def test_create_session_404_when_stripe_not_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "stripe_price_flex", "")
    _register(client)
    resp = client.post("/api/checkout/create-session", json={"plan": "flex"})
    assert resp.status_code == 404


def test_create_session_requires_login(client):
    resp = client.post("/api/checkout/create-session", json={"plan": "flex"})
    assert resp.status_code == 401


def test_create_session_rejects_unknown_plan(client):
    _register(client)
    resp = client.post("/api/checkout/create-session", json={"plan": "gold"})
    assert resp.status_code == 400


def test_create_session_returns_checkout_url(client, session, monkeypatch):
    _register(client)
    user = session.query(User).one()

    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(url="https://checkout.stripe.com/pay/cs_test_123")

    monkeypatch.setattr(billing_routes.stripe.checkout.Session, "create", fake_create)

    resp = client.post("/api/checkout/create-session", json={"plan": "essentiel"})
    assert resp.status_code == 200
    assert resp.json() == {"url": "https://checkout.stripe.com/pay/cs_test_123"}
    assert captured["client_reference_id"] == str(user.id)
    assert captured["customer_email"] == user.email
    assert captured["line_items"][0]["price"] == "price_essentiel"
    assert captured["mode"] == "subscription"


def test_create_session_surfaces_stripe_errors_as_500(client, monkeypatch):
    _register(client)

    def fake_create(**kwargs):
        raise billing_routes.stripe.error.StripeError("boom")

    monkeypatch.setattr(billing_routes.stripe.checkout.Session, "create", fake_create)

    resp = client.post("/api/checkout/create-session", json={"plan": "flex"})
    assert resp.status_code == 500


# ── GET /billing/success ────────────────────────────────────────────

def test_billing_success_activates_premium_and_reissues_cookie(client, session, monkeypatch):
    _register(client)
    user = session.query(User).one()
    assert user.is_premium is False

    def fake_retrieve(session_id, expand=None):
        return {
            "payment_status": "paid",
            "client_reference_id": str(user.id),
            "subscription": _fake_subscription(user.id),
        }

    monkeypatch.setattr(billing_routes.stripe.checkout.Session, "retrieve", fake_retrieve)

    resp = client.get("/billing/success", params={"session_id": "cs_test_123"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/vip.html"

    session.refresh(user)
    assert user.is_premium is True
    assert session.query(Subscription).one().id == "sub_123"

    # Le cookie re-emis porte deja le role premium, sans nouvelle connexion.
    new_cookie = resp.headers["set-cookie"]
    token = new_cookie.split(f"{SESSION_COOKIE_NAME}=")[1].split(";")[0]
    info = read_session_token(token)
    assert info.is_premium is True


def test_billing_success_redirects_without_touching_db_when_unpaid(client, session, monkeypatch):
    _register(client)
    user = session.query(User).one()

    monkeypatch.setattr(
        billing_routes.stripe.checkout.Session,
        "retrieve",
        lambda session_id, expand=None: {"payment_status": "unpaid", "client_reference_id": str(user.id)},
    )

    resp = client.get("/billing/success", params={"session_id": "cs_test_123"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/premium.html"
    session.refresh(user)
    assert user.is_premium is False


def test_billing_success_redirects_when_session_id_missing(client):
    resp = client.get("/billing/success", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/premium.html"


# ── POST /api/webhooks/stripe ───────────────────────────────────────

def test_webhook_rejects_bad_signature(client, monkeypatch):
    def fake_construct_event(payload, sig_header, secret):
        raise billing_routes.stripe.error.SignatureVerificationError("bad sig", sig_header)

    monkeypatch.setattr(billing_routes.stripe.Webhook, "construct_event", fake_construct_event)

    resp = client.post("/api/webhooks/stripe", content=b"{}", headers={"stripe-signature": "t=1,v1=bad"})
    assert resp.status_code == 400


def test_webhook_checkout_completed_activates_premium(client, session, monkeypatch):
    _register(client)
    user = session.query(User).one()

    event = {
        "type": "checkout.session.completed",
        "data": {"object": {"client_reference_id": str(user.id), "subscription": "sub_123"}},
    }
    monkeypatch.setattr(billing_routes.stripe.Webhook, "construct_event", lambda *a, **k: event)
    monkeypatch.setattr(billing_routes.stripe.Subscription, "retrieve", lambda sub_id: _fake_subscription(user.id))

    resp = client.post("/api/webhooks/stripe", content=b"{}", headers={"stripe-signature": "t=1,v1=ok"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "success"}

    session.refresh(user)
    assert user.is_premium is True


def test_webhook_subscription_deleted_revokes_premium(client, session, monkeypatch):
    _register(client)
    user = session.query(User).one()
    session.add(Subscription(
        id="sub_123", user_id=user.id, stripe_customer_id="cus_123",
        plan_name="flex", status="active",
        current_period_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
    ))
    user.is_premium = True
    session.commit()

    event = {"type": "customer.subscription.deleted", "data": {"object": {"id": "sub_123"}}}
    monkeypatch.setattr(billing_routes.stripe.Webhook, "construct_event", lambda *a, **k: event)

    resp = client.post("/api/webhooks/stripe", content=b"{}", headers={"stripe-signature": "t=1,v1=ok"})
    assert resp.status_code == 200

    session.refresh(user)
    assert user.is_premium is False
    assert session.query(Subscription).one().status == "canceled"


def test_webhook_payment_failed_revokes_premium(client, session, monkeypatch):
    _register(client)
    user = session.query(User).one()
    session.add(Subscription(
        id="sub_123", user_id=user.id, stripe_customer_id="cus_123",
        plan_name="flex", status="active",
        current_period_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
    ))
    user.is_premium = True
    session.commit()

    event = {"type": "invoice.payment_failed", "data": {"object": {"customer": "cus_123"}}}
    monkeypatch.setattr(billing_routes.stripe.Webhook, "construct_event", lambda *a, **k: event)

    resp = client.post("/api/webhooks/stripe", content=b"{}", headers={"stripe-signature": "t=1,v1=ok"})
    assert resp.status_code == 200

    session.refresh(user)
    assert user.is_premium is False
    assert session.query(Subscription).one().status == "past_due"


def test_webhook_404_when_stripe_not_configured(client, monkeypatch):
    # `_stripe_configured()` ne verifie pas stripe_webhook_secret (utile
    # seulement a la verification de signature) mais les cles/prix: c'est
    # l'absence de ceux-ci qui doit rendre la route indisponible.
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    resp = client.post("/api/webhooks/stripe", content=b"{}", headers={"stripe-signature": "t=1,v1=ok"})
    assert resp.status_code == 404
