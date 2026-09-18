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


def test_subscription_user_id_is_nullable(session):
    """Le webhook peut enregistrer un paiement avant la creation du compte."""
    session.add(Subscription(
        id="sub_anon", user_id=None, stripe_customer_id="cus_anon",
        plan_name="flex", status="active",
        current_period_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
    ))
    session.commit()
    assert session.get(Subscription, "sub_anon").user_id is None


# ── POST /api/checkout/create-session ──────────────────────────────

def test_create_session_404_when_stripe_not_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "stripe_price_flex", "")
    _register(client)
    resp = client.post("/api/checkout/create-session", json={"plan": "flex"})
    assert resp.status_code == 404


def test_create_session_allows_anonymous_checkout(client, monkeypatch):
    """Stripe collecte lui-meme l'email quand aucun compte n'est connecte."""
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(url="https://checkout.stripe.com/pay/cs_test_anon")

    monkeypatch.setattr(billing_routes.stripe.checkout.Session, "create", fake_create)
    resp = client.post("/api/checkout/create-session", json={"plan": "flex"})
    assert resp.status_code == 200
    assert "client_reference_id" not in captured
    assert "customer_email" not in captured
    # En mode subscription, Stripe cree deja le Customer et refuse le
    # parametre customer_creation (reserve aux modes payment et setup).
    assert "customer_creation" not in captured


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
        "id": "evt_1",
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


def test_webhook_checkout_completed_without_reference_creates_unlinked_subscription(client, session, monkeypatch):
    """Paiement anonyme: le webhook cree la ligne Subscription avec
    user_id=None, sans lever d'erreur sur l'absence de client_reference_id."""
    event = {
        "id": "evt_2",
        "type": "checkout.session.completed",
        "data": {"object": {"client_reference_id": None, "subscription": "sub_anon", "customer": "cus_anon"}},
    }
    monkeypatch.setattr(billing_routes.stripe.Webhook, "construct_event", lambda *a, **k: event)
    monkeypatch.setattr(
        billing_routes.stripe.Subscription, "retrieve",
        lambda sub_id: {**_fake_subscription(), "id": "sub_anon", "customer": "cus_anon"},
    )

    resp = client.post("/api/webhooks/stripe", content=b"{}", headers={"stripe-signature": "t=1,v1=ok"})
    assert resp.status_code == 200

    session.expire_all()
    row = session.query(Subscription).filter(Subscription.id == "sub_anon").one()
    assert row.user_id is None
    assert row.stripe_customer_id == "cus_anon"


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

    event = {"id": "evt_3", "type": "customer.subscription.deleted", "data": {"object": {"id": "sub_123"}}}
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

    event = {"id": "evt_4", "type": "invoice.payment_failed", "data": {"object": {"customer": "cus_123"}}}
    monkeypatch.setattr(billing_routes.stripe.Webhook, "construct_event", lambda *a, **k: event)

    resp = client.post("/api/webhooks/stripe", content=b"{}", headers={"stripe-signature": "t=1,v1=ok"})
    assert resp.status_code == 200

    session.refresh(user)
    assert user.is_premium is False
    assert session.query(Subscription).one().status == "past_due"


def test_webhook_charge_refunded_revokes_premium(client, session, monkeypatch):
    """Un remboursement (dashboard ou API) n'annule pas forcement
    l'abonnement Stripe: `is_premium` doit repasser a False immediatement
    depuis le seul event `charge.refunded`, sans attendre un eventuel
    `customer.subscription.deleted` separe."""
    _register(client)
    user = session.query(User).one()
    session.add(Subscription(
        id="sub_123", user_id=user.id, stripe_customer_id="cus_123",
        plan_name="flex", status="active",
        current_period_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
    ))
    user.is_premium = True
    session.commit()

    event = {"id": "evt_5", "type": "charge.refunded", "data": {"object": {"customer": "cus_123"}}}
    monkeypatch.setattr(billing_routes.stripe.Webhook, "construct_event", lambda *a, **k: event)
    monkeypatch.setattr(billing_routes.stripe.Subscription, "cancel", lambda sub_id: None)

    resp = client.post("/api/webhooks/stripe", content=b"{}", headers={"stripe-signature": "t=1,v1=ok"})
    assert resp.status_code == 200

    session.refresh(user)
    assert user.is_premium is False
    assert session.query(Subscription).one().status == "canceled"


