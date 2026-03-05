import os
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
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

DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Europe/Helsinki")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


# -------------------------
# Moon / Zodiac (emoji-only)
# -------------------------
# Index 0..11 corresponds to 0..360 degrees of ecliptic longitude in 30-degree slices.
# Name stored internally (not shown), emoji used in calendar.
ZODIAC_SIGNS = [
    ("Oinas", "🐏🔥"),
    ("Härkä", "🐂🌍"),
    ("Kaksoset", "👯🌬"),
    ("Rapu", "🦀💧"),
    ("Leijona", "🦁🔥"),
    ("Neitsyt", "👧🌍"),
    ("Vaaka", "⚖️🌬"),
    ("Skorpioni", "🦂💧"),
    ("Jousimies", "🏹🔥"),
    ("Kauris", "🐐🌍"),
    ("Vesimies", "🏺🌬"),
    ("Kalat", "🐟💧"),
]


def plant_emoji_from_sign(sign_emoji: str) -> str:
    """
    Element -> plant/day emoji:
    💧 -> 🥬 (leaf)
    🌍 -> 🥕 (root)
    🌬 -> 🌸 (flower)
    🔥 -> 🍓 (fruit/seed)
    """
    if "💧" in sign_emoji:
        return "🥬"
    if "🌍" in sign_emoji:
        return "🥕"
    if "🌬" in sign_emoji:
        return "🌸"
    if "🔥" in sign_emoji:
        return "🍓"
    return "🌿"


def fmt_hhmm(dt_local: datetime) -> str:
    return dt_local.strftime("%H:%M")


