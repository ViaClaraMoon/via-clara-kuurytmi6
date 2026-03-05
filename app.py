import os
import secrets
from datetime import datetime

import psycopg2
import stripe
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

app = FastAPI()

# -------------------------
# Environment
# -------------------------
DATABASE_URL = os.getenv("DATABASE_URL")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID_MONTHLY = os.getenv("STRIPE_PRICE_ID_MONTHLY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

BASE_URL = os.getenv("BASE_URL", "https://via-clara-kuurytmi6.onrender.com")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


# -------------------------
# Database helpers
# -------------------------
def get_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tokens (
            id SERIAL PRIMARY KEY,
            token TEXT UNIQUE NOT NULL,
            active BOOLEAN DEFAULT TRUE
        );
        """
    )

    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS stripe_session_id TEXT;")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT;")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();")

    conn.commit()
    cur.close()
    conn.close()


def ensure_tokens_schema():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS stripe_session_id TEXT;")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT;")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();")
    conn.commit()
    cur.close()
    conn.close()


@app.on_event("startup")
def startup():
    init_db()


# -------------------------
# Public endpoints
# -------------------------
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/debug/token/{token}")
def debug_token(token: str):
    ensure_tokens_schema()

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT active, stripe_session_id, stripe_subscription_id, created_at FROM tokens WHERE token = %s",
        (token,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="token not found")

    active, sess_id, sub_id, created_at = row
    return {
        "token": token,
        "active": active,
        "stripe_session_id": sess_id,
        "stripe_subscription_id": sub_id,
        "created_at": str(created_at),
    }


@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <h2>Via Clara – Kuurytmi</h2>
    <p><a href="/buy-monthly">Aloita 7 päivän kokeilu (3.99€/kk)</a></p>
    """


@app.get("/buy-monthly")
def buy_monthly():
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="STRIPE_SECRET_KEY is not set")
    if not STRIPE_PRICE_ID_MONTHLY:
        raise HTTPException(status_code=500, detail="STRIPE_PRICE_ID_MONTHLY is not set")

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[
            {
                "price": STRIPE_PRICE_ID_MONTHLY,
                "quantity": 1,
            }
        ],
        success_url=f"{BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{BASE_URL}/cancel",
        allow_promotion_codes=True,
    )

    return RedirectResponse(session.url, status_code=303)


@app.get("/success", response_class=HTMLResponse)
def success(session_id: str):
    """
    Stripe ohjaa tänne muodossa:
    /success?session_id={CHECKOUT_SESSION_ID}
    """
    ensure_tokens_schema()

    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    try:
        session = stripe.checkout.Session.retrieve(session_id, expand=["subscription"])
        subscription = session.get("subscription")
        if isinstance(subscription, dict):
            subscription_id = subscription.get("id")
        else:
            subscription_id = subscription
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid session_id")

    token = secrets.token_urlsafe(16)

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT token FROM tokens WHERE stripe_session_id = %s", (session_id,))
    row = cur.fetchone()

    if row:
        token = row[0]
        cur.execute(
            "UPDATE tokens SET stripe_subscription_id = COALESCE(stripe_subscription_id, %s) "
            "WHERE stripe_session_id = %s",
            (subscription_id, session_id),
        )
        conn.commit()
    else:
        cur.execute(
            "INSERT INTO tokens (token, active, stripe_session_id, stripe_subscription_id) "
            "VALUES (%s, TRUE, %s, %s)",
            (token, session_id, subscription_id),
        )
        conn.commit()

    cur.close()
    conn.close()

    cal_url = f"{BASE_URL}/calendar/{token}.ics"

    return HTMLResponse(
        f"""
        <h2>Kiitos! Tilauksesi on käsitelty ✅</h2>
        <p>Tässä on henkilökohtainen kalenterilinkkisi:</p>
        <p><a href="{cal_url}">{cal_url}</a></p>
        <p><b>Google Kalenteri:</b> Asetukset → Lisää kalenteri → URL → liitä linkki</p>
        """
    )


@app.get("/cancel", response_class=HTMLResponse)
def cancel():
    return HTMLResponse("<h2>Peruit maksun.</h2><p>Voit yrittää uudelleen milloin vain.</p>")


@app.get("/calendar/{token}.ics")
def calendar_ics(token: str):
    ensure_tokens_schema()

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT active FROM tokens WHERE token = %s", (token,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row or not row[0]:
        raise HTTPException(status_code=403, detail="Invalid or inactive token")

    now_utc = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Via Clara//Kuurytmi Backend//FI
CALSCALE:GREGORIAN
METHOD:PUBLISH
X-WR-CALNAME:Via Clara – Kuurytmi
BEGIN:VEVENT
DTSTAMP:{now_utc}
UID:{token}@via-clara
DTSTART;VALUE=DATE:20260301
DTEND;VALUE=DATE:20260302
SUMMARY:🌙 Via Clara Kuurytmi aktiivinen
END:VEVENT
END:VCALENDAR
"""
    return Response(content=ics, media_type="text/calendar; charset=utf-8")


# -------------------------
# Stripe webhook
# -------------------------
@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET is not set")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event_type = event["type"]

    if event_type == "invoice.payment_failed":
        sub_id = event["data"]["object"].get("subscription")
        if sub_id:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                "UPDATE tokens SET active = FALSE WHERE stripe_subscription_id = %s",
                (sub_id,),
            )
            conn.commit()
            cur.close()
            conn.close()

    if event_type == "customer.subscription.deleted":
        sub_id = event["data"]["object"].get("id")
        if sub_id:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                "UPDATE tokens SET active = FALSE WHERE stripe_subscription_id = %s",
                (sub_id,),
            )
            conn.commit()
            cur.close()
            conn.close()
            
  @app.post("/stripe/webhook")
async def stripe_webhook(request: Request):

    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET is not set")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event_type = event["type"]

    if event_type == "customer.subscription.updated":
        sub = event["data"]["object"]
        sub_id = sub.get("id")
        status = sub.get("status")
        cancel_at_period_end = sub.get("cancel_at_period_end")
        canceled_at = sub.get("canceled_at")

        if sub_id and (status == "canceled" or cancel_at_period_end or canceled_at):
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                "UPDATE tokens SET active = FALSE WHERE stripe_subscription_id = %s",
                (sub_id,),
            )
            conn.commit()
            cur.close()
            conn.close()

    return {"ok": True}
   

