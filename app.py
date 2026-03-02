from datetime import datetime
from fastapi import FastAPI, Response, HTTPException
from fastapi.responses import PlainTextResponse
import secrets

app = FastAPI(title="Via Clara Kuurytmi API")

# Väliaikainen "tietokanta"
active_tokens = set()

@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"

@app.post("/create-token")
def create_token():
    token = secrets.token_urlsafe(16)
    active_tokens.add(token)
    return {"token": token,
            "calendar_url": f"https://via-clara-kuurytmi6.onrender.com/calendar/{token}.ics"}

@app.get("/calendar/{token}.ics")
def calendar_ics(token: str):
    if token not in active_tokens:
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
