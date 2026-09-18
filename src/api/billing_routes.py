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
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session as SASession

from src.api.auth import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    get_user_by_email,
    normalize_email,
    password_problem,
    register_user,
    token_for_user,
)
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
    price_id = PLANS.get(body.plan, lambda: None)()
    if not price_id:
        raise HTTPException(status_code=400, detail="Offre invalide")

    info = _session(request)
    user = db.get(User, info.user_id) if info is not None and info.user_id != 0 else None
    checkout_kwargs: dict = {}
    if user is not None:
        checkout_kwargs["client_reference_id"] = str(user.id)
        checkout_kwargs["customer_email"] = user.email
    origin = str(request.base_url).rstrip("/")
    stripe.api_key = settings.stripe_secret_key
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=f"{origin}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{origin}/payant.html",
            **checkout_kwargs,
        )
    except stripe.error.StripeError as exc:
        log.error("Echec de creation de session Stripe Checkout: %s", exc)
        raise HTTPException(status_code=500, detail="Le paiement n'a pas pu démarrer") from exc
    return {"url": checkout_session.url}


def _upsert_subscription(db: SASession, stripe_subscription: dict, user_id: int | None, plan_name: str | None = None) -> None:
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
        if row.user_id is None and user_id is not None:
            row.user_id = user_id
    if row.user_id is not None:
        user = db.get(User, row.user_id)
        if user is not None:
            user.is_premium = status in ("active", "trialing")
    db.commit()


def _issue_session_cookie(response: JSONResponse, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME, token, max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True, samesite="lax", secure=settings.cookie_secure,
    )


def _retrieve_paid_session(session_id: str) -> dict:
    stripe.api_key = settings.stripe_secret_key
    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
    except stripe.error.StripeError as exc:
        log.warning("Session Checkout introuvable: %s", exc)
        raise HTTPException(status_code=400, detail="Session de paiement invalide") from exc
    if checkout_session.get("payment_status") != "paid":
        raise HTTPException(status_code=400, detail="Paiement non confirme")
    return checkout_session


def _find_subscription_by_customer(db: SASession, customer_id: str | None) -> Subscription | None:
    return (
        db.query(Subscription).filter(Subscription.stripe_customer_id == customer_id)
        .order_by(Subscription.created_at.desc()).first()
    )


@router.get("/api/billing/session-info")
def billing_session_info(session_id: str, db: SASession = Depends(get_db)):
    if not _stripe_configured():
        raise HTTPException(status_code=404, detail="Paiement non configure")
    checkout_session = _retrieve_paid_session(session_id)
    email = (checkout_session.get("customer_details") or {}).get("email", "")
    row = _find_subscription_by_customer(db, checkout_session.get("customer"))
    return {"email": email, "already_linked": row is not None and row.user_id is not None}


class FinalizeRequest(BaseModel):
    session_id: str
    email: str
    password: str


@router.post("/api/billing/finalize")
def finalize_billing_account(body: FinalizeRequest, db: SASession = Depends(get_db)):
    if not _stripe_configured():
        raise HTTPException(status_code=404, detail="Paiement non configure")
    checkout_session = _retrieve_paid_session(body.session_id)
    row = _find_subscription_by_customer(db, checkout_session.get("customer"))
    if row is None:
        raise HTTPException(status_code=404, detail="Le paiement est en cours de confirmation")
    if row.user_id is not None:
        user = db.get(User, row.user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="Compte introuvable")
        response = JSONResponse({"redirect": "/vip.html"})
        _issue_session_cookie(response, token_for_user(user))
        return response
    email = normalize_email(body.email)
    if "@" not in email or len(email) > 320:
        raise HTTPException(status_code=422, detail="Adresse email invalide")
    problem = password_problem(body.password)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    if get_user_by_email(db, email) is not None:
        raise HTTPException(status_code=409, detail="Un compte existe deja avec cet email")
    user = register_user(db, email, body.password)
    row.user_id = user.id
    user.is_premium = row.status in ("active", "trialing")
    db.commit()
    response = JSONResponse({"redirect": "/vip.html"})
    _issue_session_cookie(response, token_for_user(user))
    return response


def attach_subscription_after_google(db: SASession, session_id: str, user: User) -> RedirectResponse:
    if not _stripe_configured():
        return RedirectResponse(url="/premium.html", status_code=303)
    try:
        checkout_session = _retrieve_paid_session(session_id)
    except HTTPException:
        return RedirectResponse(url="/premium.html", status_code=303)
    row = _find_subscription_by_customer(db, checkout_session.get("customer"))
    if row is not None and row.user_id is None:
        row.user_id = user.id
        user.is_premium = row.status in ("active", "trialing")
        db.commit()
    return _login_response(token_for_user(user), url="/vip.html")


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
        return RedirectResponse(url=f"/finaliser-compte.html?session_id={session_id}", status_code=303)

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
        subscription_id = data.get("subscription")
        if subscription_id:
            user_id_raw = data.get("client_reference_id")
            user_id = int(user_id_raw) if user_id_raw and str(user_id_raw).isdigit() else None
            stripe.api_key = settings.stripe_secret_key
            subscription = stripe.Subscription.retrieve(subscription_id)
            _upsert_subscription(db, subscription, user_id)

    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        row = db.get(Subscription, data["id"])
        if row is not None:
            row.status = "canceled" if event_type == "customer.subscription.deleted" else data["status"]
            user = db.get(User, row.user_id) if row.user_id is not None else None
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
                user = db.get(User, row.user_id) if row.user_id is not None else None
                if user is not None:
                    user.is_premium = False
                db.commit()

    return {"status": "success"}
