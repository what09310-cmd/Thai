# Stripe Payment-First Signup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an anonymous visitor on `payant.html` pay via Stripe Checkout before creating an account. Stripe collects the paying email itself; account creation (password or Google) happens on a new return page, and only then does the subscription attach to a `User` and a session cookie get issued.

**Architecture:** `POST /api/checkout/create-session` stops requiring a login — it omits `client_reference_id`/`customer_email` for anonymous callers so Stripe collects the email itself. The webhook creates a `Subscription` row with `user_id=NULL` when there's no `client_reference_id`. `GET /billing/success` redirects an unlinked payment to `frontend/finaliser-compte.html`, a new static page that collects a password (via a new `POST /api/billing/finalize`) or triggers Google OAuth (reusing `/auth/google`, threading the Stripe session id through `request.session` the same way `post_login_next` already works) to attach the paid `Subscription` row to a real `User` and issue the session cookie. The existing compte-first path (already logged in before clicking a plan) is untouched.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, `stripe` Python SDK, authlib (Google OAuth), vanilla JS static frontend, pytest with `stripe` calls mocked via `monkeypatch`.

**Spec:** `docs/superpowers/specs/2026-09-17-stripe-payment-first-signup-design.md` (this plan implements it end to end; read both together — the spec has the full rationale for each decision, this plan has the exact code).

## Global Constraints

- Never call the real Stripe network in tests — mock `stripe.checkout.Session.create/retrieve`, `stripe.Subscription.retrieve`, `stripe.Webhook.construct_event` via `monkeypatch`, per `.claude/rules/testing.md` and the existing `tests/test_billing.py` pattern.
- Schema changes to an existing database go through `scripts/migrate.py`, never bare `create_all` (`.claude/rules/database.md`).
- `User.is_premium` remains the only field the rest of the app reads for access — `Subscription` stays bookkeeping.
- The compte-first path (`client_reference_id` present at Checkout creation) must keep working exactly as before — this is an additive path, not a replacement. Every existing test in `tests/test_billing.py` that isn't explicitly called out below must keep passing unmodified.
- Run `pytest` and `ruff check src scripts tests` from the repo root (`c:\Users\rxWe3\renthub-tracker`) — imports resolve from there.
- Commit after each task, matching this repo's existing commit style (French, present-tense summary, `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` trailer).

---

## Task 1: Make `Subscription.user_id` nullable + migration step

**Files:**
- Modify: `src/database/models.py:264-277` (`Subscription` class)
- Modify: `scripts/migrate.py`
- Test: `tests/test_billing.py` (new test near the top, after imports)

**Interfaces:**
- Produces: `Subscription.user_id` is now `Mapped[Optional[int]]`, nullable — every later task's webhook/finalize code relies on being able to insert a `Subscription` row with `user_id=None`.

- [ ] **Step 1: Edit the model**

In `src/database/models.py`, change:

```python
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
```

to:

```python
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    # None entre le webhook `checkout.session.completed` et la finalisation
    # du compte (design 2026-09-17-stripe-payment-first-signup): un paiement
    # anonyme cree cette ligne avant qu'un compte existe.
    user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
```

(`Optional` is already imported at the top of `models.py` — no new import needed.)

- [ ] **Step 2: Add the migration step to `scripts/migrate.py`**

Add `Subscription` to the model import at the top:

```python
from src.database.models import Base, Listing, ScanLog, Subscription  # noqa: E402
```

Add this new function after `drop_obsolete_indexes` (before `fix_sequences`):

```python
def relax_subscription_user_id(conn, dry_run: bool) -> None:
    """Retire la contrainte NOT NULL de `subscriptions.user_id`.

    Necessaire pour le paiement avant creation de compte (design
    2026-09-17-stripe-payment-first-signup): la ligne existe entre le
    webhook Stripe et la finalisation du compte, sans utilisateur encore
    attribue. Postgres sait le faire en place; SQLite n'a pas d'ALTER
    COLUMN et exige de reconstruire la table.
    """
    inspector = inspect(conn)
    if "subscriptions" not in inspector.get_table_names():
        return
    columns = {c["name"]: c for c in inspector.get_columns("subscriptions")}
    if "user_id" not in columns or columns["user_id"]["nullable"]:
        return
    _log(dry_run, "subscriptions.user_id: retrait de la contrainte NOT NULL")
    if dry_run:
        return
    if engine.url.get_backend_name() == "postgresql":
        conn.execute(text("ALTER TABLE subscriptions ALTER COLUMN user_id DROP NOT NULL"))
        return
    conn.execute(text("ALTER TABLE subscriptions RENAME TO subscriptions_old"))
    Subscription.__table__.create(conn)
    conn.execute(text(
        "INSERT INTO subscriptions (id, user_id, stripe_customer_id, plan_name, status, "
        "current_period_end, created_at) "
        "SELECT id, user_id, stripe_customer_id, plan_name, status, current_period_end, created_at "
        "FROM subscriptions_old"
    ))
    conn.execute(text("DROP TABLE subscriptions_old"))
```

Call it from `main()`, right after `add_missing_columns`:

