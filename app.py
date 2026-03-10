import html as py_html
import logging
import os
import secrets
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import psycopg2
import resend
import stripe
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from icalendar import Calendar, Event
from skyfield import almanac
from skyfield.api import Loader

app = FastAPI()
logger = logging.getLogger(__name__)

# -------------------------
# Environment
# -------------------------
DATABASE_URL = os.getenv("DATABASE_URL")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID_MONTHLY = os.getenv("STRIPE_PRICE_ID_MONTHLY")
STRIPE_PRICE_ID_YEARLY = os.getenv("STRIPE_PRICE_ID_YEARLY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_FROM", "Via Clara <noreply@mail.viaclara.fi>")

DEBUG = os.getenv("DEBUG", "false").lower() == "true"

BASE_URL = os.getenv("BASE_URL", "https://via-clara-kuurytmi6.onrender.com")
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Europe/Helsinki")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

# -------------------------
# Skyfield preload (cache in /tmp)
# -------------------------
SKYFIELD_DIR = os.getenv("SKYFIELD_DIR", "/tmp/skyfield")
os.makedirs(SKYFIELD_DIR, exist_ok=True)

_sky_loader = Loader(SKYFIELD_DIR)
TS = None
EPH = None

# -------------------------
# In-memory ICS cache
# -------------------------
ICS_CACHE = {}
ICS_CACHE_TTL_SECONDS = 3600  # 1 hour

# -------------------------
# Zodiac data
# -------------------------
ZODIAC_SIGNS = [
    ("Oinas", "🐏", "🔥"),
    ("Härkä", "🐂", "🌍"),
    ("Kaksoset", "👯", "🌬"),
    ("Rapu", "🦀", "💧"),
    ("Leijona", "🦁", "🔥"),
    ("Neitsyt", "👧", "🌍"),
    ("Vaaka", "⚖️", "🌬"),
    ("Skorpioni", "🦂", "💧"),
    ("Jousimies", "🏹", "🔥"),
    ("Kauris", "🐐", "🌍"),
    ("Vesimies", "🏺", "🌬"),
    ("Kalat", "🐟", "💧"),
]


def plant_emoji_from_element(element_emoji: str) -> str:
    if element_emoji == "💧":
        return "🌿"
    if element_emoji == "🌍":
        return "🥕"
    if element_emoji == "🌬":
        return "🌸"
    if element_emoji == "🔥":
        return "🍓"
    return "🌿"


def fmt_hhmm(dt_local: datetime) -> str:
    return dt_local.strftime("%H:%M")


def moon_sign_index_at(eph, ts, dt_utc: datetime) -> int:
    t = ts.from_datetime(dt_utc)
    astrometric = eph["earth"].at(t).observe(eph["moon"]).apparent()
    lon = astrometric.ecliptic_latlon()[1]
    deg = lon.degrees % 360.0
    return int(deg // 30)


def sign_parts_from_index(idx: int):
    _, sign_emoji, element_emoji = ZODIAC_SIGNS[int(idx)]
    plant_emoji = plant_emoji_from_element(element_emoji)
    return sign_emoji, element_emoji, plant_emoji


def get_cached_ics(token: str, tz_name: str):
    key = f"{token}|{tz_name}"
    item = ICS_CACHE.get(key)
    if not item:
        return None

    created_at = item["created_at"]
    if time.time() - created_at > ICS_CACHE_TTL_SECONDS:
        ICS_CACHE.pop(key, None)
        return None

    return item["content"]


def set_cached_ics(token: str, tz_name: str, content: bytes):
    key = f"{token}|{tz_name}"
    ICS_CACHE[key] = {
        "created_at": time.time(),
        "content": content,
    }


def invalidate_token_cache(token: str):
    keys_to_delete = [k for k in ICS_CACHE if k.startswith(f"{token}|")]
    for k in keys_to_delete:
        ICS_CACHE.pop(k, None)


def set_token_active_by_subscription_id(subscription_id: str, is_active: bool):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE tokens SET active = %s WHERE stripe_subscription_id = %s",
        (is_active, subscription_id),
    )
    conn.commit()
    cur.close()
    conn.close()


