from datetime import datetime
from fastapi import FastAPI, Response, HTTPException
from fastapi.responses import PlainTextResponse
import secrets
import os
import psycopg2

app = FastAPI(title="Via Clara Kuurytmi API")

DATABASE_URL = os.getenv("DATABASE_URL")

def get_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            id SERIAL PRIMARY KEY,
            token TEXT UNIQUE NOT NULL,
            active BOOLEAN DEFAULT TRUE
        );
    """)
    conn.commit()
    cur.close()
    conn.close()

@app.on_event("startup")
def startup():
    init_db()

@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"

@app.post("/create-token")
def create_token():
    token = secrets.token_urlsafe(16)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO tokens (token) VALUES (%s)", (token,))
    conn.commit()
    cur.close()
    conn.close()

    return {
        "token": token,
        "calendar_url": f"https://via-clara-kuurytmi6.onrender.com/calendar/{token}.ics"
    }

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