def moon_sign_index_at(eph, ts, dt_utc: datetime) -> int:
    """Return sign index 0..11 for the moon at dt_utc (UTC datetime)."""
    t = ts.from_datetime(dt_utc)
    astrometric = eph["earth"].at(t).observe(eph["moon"]).apparent()
    lon = astrometric.ecliptic_latlon()[1]
    deg = lon.degrees % 360.0
    return int(deg // 30)


def build_ics_for_token(token: str, tz_name: str) -> bytes:
    """
    Calendar contains ONLY:
      - New Moon: 🌑 ✂️⬆️ + sign/element emoji + plant emoji + time
      - Full Moon: 🌕 ✂️⬇️ + sign/element emoji + plant emoji + time
      - Moon sign ingresses: sign/element emoji + plant emoji + time
    For ~12 months ahead, in token's timezone.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(DEFAULT_TIMEZONE)
        tz_name = DEFAULT_TIMEZONE

    now_utc = datetime.now(timezone.utc)
    ts = load.timescale()
    eph = load("de421.bsp")

    t0 = ts.from_datetime(now_utc)
    t1 = ts.from_datetime(now_utc + timedelta(days=365))

    cal = Calendar()
    cal.add("prodid", "-//Via Clara//Kuurytmi Backend//FI")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", "Via Clara – Kuurytmi")
    cal.add("x-wr-timezone", tz_name)

    # -------------------------
    # A) New Moon + Full Moon only
    # -------------------------
    phase_func = almanac.moon_phases(eph)
    phase_times, phase_ids = almanac.find_discrete(t0, t1, phase_func)

    for t, pid in zip(phase_times, phase_ids):
        pid = int(pid)
        if pid not in (0, 2):
            continue  # only new + full

        dt_utc = t.utc_datetime().replace(tzinfo=timezone.utc)
        dt_local = dt_utc.astimezone(tz)
        time_str = fmt_hhmm(dt_local)

        idx = moon_sign_index_at(eph, ts, dt_utc)
        _, sign_emoji = ZODIAC_SIGNS[idx]
        plant = plant_emoji_from_sign(sign_emoji)

        if pid == 0:
            summary = f"🌑 ✂️⬆️ {sign_emoji} {plant} {time_str}"
            uid_prefix = "new"
            duration = timedelta(minutes=15)
        else:
            summary = f"🌕 ✂️⬇️ {sign_emoji} {plant} {time_str}"
            uid_prefix = "full"
            duration = timedelta(minutes=15)

        ev = Event()
        ev.add("uid", f"{token}-{uid_prefix}-{int(dt_utc.timestamp())}@via-clara")
        ev.add("summary", summary)
        ev.add("description", summary)  # emoji-only
        ev.add("dtstart", dt_local)
        ev.add("dtend", dt_local + duration)
        ev.add("dtstamp", now_utc)
        cal.add_component(ev)

    # -------------------------
    # B) Moon sign ingresses (emoji-only)
    # -------------------------
    def moon_sign_index_vector(t_skyfield):
        astrometric = eph["earth"].at(t_skyfield).observe(eph["moon"]).apparent()
        lon = astrometric.ecliptic_latlon()[1]
        deg = lon.degrees % 360.0
        return np.floor_divide(deg, 30).astype(int)

    ingress_times, ingress_idxs = almanac.find_discrete(t0, t1, moon_sign_index_vector)

    for t, idx in zip(ingress_times, ingress_idxs):
        dt_utc = t.utc_datetime().replace(tzinfo=timezone.utc)
        dt_local = dt_utc.astimezone(tz)
        time_str = fmt_hhmm(dt_local)

        _, sign_emoji = ZODIAC_SIGNS[int(idx)]
        plant = plant_emoji_from_sign(sign_emoji)

        summary = f"{sign_emoji} {plant} {time_str}"

        ev = Event()
        ev.add("uid", f"{token}-ingress-{int(dt_utc.timestamp())}@via-clara")
        ev.add("summary", summary)
        ev.add("description", summary)
        ev.add("dtstart", dt_local)
        ev.add("dtend", dt_local + timedelta(minutes=10))
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

    # schema upgrades
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS stripe_session_id TEXT;")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT;")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS timezone TEXT;")

    conn.commit()
    cur.close()
    conn.close()


def ensure_tokens_schema():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS stripe_session_id TEXT;")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT;")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS timezone TEXT;")
    conn.commit()
    cur.close()
    conn.close()


def get_token_timezone(token: str) -> str:
    """Return token timezone (or default)."""
    ensure_tokens_schema()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT timezone FROM tokens WHERE token = %s", (token,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row or not row[0]:
        return DEFAULT_TIMEZONE
    return row[0]


def set_token_timezone(token: str, tz: str):
    """Set token timezone (validate by ZoneInfo)."""
    # validate tz
    try:
        ZoneInfo(tz)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid timezone")

    ensure_tokens_schema()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE tokens SET timezone = %s WHERE token = %s", (tz, token))
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
        "SELECT active, stripe_session_id, stripe_subscription_id, created_at, timezone FROM tokens WHERE token = %s",
        (token,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="token not found")

    active, sess_id, sub_id, created_at, tz = row
    return {
        "token": token,
        "active": active,
        "stripe_session_id": sess_id,
        "stripe_subscription_id": sub_id,
        "created_at": str(created_at),
        "timezone": tz or DEFAULT_TIMEZONE,
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
        line_items=[{"price": STRIPE_PRICE_ID_MONTHLY, "quantity": 1}],
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
        subscription_id = subscription.get("id") if isinstance(subscription, dict) else subscription
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
            "INSERT INTO tokens (token, active, stripe_session_id, stripe_subscription_id, timezone) "
            "VALUES (%s, TRUE, %s, %s, %s)",
            (token, session_id, subscription_id, DEFAULT_TIMEZONE),
        )
        conn.commit()

    cur.close()
    conn.close()

    cal_url = f"{BASE_URL}/calendar/{token}.ics"
    tz_url = f"{BASE_URL}/tz?token={token}"

    return HTMLResponse(
        f"""
        <h2>Kiitos! Tilauksesi on käsitelty ✅</h2>

        <p><b>Kalenterilinkkisi:</b></p>
        <p><a href="{cal_url}">{cal_url}</a></p>

        <p><b>Aikavyöhyke:</b> {DEFAULT_TIMEZONE}</p>
        <p><a href="{tz_url}">Vaihda aikavyöhyke</a></p>

        <p><b>Google Kalenteri:</b> Asetukset → Lisää kalenteri → URL → liitä linkki</p>
        """
    )


@app.get("/tz", response_class=HTMLResponse)
def tz_form(token: str):
    """
    Simple timezone selector for a token.
    """
    ensure_tokens_schema()

    # Verify token exists (but don't leak too much)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT active, timezone FROM tokens WHERE token = %s", (token,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Not found")

    current_tz = row[1] or DEFAULT_TIMEZONE

    # Small curated list (safe + common). You can add more later.
    options = [
        "Europe/Helsinki",
        "Europe/Vienna",
        "Europe/Stockholm",
        "Europe/London",
        "Europe/Paris",
        "America/New_York",
        "America/Los_Angeles",
        "Australia/Sydney",
    ]

    option_html = "\n".join(
        f'<option value="{tz}" {"selected" if tz == current_tz else ""}>{tz}</option>'
        for tz in options
    )

    return HTMLResponse(
        f"""
        <h2>Aikavyöhyke</h2>
        <p>Token: <code>{token}</code></p>
        <form method="post" action="/tz">
            <input type="hidden" name="token" value="{token}" />
            <label for="timezone">Valitse aikavyöhyke:</label><br/>
            <select id="timezone" name="timezone">
                {option_html}
            </select>
            <p style="margin-top:12px;">
                <button type="submit">Tallenna</button>
            </p>
        </form>
        <p>Huom: Google Calendar voi päivittää URL-kalenterin viiveellä.</p>
        """
    )


@app.post("/tz", response_class=HTMLResponse)
async def tz_save(request: Request):
    """
    Save timezone selection.
    """
    form = await request.form()
    token = (form.get("token") or "").strip()
    tz = (form.get("timezone") or "").strip()

    if not token or not tz:
        raise HTTPException(status_code=400, detail="Missing token or timezone")

    # Ensure token exists
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM tokens WHERE token = %s", (token,))
    exists = cur.fetchone()
    cur.close()
    conn.close()
    if not exists:
        raise HTTPException(status_code=404, detail="Not found")

    set_token_timezone(token, tz)

    cal_url = f"{BASE_URL}/calendar/{token}.ics"
    return HTMLResponse(
        f"""
        <h2>Tallennettu ✅</h2>
        <p>Aikavyöhyke: <b>{tz}</b></p>
        <p>Kalenterilinkki:</p>
        <p><a href="{cal_url}">{cal_url}</a></p>
        <p>Huom: Google Calendar voi päivittää URL-kalenterin viiveellä.</p>
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

    tz_name = get_token_timezone(token)
    ics_bytes = build_ics_for_token(token, tz_name)

    return Response(
        content=ics_bytes,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'inline; filename="kuurytmi-{token}.ics"',
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

    # Maksu onnistui -> palauta pääsy
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

    # Katkaise vasta kun oikeasti canceled (ei pelkästä cancel_at_period_end:stä)
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
