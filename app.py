from datetime import datetime
from fastapi import FastAPI, Response
from fastapi.responses import PlainTextResponse

app = FastAPI(title="Via Clara Kuurytmi API")

@app.get("/health", response_class=PlainTextResponse)
def health():
    # Renderin/valvonnan "onko palvelu hengissä" -endpoint
    return "ok"

@app.get("/", response_class=PlainTextResponse)
def home():
    return "Via Clara Kuurytmi backend is running. Try /health or /calendar/test.ics"

@app.get("/calendar/{token}.ics")
def calendar_ics(token: str):
    """
    Väliaikainen demo: palauttaa pienen iCalendar-tiedoston.
    Myöhemmin:
      - tarkistetaan token tietokannasta
      - tarkistetaan Stripe-tilauksen tila
      - palautetaan oikea kuurytmi-ICS
    """
    now_utc = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Via Clara//Kuurytmi Backend//FI
CALSCALE:GREGORIAN
METHOD:PUBLISH
X-WR-CALNAME:Via Clara – Kuurytmi (Demo)
BEGIN:VEVENT
DTSTAMP:{now_utc}
UID:demo-{token}@via-clara
DTSTART;VALUE=DATE:20260301
DTEND;VALUE=DATE:20260302
SUMMARY:✅ Demo toimii ({token})
END:VEVENT
END:VCALENDAR
"""
    return Response(content=ics, media_type="text/calendar; charset=utf-8")
