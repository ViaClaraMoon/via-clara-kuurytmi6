print("Kalenterin generointi käynnissä...")

from datetime import datetime, timedelta
import math

def moon_phase_simple(date):
    diff = date - datetime(2001, 1, 1)
    days = diff.days + (diff.seconds / 86400)
    lunations = 0.20439731 + (days * 0.03386319269)
    return lunations % 1

def moon_phase_emoji(phase):
    if phase < 0.03 or phase > 0.97:
        return "🌑"
    elif 0.47 < phase < 0.53:
        return "🌕"
    else:
        return ""

SIGNS = ["🐏","🐂","👯","🦀","🦁","👧","⚖️","🦂","🏹","🐐","🏺","🐟"]
ELEMENTS = {
    0:"🔥",4:"🔥",8:"🔥",
    1:"🌱",5:"🌱",9:"🌱",
    2:"🌬",6:"🌬",10:"🌬",
    3:"💧",7:"💧",11:"💧"
}

def fake_moon_sign(day_index):
    return day_index % 12

def generate_calendar():
    today = datetime.now()
    end = today + timedelta(days=365)

    lines = []
    lines.append("BEGIN:VCALENDAR")
    lines.append("VERSION:2.0")
    lines.append("PRODID:-//Via Clara//Kuurytmi//FI")

    current = today
    day_index = 0

    while current < end:
        sign = fake_moon_sign(day_index)
        emoji = SIGNS[sign]
        element = ELEMENTS[sign]
        phase = moon_phase_simple(current)
        moon = moon_phase_emoji(phase)

        if moon:
            summary = f"{moon} {emoji}"
        else:
            summary = f"{emoji}{element}"

        start = current.strftime("%Y%m%d")
        next_day = (current + timedelta(days=1)).strftime("%Y%m%d")

        lines.append("BEGIN:VEVENT")
        lines.append(f"DTSTART;VALUE=DATE:{start}")
        lines.append(f"DTEND;VALUE=DATE:{next_day}")
        lines.append(f"SUMMARY:{summary}")
        lines.append("END:VEVENT")

        current += timedelta(days=1)
        day_index += 1

    lines.append("END:VCALENDAR")

    with open("via-clara-kuurytmi.ics", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print("Kalenteri luotu!")

if __name__ == "__main__":
    generate_calendar()