def test_webhook_charge_refunded_survives_stripe_cancel_error(client, session, monkeypatch):
    """L'annulation cote Stripe est du best-effort: si elle echoue (deja
    annule, erreur reseau), `is_premium` doit quand meme rester coupe."""
    _register(client)
    user = session.query(User).one()
    session.add(Subscription(
        id="sub_123", user_id=user.id, stripe_customer_id="cus_123",
        plan_name="flex", status="active",
        current_period_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
    ))
    user.is_premium = True
    session.commit()

    def fake_cancel(sub_id):
        raise billing_routes.stripe.error.StripeError("deja annule")

    event = {"id": "evt_6", "type": "charge.refunded", "data": {"object": {"customer": "cus_123"}}}
    monkeypatch.setattr(billing_routes.stripe.Webhook, "construct_event", lambda *a, **k: event)
    monkeypatch.setattr(billing_routes.stripe.Subscription, "cancel", fake_cancel)

    resp = client.post("/api/webhooks/stripe", content=b"{}", headers={"stripe-signature": "t=1,v1=ok"})
    assert resp.status_code == 200

    session.refresh(user)
    assert user.is_premium is False


def test_webhook_deduplicates_by_event_id(client, session, monkeypatch):
    """Stripe livre chaque event au moins une fois, parfois deux (retry
    reseau, redemarrage de worker): un `event.id` deja vu doit etre
    ignore sans retraitement, meme si l'effet observable d'aujourd'hui
    (upsert par cle naturelle) le rendrait deja idempotent par
    ailleurs."""
    _register(client)
    user = session.query(User).one()

    calls = {"n": 0}

    def fake_retrieve(sub_id):
        calls["n"] += 1
        return _fake_subscription(user.id)

    event = {
        "id": "evt_replayed",
        "type": "checkout.session.completed",
        "data": {"object": {"client_reference_id": str(user.id), "subscription": "sub_123"}},
    }
    monkeypatch.setattr(billing_routes.stripe.Webhook, "construct_event", lambda *a, **k: event)
    monkeypatch.setattr(billing_routes.stripe.Subscription, "retrieve", fake_retrieve)

    first = client.post("/api/webhooks/stripe", content=b"{}", headers={"stripe-signature": "t=1,v1=ok"})
    assert first.status_code == 200
    assert calls["n"] == 1

    second = client.post("/api/webhooks/stripe", content=b"{}", headers={"stripe-signature": "t=1,v1=ok"})
    assert second.status_code == 200
    assert calls["n"] == 1  # pas de deuxieme appel Stripe: l'event rejoue est ignore.


def test_webhook_404_when_stripe_not_configured(client, monkeypatch):
    # `_stripe_configured()` ne verifie pas stripe_webhook_secret (utile
    # seulement a la verification de signature) mais les cles/prix: c'est
    # l'absence de ceux-ci qui doit rendre la route indisponible.
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    resp = client.post("/api/webhooks/stripe", content=b"{}", headers={"stripe-signature": "t=1,v1=ok"})
    assert resp.status_code == 404


def _fake_checkout_session(email: str = "buyer@example.com", customer: str = "cus_anon", paid: bool = True) -> dict:
    return {
        "payment_status": "paid" if paid else "unpaid",
        "client_reference_id": None,
        "customer": customer,
        "customer_details": {"email": email},
    }