def build_ics_for_token(token: str, tz_name: str) -> bytes:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(DEFAULT_TIMEZONE)
        tz_name = DEFAULT_TIMEZONE

    ts = TS
    eph = EPH
    if ts is None or eph is None:
        raise RuntimeError("Skyfield not initialized")

    now_utc = datetime.now(timezone.utc)
    today_local = datetime.now(tz).date()
    days_ahead = 365
    days_past = 180

    local_start = datetime.combine(today_local - timedelta(days=days_past), dt_time(0, 0), tzinfo=tz)
    local_end = datetime.combine(today_local + timedelta(days=days_ahead), dt_time(0, 0), tzinfo=tz)

    search_start_utc = (local_start - timedelta(days=2)).astimezone(timezone.utc)
    search_end_utc = (local_end + timedelta(days=2)).astimezone(timezone.utc)

    t0 = ts.from_datetime(search_start_utc)
    t1 = ts.from_datetime(search_end_utc)

    cal = Calendar()
    cal.add("prodid", "-//Via Clara//Kuurytmi Backend//FI")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", "Via Clara – Kuurytmi")
    cal.add("x-wr-timezone", tz_name)

    valid_dates = {today_local + timedelta(days=i) for i in range(-days_past, days_ahead)}

    phase_by_date = {}

    phase_func = almanac.moon_phases(eph)
    phase_times, phase_ids = almanac.find_discrete(t0, t1, phase_func)

    for t, pid in zip(phase_times, phase_ids):
        pid = int(pid)
        if pid not in (0, 2):
            continue

        dt_utc = t.utc_datetime().replace(tzinfo=timezone.utc)
        dt_local = dt_utc.astimezone(tz)
        local_day = dt_local.date()

        if local_day not in valid_dates:
            continue

        phase_by_date[local_day] = {
            "emoji": "🌚" if pid == 0 else "🌕",
            "time": fmt_hhmm(dt_local),
            "arrow": "⬆️" if pid == 0 else "⬇️",
        }

    ingress_by_date = {}

    def moon_sign_index_vector(t_skyfield):
        astrometric = eph["earth"].at(t_skyfield).observe(eph["moon"]).apparent()
        lon = astrometric.ecliptic_latlon()[1]
        deg = lon.degrees % 360.0
        return np.floor_divide(deg, 30).astype(int)

    moon_sign_index_vector.step_days = 0.5

    ingress_times, ingress_idxs = almanac.find_discrete(t0, t1, moon_sign_index_vector)

    for t, idx in zip(ingress_times, ingress_idxs):
        dt_utc = t.utc_datetime().replace(tzinfo=timezone.utc)
        dt_local = dt_utc.astimezone(tz)
        local_day = dt_local.date()

        if local_day not in valid_dates:
            continue

        ingress_by_date[local_day] = {
            "idx": int(idx),
            "time": fmt_hhmm(dt_local),
        }

    for i in range(-days_past, days_ahead):
        local_day = today_local + timedelta(days=i)

        ingress = ingress_by_date.get(local_day)
        if ingress:
            sign_idx = ingress["idx"]
            sign_time = ingress["time"]
        else:
            local_noon = datetime.combine(local_day, dt_time(12, 0), tzinfo=tz)
            dt_utc = local_noon.astimezone(timezone.utc)
            sign_idx = moon_sign_index_at(eph, ts, dt_utc)
            sign_time = None

        sign_emoji, element_emoji, plant_emoji = sign_parts_from_index(sign_idx)
        phase = phase_by_date.get(local_day)

        if phase:
            if sign_time:
                summary = (
                    f'{phase["emoji"]} {phase["time"]} {phase["arrow"]} '
                    f'{sign_emoji} {sign_time} {element_emoji} {plant_emoji}'
                )
            else:
                summary = (
                    f'{phase["emoji"]} {phase["time"]} {phase["arrow"]} '
                    f'{sign_emoji}{element_emoji} {plant_emoji}'
                )
        else:
            if sign_time:
                summary = f"{sign_emoji} {sign_time} {element_emoji} {plant_emoji}"
            else:
                summary = f"{sign_emoji}{element_emoji} {plant_emoji}"

        ev = Event()
        ev.add("uid", f"{token}-{local_day.isoformat()}@via-clara")
        ev.add("summary", summary)
        ev.add("description", summary)
        ev.add("dtstart", local_day)
        ev.add("dtend", local_day + timedelta(days=1))
        ev.add("dtstamp", now_utc)
        cal.add_component(ev)

    return cal.to_ical()


