import os
import secrets
from datetime import datetime, timedelta, timezone

import psycopg2
import stripe
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from icalendar import Calendar, Event
from skyfield.api import load
from skyfield import almanac

app = FastAPI()

# -------------------------
# Environment
# -------------------------
DATABASE_URL = os.getenv("DATABASE_URL")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID_MONTHLY = os.getenv("STRIPE_PRICE_ID_MONTHLY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

DEBUG = os.getenv("DEBUG", "false").lower() == "true"

BASE_URL = os.getenv("BASE_URL", "https://via-clara-kuurytmi6.onrender.com")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


# -------------------------
# Moon phases (.ics generation)
# -------------------------
PHASE_LABELS = {
    "new_moon": "🌑 Uusikuu",
    "first_quarter": "🌓 Ensimmäinen neljännes",
    "full_moon": "🌕 Täysikuu",
    "last_quarter": "🌗 Viimeinen neljännes",
}


def generate_moon_phase_events(start_dt_utc: datetime):
    """
    Palauttaa listan (title, datetime_utc) kuun vaiheista ~12 kk eteenpäin.
    Datetimet ovat UTC-aikavyöhykkeessä.
    """
    # Skyfield setup
    ts = load.timescale()
    eph = load("de421.bsp")  # comes via skyfield-data

    t0 = ts.from_datetime(start_dt_utc)
    t1 = ts.from_datetime(start_dt_utc + timedelta(days=365))

    f = almanac.moon_phases(eph)
    times, phases = almanac.find_discrete(t0, t1, f)

    # phases: 0 new, 1 first quarter, 2 full, 3 last quarter
    phase_name = {0: "new_moon", 1: "first_quarter", 2: "full_moon", 3: "last_quarter"}

    out = []
    for t, p in zip(times, phases):
        name = phase_name[int(p)]
        title = PHASE_LABELS[name]
        dt = t.utc_datetime().replace(tzinfo=timezone.utc)
        out.append((title, dt))
    return out


def build_ics_for_token(token: str) -> bytes:
    """
    Rakentaa RFC5545 .ics -sisällön (bytes) kuun vaiheista seuraavalle 12 kuukaudelle.
    """
    now_utc = datetime.now(timezone.utc)

    cal = Calendar()
    cal.add("prodid", "-//Via Clara//Kuurytmi Backend//FI")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", "Via Clara – Kuurytmi")
    cal.add("x-wr-timezone", "UTC")

    for title, dt in generate_moon_phase_events(now_utc):
        ev = Event()
        ev.add("uid", f"{token}-{int(dt.timestamp())}@via-clara")
        ev.add("summary", title)
        # Kuun vaihe on hetki ajassa; näytetään 15 min tapahtumana
        ev.add("dtstart", dt)
        ev.add("dtend", dt + timedelta(minutes=15))
        ev.add("dtstamp", now_utc)
        cal.add_component(ev)

    return cal.to_ical()


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
    # Debug näkyviin vain jos DEBUG=true
    if not DEBUG:
        raise HTTPException(status_code=404, detail="Not found")

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
        # Sama käytös kuin ennen: estä jos token puuttuu tai inactive
        raise HTTPException(status_code=403, detail="Invalid or inactive token")

    ics_bytes = build_ics_for_token(token)

    return Response(
        content=ics_bytes,
        media_type="text/calendar; charset=utf-8",
        headers={
            # auttaa selaimia ja joitain kalentericlienttejä
            "Content-Disposition": f'inline; filename="kuurytmi-{token}.ics"',
            # Google Calendar hakee feedin harvoin; tämä vähentää turhia hittejä
            "Cache-Control": "public, max-age=3600",
        },
    )


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

    # Maksu epäonnistui -> katkaise pääsy heti
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

    # Maksu onnistui -> palauta pääsy (varmistetaan sekä payment_succeeded että paid)
    if event_type in ["invoice.payment_succeeded", "invoice.paid"]:
        sub_id = event["data"]["object"].get("subscription")
        if sub_id:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                "UPDATE tokens SET active = TRUE WHERE stripe_subscription_id = %s",
                (sub_id,),
            )
            conn.commit()
            cur.close()
            conn.close()

    # Tilaus poistettu -> katkaise
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

    # B-malli: katkaise vasta kun oikeasti canceled (ei pelkästä cancel_at_period_end:stä)
    if event_type == "customer.subscription.updated":
        sub = event["data"]["object"]
        sub_id = sub.get("id")
        status = sub.get("status")
        canceled_at = sub.get("canceled_at")

        if sub_id and (status == "canceled" or canceled_at is not None):
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
