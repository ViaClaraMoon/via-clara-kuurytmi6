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