# -------------------------
# Email helpers
# -------------------------
def build_calendar_email_html(calendar_url: str, tz_url: str, portal_url: str) -> str:
    calendar_url_esc = py_html.escape(calendar_url, quote=True)
    tz_url_esc = py_html.escape(tz_url, quote=True)
    portal_url_esc = py_html.escape(portal_url, quote=True)

    return f"""
    <html>
      <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #222;">
        <h2>Kiitos tilauksestasi – Via Clara Kuurytmi</h2>

        <p>Tässä henkilökohtainen kalenterilinkkisi:</p>

        <p>
          <a href="{calendar_url_esc}">{calendar_url_esc}</a>
        </p>

        <p><strong>Lisää kalenteriin näin:</strong></p>

        <ul>
          <li><strong>Google Kalenteri:</strong> Asetukset → Lisää kalenteri → URL-osoitteesta → liitä linkki</li>
          <li><strong>Apple Calendar:</strong> File → New Calendar Subscription → liitä linkki</li>
          <li><strong>Outlook:</strong> Add calendar → Subscribe from web → liitä linkki</li>
        </ul>

        <p>
          <strong>Vaihda aikavyöhyke:</strong><br>
          <a href="{tz_url_esc}">{tz_url_esc}</a>
        </p>

        <p>
          <strong>Hallitse tilaustasi:</strong><br>
          <a href="{portal_url_esc}">{portal_url_esc}</a>
        </p>

        <p>
          Tallenna tämä viesti, koska kalenterilinkki on henkilökohtainen.
        </p>

        <p>Lämpimin terveisin,<br>Via Clara</p>
      </body>
    </html>
    """


def build_calendar_email_text(calendar_url: str, tz_url: str, portal_url: str) -> str:
    return "\n".join(
        [
            "Kiitos tilauksestasi – Via Clara Kuurytmi",
            "",
            "Tässä henkilökohtainen kalenterilinkkisi:",
            calendar_url,
            "",
            "Lisää kalenteriin näin:",
            "Google Kalenteri: Asetukset -> Lisää kalenteri -> URL-osoitteesta -> liitä linkki",
            "Apple Calendar: File -> New Calendar Subscription -> liitä linkki",
            "Outlook: Add calendar -> Subscribe from web -> liitä linkki",
            "",
            "Vaihda aikavyöhyke:",
            tz_url,
            "",
            "Hallitse tilaustasi:",
            portal_url,
            "",
            "Tallenna tämä viesti, koska kalenterilinkki on henkilökohtainen.",
            "",
            "Via Clara",
        ]
    )


def send_calendar_email(to_email: str, calendar_url: str, tz_url: str, portal_url: str):
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY puuttuu, sähköpostia ei lähetetty.")
        return None

    params = {
        "from": EMAIL_FROM,
        "to": [to_email],
        "subject": "Via Clara Kuurytmi – henkilökohtainen kalenterilinkkisi",
        "html": build_calendar_email_html(calendar_url, tz_url, portal_url),
        "text": build_calendar_email_text(calendar_url, tz_url, portal_url),
    }

    return resend.Emails.send(params)


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
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT;")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS timezone TEXT;")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS customer_email TEXT;")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS welcome_email_sent_at TIMESTAMP NULL;")

    conn.commit()
    cur.close()
    conn.close()


def ensure_tokens_schema():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS stripe_session_id TEXT;")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT;")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT;")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS timezone TEXT;")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS customer_email TEXT;")
    cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS welcome_email_sent_at TIMESTAMP NULL;")
    conn.commit()
    cur.close()
    conn.close()


def get_token_timezone(token: str) -> str:
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
    global TS, EPH
    init_db()
    TS = _sky_loader.timescale()
    EPH = _sky_loader("de421.bsp")


# -------------------------
# Public endpoints
# -------------------------
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/debug/token/{token}")
def debug_token(token: str):
    if not DEBUG:
        raise HTTPException(status_code=404, detail="Not found")

    ensure_tokens_schema()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT active, stripe_session_id, stripe_subscription_id, stripe_customer_id, created_at, timezone, customer_email, welcome_email_sent_at "
        "FROM tokens WHERE token = %s",
        (token,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="token not found")

    active, sess_id, sub_id, customer_id, created_at, tz, customer_email, welcome_email_sent_at = row
    return {
        "token": token,
        "active": active,
        "stripe_session_id": sess_id,
        "stripe_subscription_id": sub_id,
        "stripe_customer_id": customer_id,
        "created_at": str(created_at),
        "timezone": tz or DEFAULT_TIMEZONE,
        "customer_email": customer_email,
        "welcome_email_sent_at": str(welcome_email_sent_at) if welcome_email_sent_at else None,
    }


