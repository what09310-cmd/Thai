"""Abonnements payants: session Stripe Checkout, retour de paiement, webhooks.

`User.is_premium` reste la seule source lue par le reste de l'application
(src/api/security.py::PROTECTED_PATHS); `Subscription` (src/database/models.py)
n'est qu'un historique de facturation.

Le webhook est la source de verite durable du statut (renouvellement,
resiliation, echec de paiement), independamment du navigateur; /billing/success
n'existe que pour re-emettre tout de suite le cookie de session, qui ne
revient sinon a jour qu'a la prochaine connexion (src/api/auth.py).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session as SASession

from src.api.auth import token_for_user
from src.api.auth_routes import _login_response
from src.api.deps import get_db
from src.api.security import _session
from src.config import settings
from src.database.models import Subscription, User

log = logging.getLogger(__name__)
router = APIRouter()

PLANS = {
    "flex": lambda: settings.stripe_price_flex,
    "essentiel": lambda: settings.stripe_price_essentiel,
    "serenite": lambda: settings.stripe_price_serenite,
}


def _stripe_configured() -> bool:
    return bool(
        settings.stripe_secret_key
        and settings.stripe_price_flex
        and settings.stripe_price_essentiel
        and settings.stripe_price_serenite
    )


class CheckoutRequest(BaseModel):
    plan: str


@router.post("/api/checkout/create-session")
def create_checkout_session(body: CheckoutRequest, request: Request, db: SASession = Depends(get_db)):
    if not _stripe_configured():
        raise HTTPException(status_code=404, detail="Paiement non configuré")
    info = _session(request)
    if info is None or info.user_id == 0:
        # user_id 0 est le compte administrateur (src/api/auth.py), qui n'a
        # pas de ligne dans `users` et donc pas d'email a facturer.
        raise HTTPException(status_code=401, detail="Connexion requise")
    price_id = PLANS.get(body.plan, lambda: None)()
    if not price_id:
        raise HTTPException(status_code=400, detail="Offre invalide")

    user = db.get(User, info.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Connexion requise")
    origin = str(request.base_url).rstrip("/")
    stripe.api_key = settings.stripe_secret_key
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            client_reference_id=str(user.id),
            customer_email=user.email,
            success_url=f"{origin}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{origin}/payant.html",
        )
    except stripe.error.StripeError as exc:
        log.error("Echec de creation de session Stripe Checkout: %s", exc)
        raise HTTPException(status_code=500, detail="Le paiement n'a pas pu démarrer") from exc
    return {"url": checkout_session.url}


def _upsert_subscription(db: SASession, stripe_subscription: dict, user_id: int, plan_name: str | None = None) -> None:
    """Cree ou met a jour la ligne `Subscription`, active `User.is_premium`.

    Idempotent: appele a la fois par /billing/success et par
    checkout.session.completed, la cle primaire (id d'abonnement Stripe)
    absorbe le doublon sans creer deux lignes.
    """
    sub_id = stripe_subscription["id"]
    row = db.get(Subscription, sub_id)
    period_end = datetime.fromtimestamp(stripe_subscription["current_period_end"], tz=timezone.utc)
    status = stripe_subscription["status"]
    if row is None:
        row = Subscription(
            id=sub_id,
            user_id=user_id,
            stripe_customer_id=stripe_subscription["customer"],
            plan_name=plan_name or "inconnu",
            status=status,
            current_period_end=period_end,
        )
        db.add(row)
    else:
        row.status = status
        row.current_period_end = period_end
        if plan_name:
            row.plan_name = plan_name
    user = db.get(User, user_id)
    if user is not None:
        user.is_premium = status in ("active", "trialing")
    db.commit()


@router.get("/billing/success")
def billing_success(session_id: str | None = None, db: SASession = Depends(get_db)):
    """Retour de Stripe Checkout: confirme le paiement, active le compte,
    re-emet le cookie de session a jour avant de renvoyer vers /vip.html."""
    if not _stripe_configured() or not session_id:
        return RedirectResponse(url="/premium.html", status_code=303)

    stripe.api_key = settings.stripe_secret_key
    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id, expand=["subscription"])
    except stripe.error.StripeError as exc:
        log.warning("Session Checkout introuvable a /billing/success: %s", exc)
        return RedirectResponse(url="/premium.html", status_code=303)

    if checkout_session.get("payment_status") != "paid":
        return RedirectResponse(url="/premium.html", status_code=303)

    user_id_raw = checkout_session.get("client_reference_id")
    if not user_id_raw or not str(user_id_raw).isdigit():
        return RedirectResponse(url="/premium.html", status_code=303)

    user = db.get(User, int(user_id_raw))
    subscription = checkout_session.get("subscription")
    if user is None or not subscription:
        return RedirectResponse(url="/premium.html", status_code=303)
    _upsert_subscription(db, subscription, user.id)
    db.refresh(user)
    return _login_response(token_for_user(user), url="/vip.html")


@router.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request, db: SASession = Depends(get_db)):
    if not _stripe_configured():
        raise HTTPException(status_code=404, detail="Paiement non configuré")
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        log.warning("Webhook Stripe rejete: %s", exc)
        raise HTTPException(status_code=400, detail="Webhook invalide") from exc

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        user_id_raw = data.get("client_reference_id")
        subscription_id = data.get("subscription")
        if user_id_raw and str(user_id_raw).isdigit() and subscription_id:
            stripe.api_key = settings.stripe_secret_key
            subscription = stripe.Subscription.retrieve(subscription_id)
            _upsert_subscription(db, subscription, int(user_id_raw))

    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        row = db.get(Subscription, data["id"])
        if row is not None:
            row.status = "canceled" if event_type == "customer.subscription.deleted" else data["status"]
            user = db.get(User, row.user_id)
            if user is not None:
                user.is_premium = row.status in ("active", "trialing")
            db.commit()

    elif event_type == "invoice.payment_failed":
        customer_id = data.get("customer")
        if customer_id:
            row = (
                db.query(Subscription)
                .filter(Subscription.stripe_customer_id == customer_id)
                .order_by(Subscription.created_at.desc())
                .first()
            )
            if row is not None:
                row.status = "past_due"
                user = db.get(User, row.user_id)
                if user is not None:
                    user.is_premium = False
                db.commit()

    return {"status": "success"}