def test_billing_success_redirects_anonymous_payment_to_finalize_page(client, monkeypatch):
    monkeypatch.setattr(
        billing_routes.stripe.checkout.Session, "retrieve",
        lambda session_id, expand=None: _fake_checkout_session(),
    )
    resp = client.get("/billing/success", params={"session_id": "cs_test_anon"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/finaliser-compte.html?session_id=cs_test_anon"


def test_session_info_returns_email_and_link_status(client, session, monkeypatch):
    session.add(Subscription(
        id="sub_anon", user_id=None, stripe_customer_id="cus_anon", plan_name="flex",
        status="active", current_period_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
    ))
    session.commit()
    monkeypatch.setattr(billing_routes.stripe.checkout.Session, "retrieve", lambda session_id: _fake_checkout_session())
    resp = client.get("/api/billing/session-info", params={"session_id": "cs_test_anon"})
    assert resp.status_code == 200
    assert resp.json() == {"email": "buyer@example.com", "already_linked": False}


def test_finalize_creates_account_and_activates_premium(client, session, monkeypatch):
    session.add(Subscription(
        id="sub_anon", user_id=None, stripe_customer_id="cus_anon", plan_name="flex",
        status="active", current_period_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
    ))
    session.commit()
    monkeypatch.setattr(billing_routes.stripe.checkout.Session, "retrieve", lambda session_id: _fake_checkout_session())
    resp = client.post("/api/billing/finalize", json={
        "session_id": "cs_test_anon", "email": "buyer@example.com", "password": "long-enough",
    })
    assert resp.status_code == 200
    assert resp.json() == {"redirect": "/vip.html"}
    user = session.query(User).filter(User.email == "buyer@example.com").one()
    assert user.is_premium is True
    assert session.get(Subscription, "sub_anon").user_id == user.id


def test_finalize_rejects_an_email_that_does_not_match_the_paid_session(client, session, monkeypatch):
    """`body.email` est fourni par le client: sans verification contre
    l'email reel du payeur Stripe, un attaquant en possession d'un
    session_id paye (fuite Referer/historique/logs) pourrait creer un
    compte premium sous l'adresse email d'une victime."""
    session.add(Subscription(
        id="sub_anon", user_id=None, stripe_customer_id="cus_anon", plan_name="flex",
        status="active", current_period_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
    ))
    session.commit()
    monkeypatch.setattr(
        billing_routes.stripe.checkout.Session, "retrieve",
        lambda session_id: _fake_checkout_session(email="buyer@example.com"),
    )
    resp = client.post("/api/billing/finalize", json={
        "session_id": "cs_test_anon", "email": "victim@example.com", "password": "long-enough",
    })
    assert resp.status_code == 403
    assert session.query(User).filter(User.email == "victim@example.com").first() is None


def test_finalize_404_when_webhook_has_not_landed_yet(client, monkeypatch):
    monkeypatch.setattr(billing_routes.stripe.checkout.Session, "retrieve", lambda session_id: _fake_checkout_session())
    resp = client.post("/api/billing/finalize", json={
        "session_id": "cs_test_anon", "email": "buyer@example.com", "password": "long-enough",
    })
    assert resp.status_code == 404


def test_finalize_rejects_a_replayed_session_id(client, session, monkeypatch):
    """Un `session_id` deja echange contre un cookie ne doit pas pouvoir
    re-emettre un compte/cookie une seconde fois (prise de controle de
    compte si le `session_id` fuite via Referer, historique, logs)."""
    session.add(Subscription(
        id="sub_anon", user_id=None, stripe_customer_id="cus_anon", plan_name="flex",
        status="active", current_period_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
    ))
    session.commit()
    monkeypatch.setattr(billing_routes.stripe.checkout.Session, "retrieve", lambda session_id: _fake_checkout_session())

    first = client.post("/api/billing/finalize", json={
        "session_id": "cs_test_anon", "email": "buyer@example.com", "password": "long-enough",
    })
    assert first.status_code == 200

    second = client.post("/api/billing/finalize", json={
        "session_id": "cs_test_anon", "email": "someone-else@example.com", "password": "long-enough",
    })
    assert second.status_code == 401
    # Aucun deuxieme compte n'a ete cree avec le session_id vole.
    assert session.query(User).filter(User.email == "someone-else@example.com").first() is None


def test_billing_success_rejects_a_replayed_session_id(client, session, monkeypatch):
    """Meme protection sur le retour direct de Stripe (compte deja
    identifie au moment du paiement, `client_reference_id` present)."""
    _register(client)
    user = session.query(User).one()

    def fake_retrieve(session_id, expand=None):
        return {
            "payment_status": "paid",
            "client_reference_id": str(user.id),
            "subscription": _fake_subscription(user.id),
        }

    monkeypatch.setattr(billing_routes.stripe.checkout.Session, "retrieve", fake_retrieve)

    first = client.get("/billing/success", params={"session_id": "cs_test_123"}, follow_redirects=False)
    assert first.status_code == 303
    assert first.headers["location"] == "/vip.html"

    second = client.get("/billing/success", params={"session_id": "cs_test_123"}, follow_redirects=False)
    assert second.status_code == 200
    assert "deja" in second.text.lower()
    assert 'href="/login"' in second.text


def test_attach_subscription_after_google_rejects_a_replayed_session_id(session, monkeypatch):
    """Meme protection sur le rattachement post-OAuth Google
    (`/auth/google?billing_session_id=...`, `auth_routes.py`): un
    `session_id` deja reclame ne doit pas pouvoir etre reattache a un
    deuxieme compte Google."""
    session.add(Subscription(
        id="sub_anon", user_id=None, stripe_customer_id="cus_anon", plan_name="flex",
        status="active", current_period_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
    ))
    victim = User(email="victim@example.com")
    attacker = User(email="attacker@example.com")
    session.add_all([victim, attacker])
    session.commit()

    monkeypatch.setattr(billing_routes.stripe.checkout.Session, "retrieve", lambda session_id: _fake_checkout_session())

    first = billing_routes.attach_subscription_after_google(session, "cs_test_anon", victim)
    assert first.headers["location"] == "/vip.html"
    assert session.get(Subscription, "sub_anon").user_id == victim.id

    second = billing_routes.attach_subscription_after_google(session, "cs_test_anon", attacker)
    assert second.headers["location"] == "/premium.html"
    # L'abonnement reste attribue a la victime, pas reattribue a l'attaquant.
    assert session.get(Subscription, "sub_anon").user_id == victim.id