```python
        add_missing_columns(conn, args.dry_run)
        relax_subscription_user_id(conn, args.dry_run)
        dedupe_listing_images(conn, args.dry_run)
```

- [ ] **Step 3: Write the regression test**

In `tests/test_billing.py`, add near the top (after the `_fake_subscription` helper):

```python
def test_subscription_user_id_is_nullable(session):
    """Une ligne Subscription sans user_id doit s'inserer sans lever
    d'IntegrityError (paiement anonyme, webhook avant finalisation du compte)."""
    session.add(Subscription(
        id="sub_anon", user_id=None, stripe_customer_id="cus_anon",
        plan_name="flex", status="active",
        current_period_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
    ))
    session.commit()
    assert session.query(Subscription).filter(Subscription.id == "sub_anon").one().user_id is None
```

- [ ] **Step 4: Run the test**

Run: `pytest tests/test_billing.py::test_subscription_user_id_is_nullable -v`
Expected: PASS (the in-memory test DB is built fresh from `Base.metadata.create_all` per `tests/conftest.py`, so it already reflects the model change — this test doesn't exercise `scripts/migrate.py` itself, only that the model allows the insert).

- [ ] **Step 5: Sanity-check the migration script**

Run: `python scripts/migrate.py --dry-run`
Expected: exits 0, and if a local `thaimonth.db` exists with the old schema, prints a `[dry-run] subscriptions.user_id: retrait de la contrainte NOT NULL` line. If no local DB exists, it just prints "Rien ecrit (dry-run)." with no error — either outcome is fine here, this step is a smoke test that the script still imports and runs.

- [ ] **Step 6: Commit**

```bash
git add src/database/models.py scripts/migrate.py tests/test_billing.py
git commit -m "$(cat <<'EOF'
Make Subscription.user_id nullable for anonymous Stripe payments

A payment-first checkout (see 2026-09-17-stripe-payment-first-signup
spec) creates the Subscription row from the webhook before any account
exists to attribute it to.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Allow anonymous checkout session creation

**Files:**
- Modify: `src/api/billing_routes.py:53-84` (`create_checkout_session`)
- Test: `tests/test_billing.py`

**Interfaces:**
- Consumes: `_session(request)` from `src/api/security.py` (unchanged signature, returns `SessionInfo | None`).
- Produces: `create_checkout_session` no longer 401s when there's no session — later tasks (webhook, `/billing/success`) must handle a Stripe event/session with no `client_reference_id`.

- [ ] **Step 1: Update the existing "requires login" test to reflect the new behavior**

In `tests/test_billing.py`, replace `test_create_session_requires_login`:

```python
def test_create_session_requires_login(client):
    resp = client.post("/api/checkout/create-session", json={"plan": "flex"})
    assert resp.status_code == 401
```

with:

```python
def test_create_session_allows_anonymous_checkout(client, monkeypatch):
    """Paiement avant creation de compte (2026-09-17-stripe-payment-first-signup):
    pas de session requise, et Stripe collecte l'email lui-meme."""
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(url="https://checkout.stripe.com/pay/cs_test_anon")

    monkeypatch.setattr(billing_routes.stripe.checkout.Session, "create", fake_create)

    resp = client.post("/api/checkout/create-session", json={"plan": "flex"})
    assert resp.status_code == 200
    assert resp.json() == {"url": "https://checkout.stripe.com/pay/cs_test_anon"}
    assert "client_reference_id" not in captured
    assert "customer_email" not in captured
    assert captured["customer_creation"] == "always"
```

- [ ] **Step 2: Run it to confirm it fails against the current code**

Run: `pytest tests/test_billing.py::test_create_session_allows_anonymous_checkout -v`
Expected: FAIL with 401 (current code still requires login).

- [ ] **Step 3: Update `create_checkout_session`**

In `src/api/billing_routes.py`, replace the body of `create_checkout_session` (lines 53-84):

```python
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
        # Compte deja connu: on peut attribuer directement l'abonnement a
        # la creation de la session Checkout (chemin compte-avant-paiement,
        # inchange depuis 2026-09-17-stripe-subscriptions-design.md).
        checkout_kwargs["client_reference_id"] = str(user.id)
        checkout_kwargs["customer_email"] = user.email
    else:
        # Visiteur anonyme (2026-09-17-stripe-payment-first-signup-design.md):
        # Stripe collecte lui-meme l'email, le compte se cree au retour sur
        # /finaliser-compte.html.
        checkout_kwargs["customer_creation"] = "always"

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
```

- [ ] **Step 4: Run the full billing test file**

Run: `pytest tests/test_billing.py -v`
Expected: `test_create_session_allows_anonymous_checkout` PASSes; `test_create_session_returns_checkout_url` (the logged-in path) still PASSes unmodified — it still asserts `client_reference_id`/`customer_email` are present when a user is logged in.

- [ ] **Step 5: Commit**

```bash
git add src/api/billing_routes.py tests/test_billing.py
git commit -m "$(cat <<'EOF'
Allow anonymous Stripe checkout session creation

The logged-in path is unchanged (still attributes the session at
creation time); an anonymous visitor now gets a Checkout session with
customer_creation=always instead of a 401.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Webhook creates an unlinked `Subscription` for anonymous payments

**Files:**
- Modify: `src/api/billing_routes.py:87-197` (`_upsert_subscription`, `stripe_webhook`)
- Test: `tests/test_billing.py`

**Interfaces:**
- Consumes: `Subscription.user_id: Optional[int]` (Task 1).
- Produces: `_upsert_subscription(db, stripe_subscription: dict, user_id: int | None, plan_name: str | None = None) -> None` — the `user_id` parameter type widens from `int` to `int | None`; later tasks (`/billing/success`, `/api/billing/finalize`) call this with a real int once they've identified the user.

- [ ] **Step 1: Write the failing test**

In `tests/test_billing.py`, add:

```python
def test_webhook_checkout_completed_without_reference_creates_unlinked_subscription(client, session, monkeypatch):
    """Paiement anonyme: le webhook cree la ligne Subscription avec
    user_id=None, sans lever d'erreur sur l'absence de client_reference_id."""
    event = {
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

    row = session.query(Subscription).filter(Subscription.id == "sub_anon").one()
    assert row.user_id is None
    assert row.stripe_customer_id == "cus_anon"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_billing.py::test_webhook_checkout_completed_without_reference_creates_unlinked_subscription -v`
Expected: FAIL — current webhook code skips the upsert entirely when `client_reference_id` is falsy (`if user_id_raw and ...`), so no `Subscription` row is created and the query in the test raises `NoResultFound`.

- [ ] **Step 3: Update `_upsert_subscription` and the webhook handler**

In `src/api/billing_routes.py`, replace `_upsert_subscription` (lines 87-116):

```python
def _upsert_subscription(db: SASession, stripe_subscription: dict, user_id: int | None, plan_name: str | None = None) -> None:
    """Cree ou met a jour la ligne `Subscription`, active `User.is_premium`
    quand un utilisateur est deja connu.

    Idempotent: appele a la fois par /billing/success, /api/billing/finalize
    et checkout.session.completed, la cle primaire (id d'abonnement Stripe)
    absorbe le doublon sans creer deux lignes. `user_id` peut etre None
    (paiement anonyme, design 2026-09-17-stripe-payment-first-signup): la
    ligne existe alors sans activer aucun compte, jusqu'a la finalisation.
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
```

Replace the `checkout.session.completed` branch inside `stripe_webhook` (lines 164-170):

```python
    if event_type == "checkout.session.completed":
        subscription_id = data.get("subscription")
        if subscription_id:
            user_id_raw = data.get("client_reference_id")
            user_id = int(user_id_raw) if user_id_raw and str(user_id_raw).isdigit() else None
            stripe.api_key = settings.stripe_secret_key
            subscription = stripe.Subscription.retrieve(subscription_id)
            _upsert_subscription(db, subscription, user_id)
```

Guard the two other branches against `row.user_id is None` — replace lines 172-179:

```python
    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        row = db.get(Subscription, data["id"])
        if row is not None:
            row.status = "canceled" if event_type == "customer.subscription.deleted" else data["status"]
            user = db.get(User, row.user_id) if row.user_id is not None else None
            if user is not None:
                user.is_premium = row.status in ("active", "trialing")
            db.commit()
```

And lines 181-195 (the `invoice.payment_failed` branch) — replace the `user = db.get(User, row.user_id)` line:

```python
            if row is not None:
                row.status = "past_due"
                user = db.get(User, row.user_id) if row.user_id is not None else None
                if user is not None:
                    user.is_premium = False
                db.commit()
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_billing.py -v`
Expected: all PASS, including the new test and the existing `test_webhook_checkout_completed_activates_premium`, `test_webhook_subscription_deleted_revokes_premium`, `test_webhook_payment_failed_revokes_premium`.

- [ ] **Step 5: Commit**

```bash
git add src/api/billing_routes.py tests/test_billing.py
git commit -m "$(cat <<'EOF'
Webhook creates unlinked Subscription rows for anonymous payments

checkout.session.completed with no client_reference_id now creates
the Subscription with user_id=NULL instead of being silently skipped;
the other event handlers guard against a still-unlinked row.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `/billing/success` redirects an unlinked payment to the finalize page

**Files:**
- Modify: `src/api/billing_routes.py:119-146` (`billing_success`)
- Test: `tests/test_billing.py`

**Interfaces:**
- Produces: `GET /billing/success` redirects to `/finaliser-compte.html?session_id=<id>` (303) when the Checkout Session has no `client_reference_id` — Task 7's static page reads that query param.

- [ ] **Step 1: Write the failing test**

```python
def test_billing_success_redirects_anonymous_payment_to_finalize_page(client, session, monkeypatch):
    monkeypatch.setattr(
        billing_routes.stripe.checkout.Session, "retrieve",
        lambda session_id, expand=None: {"payment_status": "paid", "client_reference_id": None},
    )

    resp = client.get("/billing/success", params={"session_id": "cs_test_anon"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/finaliser-compte.html?session_id=cs_test_anon"
    assert session.query(User).count() == 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_billing.py::test_billing_success_redirects_anonymous_payment_to_finalize_page -v`
Expected: FAIL — current code hits `if not user_id_raw or not str(user_id_raw).isdigit(): return RedirectResponse(url="/premium.html", ...)`, so `location` is `/premium.html`, not the finalize page.

- [ ] **Step 3: Update `billing_success`**

In `src/api/billing_routes.py`, replace lines 136-138 (the `if not user_id_raw...` guard) — keep everything above and below it unchanged:

```python
    user_id_raw = checkout_session.get("client_reference_id")
    if not user_id_raw or not str(user_id_raw).isdigit():
        # Paiement anonyme (2026-09-17-stripe-payment-first-signup-design.md):
        # pas encore de compte a attribuer, la finalisation se fait sur
        # /finaliser-compte.html.
        return RedirectResponse(url=f"/finaliser-compte.html?session_id={session_id}", status_code=303)
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_billing.py -v`
Expected: all PASS, including `test_billing_success_activates_premium_and_reissues_cookie` (still exercises the `client_reference_id` present path, unaffected by this change).

- [ ] **Step 5: Commit**

```bash
git add src/api/billing_routes.py tests/test_billing.py
git commit -m "$(cat <<'EOF'
Redirect unattributed Stripe payments to the account finalize page

/billing/success used to send a payment with no client_reference_id to
/premium.html as if it were unpaid; it now recognizes the anonymous
payment-first case and sends it to /finaliser-compte.html instead.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `POST /api/billing/finalize` + `GET /api/billing/session-info` (password path)

**Files:**
- Modify: `src/api/billing_routes.py` (imports, new `BaseModel`, new routes)
- Test: `tests/test_billing.py`

**Interfaces:**
- Consumes: `normalize_email`, `password_problem`, `get_user_by_email`, `register_user` from `src.api.auth` (all existing, unchanged signatures — see `src/api/auth.py:156-192`); `SESSION_COOKIE_NAME`, `SESSION_MAX_AGE_SECONDS` from `src.api.auth`; `token_for_user` (already imported).
- Produces: `POST /api/billing/finalize` (body `{"session_id", "email", "password"}`) → `200 {"redirect": "/vip.html"}` with session cookie set, or `400`/`404`/`409`/`422` on failure. `GET /api/billing/session-info?session_id=` → `{"email": str, "already_linked": bool}`. Task 6 (Google path) and Task 7 (frontend) both call these.

- [ ] **Step 1: Write the failing tests**

In `tests/test_billing.py`, add a new section at the end of the file:

```python
# ── GET /api/billing/session-info + POST /api/billing/finalize ─────

def _fake_checkout_session(email: str = "buyer@example.com", customer: str = "cus_anon", paid: bool = True) -> dict:
    return {
        "payment_status": "paid" if paid else "unpaid",
        "client_reference_id": None,
        "customer": customer,
        "customer_details": {"email": email},
    }


def test_session_info_returns_email_and_link_status(client, session, monkeypatch):
    session.add(Subscription(
        id="sub_anon", user_id=None, stripe_customer_id="cus_anon",
        plan_name="flex", status="active",
        current_period_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
    ))
    session.commit()
    monkeypatch.setattr(
        billing_routes.stripe.checkout.Session, "retrieve",
        lambda session_id, expand=None: _fake_checkout_session(),
    )

    resp = client.get("/api/billing/session-info", params={"session_id": "cs_test_anon"})
    assert resp.status_code == 200
    assert resp.json() == {"email": "buyer@example.com", "already_linked": False}


def test_session_info_404_when_stripe_not_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    resp = client.get("/api/billing/session-info", params={"session_id": "cs_test_anon"})
    assert resp.status_code == 404


def test_finalize_creates_account_and_activates_premium(client, session, monkeypatch):
    session.add(Subscription(
        id="sub_anon", user_id=None, stripe_customer_id="cus_anon",
        plan_name="flex", status="active",
        current_period_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
    ))
    session.commit()
    monkeypatch.setattr(
        billing_routes.stripe.checkout.Session, "retrieve",
        lambda session_id, expand=None: _fake_checkout_session(),
    )

    resp = client.post("/api/billing/finalize", json={
        "session_id": "cs_test_anon", "email": "buyer@example.com", "password": "long-enough",
    })
    assert resp.status_code == 200
    assert resp.json() == {"redirect": "/vip.html"}
    assert "set-cookie" in resp.headers

    user = session.query(User).filter(User.email == "buyer@example.com").one()
    assert user.is_premium is True
    row = session.query(Subscription).filter(Subscription.id == "sub_anon").one()
    assert row.user_id == user.id


def test_finalize_409_when_email_already_registered(client, session, monkeypatch):
    _register(client)  # cree a@b.co
    session.add(Subscription(
        id="sub_anon", user_id=None, stripe_customer_id="cus_anon",
        plan_name="flex", status="active",
        current_period_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
    ))
    session.commit()
    monkeypatch.setattr(
        billing_routes.stripe.checkout.Session, "retrieve",
        lambda session_id, expand=None: _fake_checkout_session(email="a@b.co"),
    )

    resp = client.post("/api/billing/finalize", json={
        "session_id": "cs_test_anon", "email": "a@b.co", "password": "long-enough",
    })
    assert resp.status_code == 409


def test_finalize_404_when_webhook_has_not_landed_yet(client, monkeypatch):
    monkeypatch.setattr(
        billing_routes.stripe.checkout.Session, "retrieve",
        lambda session_id, expand=None: _fake_checkout_session(),
    )
    resp = client.post("/api/billing/finalize", json={
        "session_id": "cs_test_anon", "email": "buyer@example.com", "password": "long-enough",
    })
    assert resp.status_code == 404


def test_finalize_400_when_payment_not_confirmed(client, monkeypatch):
    monkeypatch.setattr(
        billing_routes.stripe.checkout.Session, "retrieve",
        lambda session_id, expand=None: _fake_checkout_session(paid=False),
    )
    resp = client.post("/api/billing/finalize", json={
        "session_id": "cs_test_anon", "email": "buyer@example.com", "password": "long-enough",
    })
    assert resp.status_code == 400


def test_finalize_is_idempotent_on_double_submit(client, session, monkeypatch):
    session.add(Subscription(
        id="sub_anon", user_id=None, stripe_customer_id="cus_anon",
        plan_name="flex", status="active",
        current_period_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
    ))
    session.commit()
    monkeypatch.setattr(
        billing_routes.stripe.checkout.Session, "retrieve",
        lambda session_id, expand=None: _fake_checkout_session(),
    )

    body = {"session_id": "cs_test_anon", "email": "buyer@example.com", "password": "long-enough"}
    first = client.post("/api/billing/finalize", json=body)
    second = client.post("/api/billing/finalize", json=body)
    assert first.status_code == 200
    assert second.status_code == 200
    assert session.query(User).count() == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_billing.py -k "session_info or finalize" -v`
Expected: all FAIL with 404 "Not Found" (neither route exists yet).

- [ ] **Step 3: Add the imports and new routes**

In `src/api/billing_routes.py`, update the import block at the top:

```python
from fastapi.responses import JSONResponse, RedirectResponse
```

and:

```python
from src.api.auth import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    get_user_by_email,
    normalize_email,
    password_problem,
    register_user,
    token_for_user,
)
```

Add near the bottom of the file, after `_upsert_subscription` and before `@router.get("/billing/success")`:

```python
def _issue_session_cookie(response: JSONResponse, token: str) -> None:
    """Meme forme de cookie que `_login_response` (auth_routes.py), sur une
    reponse JSON plutot qu'une redirection: /api/billing/finalize est appele
    en fetch() depuis frontend/finaliser-compte.html, qui gere elle-meme la
    navigation vers /vip.html."""
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


def _retrieve_paid_session(session_id: str) -> dict:
    """Recupere et verifie la session Checkout Stripe; leve HTTPException
    sinon. Ne fait jamais confiance au client au-dela du session_id: c'est
    ce recontrole qui autorise la creation/liaison du compte, pas la seule
    presence du parametre dans l'URL."""
    stripe.api_key = settings.stripe_secret_key
    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
    except stripe.error.StripeError as exc:
        log.warning("Session Checkout introuvable a /api/billing/finalize: %s", exc)
        raise HTTPException(status_code=400, detail="Session de paiement invalide") from exc
    if checkout_session.get("payment_status") != "paid":
        raise HTTPException(status_code=400, detail="Paiement non confirmé")
    return checkout_session


def _find_unlinked_or_pending_subscription(db: SASession, customer_id: str | None) -> Subscription | None:
    return (
        db.query(Subscription)
        .filter(Subscription.stripe_customer_id == customer_id)
        .order_by(Subscription.created_at.desc())
        .first()
    )


@router.get("/api/billing/session-info")
def billing_session_info(session_id: str, db: SASession = Depends(get_db)):
    if not _stripe_configured():
        raise HTTPException(status_code=404, detail="Paiement non configuré")
    checkout_session = _retrieve_paid_session(session_id)
    email = (checkout_session.get("customer_details") or {}).get("email", "")
    row = _find_unlinked_or_pending_subscription(db, checkout_session.get("customer"))
    return {"email": email, "already_linked": row is not None and row.user_id is not None}


class FinalizeRequest(BaseModel):
    session_id: str
    email: str
    password: str


@router.post("/api/billing/finalize")
def finalize_billing_account(body: FinalizeRequest, db: SASession = Depends(get_db)):
    if not _stripe_configured():
        raise HTTPException(status_code=404, detail="Paiement non configuré")
    checkout_session = _retrieve_paid_session(body.session_id)
    row = _find_unlinked_or_pending_subscription(db, checkout_session.get("customer"))
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Le paiement est en cours de confirmation, réessayez dans un instant",
        )
    if row.user_id is not None:
        # Deja finalise (double soumission, ou webhook + retour concurrents):
        # pas d'erreur, on reconfirme simplement la meme identite.
        user = db.get(User, row.user_id)
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
        raise HTTPException(status_code=409, detail="Un compte existe déjà avec cet email")

    user = register_user(db, email, body.password)
    row.user_id = user.id
    user.is_premium = row.status in ("active", "trialing")
    db.commit()
    response = JSONResponse({"redirect": "/vip.html"})
    _issue_session_cookie(response, token_for_user(user))
    return response
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_billing.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/billing_routes.py tests/test_billing.py
git commit -m "$(cat <<'EOF'
Add POST /api/billing/finalize and GET /api/billing/session-info

Password path of the payment-first signup flow: turns a paid, unlinked
Subscription row into a real account and issues the session cookie,
re-verifying payment status against Stripe rather than trusting the
client-supplied session_id alone.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Google path — link via `/auth/google`

**Files:**
- Modify: `src/api/auth_routes.py:398-442` (`google_login`, `google_callback`)
- Modify: `src/api/billing_routes.py` (new `attach_subscription_after_google`)
- Test: `tests/test_billing.py`, `tests/test_auth.py` if it exists (check with `Glob "tests/test_auth*.py"` first — if Google OAuth tests live in a different file, add there instead of `test_billing.py`)

**Interfaces:**
- Consumes: `get_or_create_google_user(db, sub, email, email_verified) -> User | None` (`src/api/auth.py:252-282`, unchanged); `_retrieve_paid_session`, `_find_unlinked_or_pending_subscription` (Task 5).
- Produces: `billing_routes.attach_subscription_after_google(db: SASession, session_id: str, user: User) -> RedirectResponse` — called from `auth_routes.google_callback` via a local import (avoids a circular import: `billing_routes` already imports from `auth_routes`).

- [ ] **Step 1: Check where Google OAuth tests currently live**

Run: `grep -rn "google_callback\|/auth/google" tests/`

If a `test_google_oauth` or similar exists in a file other than `tests/test_billing.py`, add this task's new test there instead, following that file's existing fixture patterns (mocking `oauth.google.authorize_access_token`). Otherwise, add to `tests/test_billing.py`.

- [ ] **Step 2: Write the failing test**

```python
def test_google_login_links_anonymous_subscription(client, session, monkeypatch):
    """Le compte Google verifie recupere l'abonnement paye anonymement,
    meme si son email differe de celui saisi sur Stripe (design
    2026-09-17-stripe-payment-first-signup: l'email Stripe n'est qu'un
    reçu de facturation, pas une identite)."""
    session.add(Subscription(
        id="sub_anon", user_id=None, stripe_customer_id="cus_anon",
        plan_name="flex", status="active",
        current_period_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
    ))
    session.commit()
    monkeypatch.setattr(settings, "google_client_id", "fake-client-id")
    monkeypatch.setattr(settings, "google_client_secret", "fake-secret")
    monkeypatch.setattr(
        billing_routes.stripe.checkout.Session, "retrieve",
        lambda session_id, expand=None: _fake_checkout_session(email="stripe-email@example.com"),
    )

    import src.api.auth_routes as auth_routes_module

    async def fake_authorize_access_token(request):
        return {"userinfo": {"sub": "google-sub-1", "email": "google-account@example.com", "email_verified": True}}

    monkeypatch.setattr(auth_routes_module.oauth.google, "authorize_access_token", fake_authorize_access_token)

    with client:
        client.get("/auth/google", params={"billing_session_id": "cs_test_anon"}, follow_redirects=False)
        resp = client.get("/auth/google/callback", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/vip.html"

    user = session.query(User).filter(User.email == "google-account@example.com").one()
    assert user.is_premium is True
    row = session.query(Subscription).filter(Subscription.id == "sub_anon").one()
    assert row.user_id == user.id
```

- [ ] **Step 3: Run it to verify it fails**

Run: `pytest tests/test_billing.py::test_google_login_links_anonymous_subscription -v`
Expected: FAIL — `/auth/google` doesn't accept `billing_session_id` yet, and even if it did nothing reads it back in the callback.

- [ ] **Step 4: Update `google_login` and `google_callback`**

In `src/api/auth_routes.py`, replace `google_login` (lines 398-411):

```python
@router.get("/auth/google")
async def google_login(request: Request, next: str = "", billing_session_id: str = ""):
    if not _google_configured():
        raise HTTPException(status_code=404, detail="Connexion Google non configurée")
    # Porte par la session `oauth_state` (SessionMiddleware) le temps de
    # l'aller-retour chez Google, la redirect_uri ne pouvant pas embarquer
    # de parametre supplementaire sans que Google la rejette.
    request.session["post_login_next"] = _safe_next(next)
    # Chemin finalisation d'un paiement anonyme (frontend/finaliser-compte.html):
    # meme mecanisme que post_login_next, lu par google_callback ci-dessous.
    request.session["billing_session_id"] = billing_session_id
    redirect_uri = str(request.url_for("google_callback"))
    if (settings.cookie_secure or _forwarded_https(request)) and redirect_uri.startswith("http://"):
        # Derriere un tunnel/proxy TLS, l'app voit du http; Google, lui,
        # exige l'URI exacte declaree (https).
        redirect_uri = "https://" + redirect_uri[len("http://"):]
    return await oauth.google.authorize_redirect(request, redirect_uri)
```

Replace `google_callback` (lines 414-442), the part after `user = get_or_create_google_user(...)`:

```python
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
        # Import tardif: billing_routes importe deja _login_response depuis
        # ce module, un import en tete de fichier creerait un cycle.
        from src.api import billing_routes
        return billing_routes.attach_subscription_after_google(db, billing_session_id, user)
    dest = request.session.pop("post_login_next", "") or "/123"
    return _login_response(token_for_user(user), url=dest)
```

- [ ] **Step 5: Add `attach_subscription_after_google` to `billing_routes.py`**

Add near `_upsert_subscription` in `src/api/billing_routes.py`:

```python
def attach_subscription_after_google(db: SASession, session_id: str, user: User) -> RedirectResponse:
    """Rattache l'abonnement paye anonymement au compte Google qui vient de
    s'authentifier (chemin Google de /finaliser-compte.html, via
    auth_routes.google_callback).

    L'email Google peut differer de celui saisi sur Stripe: l'email Stripe
    n'est qu'un reçu de facturation, pas une identite -- l'abonnement suit
    le compte Google prouve par OAuth, pas l'email Stripe (design
    2026-09-17-stripe-payment-first-signup).
    """
    if not _stripe_configured():
        return RedirectResponse(url="/premium.html", status_code=303)
    try:
        checkout_session = _retrieve_paid_session(session_id)
    except HTTPException:
        return RedirectResponse(url="/premium.html", status_code=303)
    row = _find_unlinked_or_pending_subscription(db, checkout_session.get("customer"))
    if row is not None and row.user_id is None:
        row.user_id = user.id
        user.is_premium = row.status in ("active", "trialing")
        db.commit()
    return _login_response(token_for_user(user), url="/vip.html")
```

Add `RedirectResponse` to the existing `from fastapi.responses import JSONResponse, RedirectResponse` import (already updated in Task 5) — no further import change needed since `HTTPException` is already imported.

- [ ] **Step 6: Run the tests**

Run: `pytest tests/test_billing.py -v`
Expected: all PASS. Also run `pytest tests/ -v -k google` to confirm no pre-existing Google OAuth test broke (the `billing_session_id` param defaults to `""`, so a request without it behaves exactly as before).

- [ ] **Step 7: Commit**

```bash
git add src/api/auth_routes.py src/api/billing_routes.py tests/test_billing.py
git commit -m "$(cat <<'EOF'
Link anonymous Stripe payments via Google sign-in on finalize

/auth/google now threads a billing_session_id through the OAuth state
the same way it already does post_login_next; google_callback hands
off to billing_routes.attach_subscription_after_google when present,
which links the paid-but-unlinked Subscription to whichever account
Google just verified — independent of the email Stripe collected.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: `frontend/finaliser-compte.html`

**Files:**
- Create: `frontend/finaliser-compte.html`
- Modify: `src/api/main.py:583-586` (`_PAGES`)

**Interfaces:**
- Consumes: `GET /api/billing/session-info?session_id=` and `POST /api/billing/finalize` (Task 5), `GET /auth/google?billing_session_id=` (Task 6).

- [ ] **Step 1: Add the page to the whitelist**

In `src/api/main.py`, change:

```python
_PAGES = (
    "premium.html", "carte-thailande.html", "carte-bangkok.html",
    "carte-pattaya.html", "carte-phuket.html", "payant.html", "vip.html",
)
```

to:

```python
_PAGES = (
    "premium.html", "carte-thailande.html", "carte-bangkok.html",
    "carte-pattaya.html", "carte-phuket.html", "payant.html", "vip.html",
    "finaliser-compte.html",
)
```

- [ ] **Step 2: Write the page**

Create `frontend/finaliser-compte.html` — styling mirrors `_AUTH_PAGE` in `src/api/auth_routes.py:49-91` (dark card, same palette) since this page is the static-frontend equivalent of the login/register card:

```html
<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Finaliser votre compte — ThaiMonth</title>
<style>
  body { font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0;
         padding: 16px; box-sizing: border-box; }
  .card { background: #1e293b; padding: 2rem; border-radius: 8px; width: 100%; max-width: 360px; }
  h2 { margin-top: 0; }
  p.hint { color: #94a3b8; font-size: 0.9rem; }
  label { display: block; margin-top: 0.75rem; }
  input { width: 100%; padding: 0.6rem; margin-top: 0.4rem; border-radius: 4px; border: 1px solid #334155;
          background: #0f172a; color: #e2e8f0; box-sizing: border-box; }
  button, .google { display: block; width: 100%; padding: 0.6rem; border: none; border-radius: 4px;
            margin-top: 1rem; background: #3b82f6; color: white; cursor: pointer; font-weight: 600;
            text-align: center; text-decoration: none; box-sizing: border-box; font-size: 1rem; }
  .google { background: #fff; color: #1f2937; }
  .sep { text-align: center; color: #64748b; margin: 1rem 0 0; font-size: 0.85rem; }
  .error { color: #f87171; margin-top: 0.75rem; }
  .info { color: #86efac; margin-top: 0.75rem; }
</style>
</head>
<body>
  <div class="card">
    <h2>Finaliser votre compte</h2>
    <p class="hint" id="hint">Paiement confirmé. Choisissez un mot de passe pour accéder à votre compte, ou continuez avec Google.</p>
    <form id="finalize-form">
      <label for="email">Email</label>
      <input type="email" id="email" name="email" required autocomplete="email">
      <label for="password">Mot de passe</label>
      <input type="password" id="password" name="password" required autocomplete="new-password" minlength="8">
      <div class="error" id="error" style="display:none;"></div>
      <button type="submit">Créer mon compte</button>
    </form>
    <p class="sep">ou</p>
    <a class="google" id="google-link" href="#">Continuer avec Google</a>
  </div>
<script>
const params = new URLSearchParams(window.location.search);
const sessionId = params.get("session_id");
const errorEl = document.getElementById("error");
const hintEl = document.getElementById("hint");
const emailInput = document.getElementById("email");
const form = document.getElementById("finalize-form");
const googleLink = document.getElementById("google-link");

function showError(message) {
  errorEl.textContent = message;
  errorEl.style.display = "block";
}

if (!sessionId) {
  window.location.href = "/payant.html";
} else {
  googleLink.href = "/auth/google?billing_session_id=" + encodeURIComponent(sessionId);

  fetch("/api/billing/session-info?session_id=" + encodeURIComponent(sessionId))
    .then(resp => {
      if (!resp.ok) { throw new Error("session-info failed"); }
      return resp.json();
    })
    .then(data => {
      if (data.already_linked) {
        window.location.href = "/vip.html";
        return;
      }
      emailInput.value = data.email || "";
    })
    .catch(() => {
      hintEl.textContent = "";
      showError("Cette session de paiement est introuvable ou a expiré. Retournez sur la page des offres pour réessayer.");
      form.style.display = "none";
      document.querySelector(".sep").style.display = "none";
      googleLink.style.display = "none";
    });
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorEl.style.display = "none";
  const submitButton = form.querySelector("button[type=submit]");
  submitButton.disabled = true;
  try {
    const resp = await fetch("/api/billing/finalize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId,
        email: emailInput.value,
        password: document.getElementById("password").value,
      }),
    });
    if (resp.status === 409) {
      showError("Cet email est déjà associé à un compte. Utilisez une autre adresse, ou continuez avec Google.");
      return;
    }
    if (resp.status === 404) {
      showError("Le paiement est en cours de confirmation, réessayez dans un instant.");
      return;
    }
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      showError(body.detail || "Une erreur est survenue, réessayez.");
      return;
    }
    const data = await resp.json();
    window.location.href = data.redirect;
  } catch (err) {
    console.error("Erreur lors de la finalisation du compte:", err);
    showError("Une erreur est survenue, réessayez.");
  } finally {
    submitButton.disabled = false;
  }
});
</script>
</body>
</html>
```

- [ ] **Step 3: Manual verification**

This page has no automated test (static frontend, per `.claude/rules/testing.md` — frontend changes are verified in a browser, not unit tests). Verify manually once Stripe test keys are configured locally:

1. Start the API: `venv\Scripts\python.exe -m uvicorn src.api.main:app --host 127.0.0.1 --port 8000`
2. Visit `http://127.0.0.1:8000/finaliser-compte.html` with no `session_id` — confirm it redirects to `/payant.html`.
3. Visit `http://127.0.0.1:8000/finaliser-compte.html?session_id=nonexistent` — confirm it shows the "session introuvable" error and hides the form.

Full end-to-end verification (real Stripe test-mode payment → this page → account created) is a follow-up once Stripe test keys exist locally, same caveat as the original `2026-09-17-stripe-subscriptions-design.md`.

- [ ] **Step 4: Commit**

```bash
git add frontend/finaliser-compte.html src/api/main.py
git commit -m "$(cat <<'EOF'
Add frontend/finaliser-compte.html for payment-first account creation

Return page for an anonymous Stripe payment: shows the email Stripe
collected, lets the visitor set a password or continue with Google,
and handles the "email already registered" conflict.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Full regression pass

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `pytest`
Expected: all tests pass, including every test in `tests/test_billing.py` (old and new) and the rest of the suite (parser regression tests, etc. — unaffected by this change but must not have been broken incidentally).

- [ ] **Step 2: Run lint**

Run: `ruff check src scripts tests`
Expected: no errors. Fix any it reports (unused imports are the likely category here, e.g. if `RedirectResponse` or `JSONResponse` ends up imported but only used in one of the two files it was added to).

- [ ] **Step 3: Re-read the spec and confirm every section has a corresponding task**

Cross-check against `docs/superpowers/specs/2026-09-17-stripe-payment-first-signup-design.md`:
- Data model changes → Task 1
- `POST /api/checkout/create-session` → Task 2
- `POST /api/webhooks/stripe` → Task 3
- `GET /billing/success` → Task 4
- `POST /api/billing/finalize` (password path) → Task 5
- Google path → Task 6
- `frontend/finaliser-compte.html` → Task 7
- Error handling (400/404/409/422 cases) → covered across Tasks 5-7's tests

If a gap turns up, write the missing task before considering the plan finished — do not implement it ad hoc outside the plan.