@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <h2>Via Clara – Kuurytmi</h2>
    <p><a href="/buy-monthly">Tilaa kuukausittain (3,99€/kk)</a></p>
    <p><a href="/buy-yearly">Tilaa vuodeksi (34,90€/vuosi)</a></p>
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


@app.get("/buy-yearly")
def buy_yearly():
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="STRIPE_SECRET_KEY is not set")
    if not STRIPE_PRICE_ID_YEARLY:
        raise HTTPException(status_code=500, detail="STRIPE_PRICE_ID_YEARLY is not set")

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": STRIPE_PRICE_ID_YEARLY, "quantity": 1}],
        success_url=f"{BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{BASE_URL}/cancel",
        allow_promotion_codes=True,
    )
    return RedirectResponse(session.url, status_code=303)


@app.get("/success", response_class=HTMLResponse)
def success(session_id: str):
    ensure_tokens_schema()

    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    try:
        session = stripe.checkout.Session.retrieve(session_id, expand=["subscription"])
        subscription = session.get("subscription")
        subscription_id = subscription.get("id") if isinstance(subscription, dict) else subscription
        customer_id = session.get("customer")

        customer_details = session.get("customer_details") or {}
        customer_email = customer_details.get("email") or session.get("customer_email")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid session_id")

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT token, welcome_email_sent_at, customer_email
        FROM tokens
        WHERE stripe_session_id = %s
        """,
        (session_id,),
    )
    row = cur.fetchone()

    if row:
        token = row[0]
        welcome_email_sent_at = row[1]
        existing_customer_email = row[2]

        cur.execute(
            """
            UPDATE tokens
            SET stripe_subscription_id = COALESCE(stripe_subscription_id, %s),
                stripe_customer_id = COALESCE(stripe_customer_id, %s),
                customer_email = COALESCE(customer_email, %s)
            WHERE stripe_session_id = %s
            """,
            (subscription_id, customer_id, customer_email, session_id),
        )
        conn.commit()

        if not customer_email:
            customer_email = existing_customer_email
    else:
        token = secrets.token_urlsafe(16)

        cur.execute(
            """
            INSERT INTO tokens (
                token,
                active,
                stripe_session_id,
                stripe_subscription_id,
                stripe_customer_id,
                timezone,
                customer_email
            )
            VALUES (%s, TRUE, %s, %s, %s, %s, %s)
            """,
            (token, session_id, subscription_id, customer_id, DEFAULT_TIMEZONE, customer_email),
        )
        conn.commit()
        welcome_email_sent_at = None

    cur.close()
    conn.close()

    cal_url = f"{BASE_URL}/calendar/{token}.ics"
    tz_url = f"{BASE_URL}/tz?token={token}"
    portal_url = f"{BASE_URL}/customer-portal?token={token}"

    if customer_email and not welcome_email_sent_at:
        try:
            send_calendar_email(
                to_email=customer_email,
                calendar_url=cal_url,
                tz_url=tz_url,
                portal_url=portal_url,
            )

            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE tokens
                SET welcome_email_sent_at = NOW()
                WHERE stripe_session_id = %s
                """,
                (session_id,),
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception:
            logger.exception("Kalenterisähköpostin lähetys epäonnistui")

    return HTMLResponse(
        f"""
        <h2>Kiitos! Tilauksesi on käsitelty ✅</h2>

        <p><b>Henkilökohtainen kalenterilinkkisi:</b></p>

        <p>
            <input
                id="calendar-link"
                type="text"
                value="{cal_url}"
                readonly
                style="width:100%;max-width:720px;padding:10px;font-size:14px;"
            />
        </p>

        <p>
            <button
                onclick="copyCalendarLink()"
                style="padding:10px 14px;font-size:14px;cursor:pointer;margin-right:8px;"
            >
                Kopioi kalenterilinkki
            </button>

            <a
                href="{portal_url}"
                style="display:inline-block;padding:10px 14px;font-size:14px;text-decoration:none;border:1px solid #ccc;"
            >
                Hallitse tilaustani
            </a>
        </p>

        <p id="copy-status" style="font-weight:bold;"></p>

        <p>
            Kopioi tämä linkki ja lisää se omaan kalenteriisi URL-osoitteena.
            Tämä on henkilökohtainen kalenterisyötteesi.
        </p>

        <p><b>Google Kalenteri:</b> Asetukset → Lisää kalenteri → URL-osoitteesta → liitä linkki</p>
        <p><b>Apple Calendar:</b> File → New Calendar Subscription → liitä linkki</p>
        <p><b>Outlook:</b> Add calendar → Subscribe from web → liitä linkki</p>

        <p><a href="{tz_url}">Vaihda aikavyöhyke</a></p>

        <p>Sähköposti kalenterilinkillä on lähetetty, jos Stripe palautti sähköpostiosoitteen.</p>

        <script>
        async function copyCalendarLink() {{
            const input = document.getElementById("calendar-link");
            const status = document.getElementById("copy-status");

            try {{
                await navigator.clipboard.writeText(input.value);
                status.textContent = "Kalenterilinkki kopioitu ✅";
            }} catch (err) {{
                input.select();
                input.setSelectionRange(0, 99999);
                document.execCommand("copy");
                status.textContent = "Kalenterilinkki kopioitu ✅";
            }}
        }}
        </script>
        """
    )


