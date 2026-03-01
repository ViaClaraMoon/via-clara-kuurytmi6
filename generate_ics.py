from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from skyfield.api import load
from skyfield import almanac

TZ = ZoneInfo("Europe/Helsinki")
UTC = ZoneInfo("UTC")

# Signs Aries..Pisces = 0..11
SIGN_EMOJI = {
    0: "🐏",  # Aries
    1: "🐂",  # Taurus
    2: "👯",  # Gemini
    3: "🦀",  # Cancer
    4: "🦁",  # Leo
    5: "👧",  # Virgo
    6: "⚖️",  # Libra
    7: "🦂",  # Scorpio
    8: "🏹",  # Sagittarius
    9: "🐐",  # Capricorn
    10: "🏺", # Aquarius
    11: "🐟", # Pisces
}

FIRE = {0, 4, 8}
EARTH = {1, 5, 9}
AIR = {2, 6, 10}
WATER = {3, 7, 11}

ELEMENT = {
    "fire": "🔥",
    "earth": "🌱",
    "air": "🌬",
    "water": "💧",
}

def element_for_sign(sign: int) -> str:
    if sign in FIRE:
        return ELEMENT["fire"]
    if sign in EARTH:
        return ELEMENT["earth"]
    if sign in AIR:
        return ELEMENT["air"]
    return ELEMENT["water"]

def dt_to_ics(dt: datetime) -> str:
    # floating local time
    return dt.strftime("%Y%m%dT%H%M%S")

def d_to_ics(d: date) -> str:
    return d.strftime("%Y%m%d")

def uid(seed: str) -> str:
    return f"{seed}@via-clara-kuurytmi"

def moon_ecliptic_longitude_deg(eph, ts, t) -> float:
    earth = eph["earth"]
    moon = eph["moon"]
    e = earth.at(t).observe(moon).apparent()
    lat, lon, dist = e.ecliptic_latlon()
    return lon.degrees % 360.0

def sign_from_lon(lon_deg: float) -> int:
    return int(lon_deg // 30) % 12

def local_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=TZ)

def local_end_of_day(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=TZ)

@dataclass
class Event:
    summary: str
    dtstart: datetime | date
    dtend: datetime | date
    all_day: bool

def find_sign_change_times(eph, ts, start_local: datetime, end_local: datetime) -> list[datetime]:
    # sample every 10 minutes, then bisection
    step = timedelta(minutes=10)
    samples = []
    dt = start_local
    while dt <= end_local:
        samples.append(dt)
        dt += step

    signs = []
    for dt in samples:
        t = ts.from_datetime(dt.astimezone(UTC))
        lon = moon_ecliptic_longitude_deg(eph, ts, t)
        signs.append(sign_from_lon(lon))

    changes: list[datetime] = []
    for i in range(1, len(samples)):
        if signs[i] != signs[i - 1]:
            lo = samples[i - 1]
            hi = samples[i]
            s_lo = signs[i - 1]

            for _ in range(30):
                mid = lo + (hi - lo) / 2
                t_mid = ts.from_datetime(mid.astimezone(UTC))
                lon_mid = moon_ecliptic_longitude_deg(eph, ts, t_mid)
                s_mid = sign_from_lon(lon_mid)
                if s_mid == s_lo:
                    lo = mid
                else:
                    hi = mid

            ch = hi.replace(second=0, microsecond=0)
            changes.append(ch)

    # de-duplicate very close changes
    out = []
    for c in changes:
        if not out or abs((c - out[-1]).total_seconds()) > 120:
            out.append(c)
    return out

