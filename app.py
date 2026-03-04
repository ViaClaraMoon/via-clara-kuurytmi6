import os
import secrets
from datetime import datetime

import psycopg2
import stripe
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI()


# -------------------------
# Environment
# -------------------------
DATABASE_URL = os.getenv("DATABASE_URL")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID_MONTHLY = os.getenv("STRIPE_PRICE_ID_MONTHLY")
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
            active BOOLEAN DEFAULT TRUE,
            stripe_session_id TEXT UNIQUE,
            stripe_subscription_id TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """
    )
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


@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <h2>Via Clara – Kuurytmi</h2>
    <p><a href="/buy-monthly">Aloita 7 päivän kokeilu (3.99€/kk)</a></p>
    """


@app.get("/buy-monthly")
def buy_monthly():
    # Stripe settings
    stripe.api_key = STRIPE_SECRET_KEY

    # IMPORTANT:
    # - mode MUST be "subscription"
    # - line_items MUST use "price": "<price_id>" for recurring prices
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{
            "price": STRIPE_PRICE_ID_MONTHLY,
            "quantity": 1,
        }],
        success_url=f"{BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{BASE_URL}/cancel",
        allow_promotion_codes=True,
    )
    return {"url": session.url}


@app.get("/cancel", response_class=HTMLResponse)
def cancel():
    return "<h3>Peruit maksun.</h3>"


@app.get("/thanks", response_class=HTMLResponse)
def thanks(session_id: str):
    # Odotetaan että token syntyy jo tässä (ilman webhookia)
    token = secrets.token_urlsafe(16)

    conn = get_connection()
    cur = conn.cursor()
    # Jos session_id on jo olemassa, käytetään samaa tokenia
    cur.execute("SELECT token FROM tokens WHERE stripe_session_id = %s", (session_id,))
    row = cur.fetchone()
    if row:
        token = row[0]
    else:
        cur.execute(
            "INSERT INTO tokens (token, active, stripe_session_id) VALUES (%s, TRUE, %s)",
            (token, session_id),
        )
        conn.commit()
    cur.close()
    conn.close()

    cal_url = f"{BASE_URL}/calendar/{token}.ics"
    return HTMLResponse(
        f"""
        <h2>Kiitos! 🌙</h2>
        <p>Tässä on henkilökohtainen kalenterilinkkisi:</p>
        <p><a href="{cal_url}">{cal_url}</a></p>
        <p><b>Google Kalenteri:</b> Asetukset → Lisää kalenteri → URL → liitä linkki</p>
        """
    )


@app.get("/calendar/{token}.ics")
def calendar_ics(token: str):
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
@app.get("/success", response_class=HTMLResponse)
def success(session_id: str):
    # 1) Haetaan Checkout Session Stripeltä
    session = stripe.checkout.Session.retrieve(session_id)

    # 2) Varmistetaan että maksu / tilaus on OK
    # (subscription-moodissa payment_status on yleensä 'paid' kun ensimmäinen maksu onnistui)
    if session.get("payment_status") != "paid":
        return HTMLResponse(
            "<h2>Maksu ei vielä vahvistunut.</h2><p>Odota hetki ja päivitä sivu.</p>",
            status_code=402,
        )

    # 3) Luodaan token ja palautetaan kalenterilinkki
    data = create_token()
    cal_url = data["calendar_url"]

    html = f"""
    <h2>Kiitos! Tilauksesi on aktiivinen ✅</h2>
    <p>Tässä sinun henkilökohtainen kalenterilinkki:</p>
    <p><a href="{cal_url}">{cal_url}</a></p>
    <p>Lisää se Google Kalenteriin: Asetukset → Lisää kalenteri → URL-osoitteesta.</p>
    """
    return HTMLResponse(html)

@app.get("/cancel", response_class=HTMLResponse)
def cancel():
    return HTMLResponse("<h2>Peruit maksun.</h2><p>Voit yrittää uudelleen milloin vain.</p>")