@app.get("/customer-portal")
def customer_portal(token: str):
    ensure_tokens_schema()

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT stripe_customer_id FROM tokens WHERE token = %s",
        (token,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="Customer not found")

    portal_session = stripe.billing_portal.Session.create(
        customer=row[0],
        return_url=f"{BASE_URL}/",
    )

    return RedirectResponse(portal_session.url, status_code=303)


@app.get("/tz", response_class=HTMLResponse)
def tz_form(token: str):
    ensure_tokens_schema()

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT timezone FROM tokens WHERE token = %s", (token,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Not found")

    current_tz = row[0] or DEFAULT_TIMEZONE

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
    form = await request.form()
    token = (form.get("token") or "").strip()
    tz = (form.get("timezone") or "").strip()

    if not token or not tz:
        raise HTTPException(status_code=400, detail="Missing token or timezone")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM tokens WHERE token = %s", (token,))
    exists = cur.fetchone()
    cur.close()
    conn.close()

    if not exists:
        raise HTTPException(status_code=404, detail="Not found")

    set_token_timezone(token, tz)
    invalidate_token_cache(token)

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

    cached = get_cached_ics(token, tz_name)
    if cached is not None:
        return Response(
            content=cached,
            media_type="text/calendar; charset=utf-8",
            headers={
                "Content-Disposition": f'inline; filename="kuurytmi-{token}.ics"',
                "Cache-Control": "public, max-age=3600",
                "X-Cache": "HIT",
            },
        )

    try:
        ics_bytes = build_ics_for_token(token, tz_name)
        set_cached_ics(token, tz_name, ics_bytes)
    except Exception as e:
        import traceback
        print("ICS generation failed:", repr(e))
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="ICS generation failed")

    return Response(
        content=ics_bytes,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'inline; filename="kuurytmi-{token}.ics"',
            "Cache-Control": "public, max-age=3600",
            "X-Cache": "MISS",
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

    if event_type == "customer.subscription.updated":
        sub = event["data"]["object"]
        sub_id = sub.get("id")
        status = sub.get("status")

        if sub_id:
            if status in ("canceled", "unpaid", "incomplete_expired"):
                set_token_active_by_subscription_id(sub_id, False)
            elif status in ("active", "trialing", "past_due", "incomplete"):
                set_token_active_by_subscription_id(sub_id, True)

    elif event_type == "customer.subscription.deleted":
        sub_id = event["data"]["object"].get("id")
        if sub_id:
            set_token_active_by_subscription_id(sub_id, False)

    elif event_type in ("invoice.payment_succeeded", "invoice.paid"):
        sub_id = event["data"]["object"].get("subscription")
        if sub_id:
            set_token_active_by_subscription_id(sub_id, True)

    elif event_type == "invoice.payment_failed":
        pass

    return {"ok": True}