def find_new_full_moons(eph, ts, start_local: datetime, end_local: datetime) -> list[Event]:
    t0 = ts.from_datetime(start_local.astimezone(UTC))
    t1 = ts.from_datetime(end_local.astimezone(UTC))

    f = almanac.moon_phases(eph)
    times, phases = almanac.find_discrete(t0, t1, f)

    events: list[Event] = []
    for t, ph in zip(times, phases):
        if int(ph) not in (0, 2):  # 0=new, 2=full
            continue
        dt_local = t.utc_datetime().replace(tzinfo=UTC).astimezone(TZ)
        lon = moon_ecliptic_longitude_deg(eph, ts, t)
        sign = sign_from_lon(lon)
        sign_emoji = SIGN_EMOJI[sign]
        moon_emoji = "🌑" if int(ph) == 0 else "🌕"
        summary = f"{moon_emoji} {dt_local:%H:%M} {sign_emoji}"
        events.append(Event(summary=summary, dtstart=dt_local, dtend=dt_local + timedelta(minutes=15), all_day=False))
    return events

def build_events(days_ahead: int = 365) -> list[Event]:
    today = datetime.now(TZ).date()
    start = local_midnight(today)
    end = local_midnight(today + timedelta(days=days_ahead))

    ts = load.timescale()
    eph = load("de421.bsp")

    events: list[Event] = []
    events.extend(find_new_full_moons(eph, ts, start, end))

    d = today
    while d < (today + timedelta(days=days_ahead)):
        day_start = local_midnight(d)
        day_end = local_end_of_day(d)

        changes = find_sign_change_times(eph, ts, day_start, day_end)

        if not changes:
            # whole day in one sign -> all day "emoji+element"
            t_mid = ts.from_datetime((day_start + timedelta(hours=12)).astimezone(UTC))
            lon = moon_ecliptic_longitude_deg(eph, ts, t_mid)
            sign = sign_from_lon(lon)
            summary = f"{SIGN_EMOJI[sign]}{element_for_sign(sign)}"
            events.append(Event(summary=summary, dtstart=d, dtend=d + timedelta(days=1), all_day=True))
        else:
            # only timed change events "emoji HH:MM"
            for ch in changes:
                t_ch = ts.from_datetime(ch.astimezone(UTC))
                lon = moon_ecliptic_longitude_deg(eph, ts, t_ch)
                sign = sign_from_lon(lon)
                summary = f"{SIGN_EMOJI[sign]} {ch:%H:%M}"
                events.append(Event(summary=summary, dtstart=ch, dtend=ch + timedelta(minutes=10), all_day=False))

        d += timedelta(days=1)

    def sort_key(ev: Event):
        if ev.all_day:
            return datetime(ev.dtstart.year, ev.dtstart.month, ev.dtstart.day, 0, 0, tzinfo=TZ)
        return ev.dtstart

    events.sort(key=sort_key)
    return events

def generate_ics(events: list[Event]) -> str:
    now = datetime.now(TZ)
    dtstamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Via Clara//Kuurytmi//FI",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Via Clara – Kuurytmi",
        "X-WR-TIMEZONE:Europe/Helsinki",
    ]

    for i, ev in enumerate(events):
        lines.append("BEGIN:VEVENT")
        lines.append(f"DTSTAMP:{dtstamp}")
        lines.append(f"UID:{uid(str(i))}")
        if ev.all_day:
            lines.append(f"DTSTART;VALUE=DATE:{d_to_ics(ev.dtstart)}")
            lines.append(f"DTEND;VALUE=DATE:{d_to_ics(ev.dtend)}")  # exclusive
        else:
            lines.append(f"DTSTART:{dt_to_ics(ev.dtstart)}")
            lines.append(f"DTEND:{dt_to_ics(ev.dtend)}")
        lines.append(f"SUMMARY:{ev.summary}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"

def main():
    events = build_events(days_ahead=365)
    ics = generate_ics(events)

    # GitHub Pages uses /docs
    import os
    os.makedirs("docs", exist_ok=True)
    with open("docs/via-clara-kuurytmi.ics", "w", encoding="utf-8") as f:
        f.write(ics)

if __name__ == "__main__":
    main()
