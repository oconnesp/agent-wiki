#!/usr/bin/env python3
"""
Health data sync: Google Health API v4 (Fitbit Air) → SQLite + raw/
Runs twice daily via systemd timer.

  sync-health.py        daily metrics for the last 3 days
  sync-health.py 30     backfill daily metrics for the last 30 days
"""
import json, sqlite3, urllib.request, urllib.parse, urllib.error
import datetime, pathlib, sys

CONFIG_DIR = pathlib.Path.home() / '.config' / 'agent-wiki' / 'health'
DB_PATH    = pathlib.Path.home() / '.local' / 'share' / 'agent-wiki' / 'health.db'
RAW_DIR    = pathlib.Path.home() / 'agent-wiki' / 'raw'
API        = 'https://health.googleapis.com/v4/users/me/dataTypes/{}/dataPoints'

DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def db_connect():
    con = sqlite3.connect(str(DB_PATH))
    con.execute('''CREATE TABLE IF NOT EXISTS daily (
        date             TEXT PRIMARY KEY,
        sleep_start      TEXT,
        sleep_end        TEXT,
        sleep_total_min  INTEGER,
        sleep_deep_min   INTEGER,
        sleep_light_min  INTEGER,
        sleep_rem_min    INTEGER,
        sleep_awake_min  INTEGER,
        rhr              INTEGER,
        hrv_rmssd_avg    REAL,
        hrv_rmssd_min    REAL,
        hrv_rmssd_max    REAL,
        steps            INTEGER
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS exercises (
        id                  TEXT PRIMARY KEY,
        date                TEXT NOT NULL,
        start_time          TEXT NOT NULL,
        end_time            TEXT,
        utc_offset_s        INTEGER,
        exercise_type       TEXT,
        display_name        TEXT,
        recording_method    TEXT,
        duration_s          REAL,
        distance_m          REAL,
        avg_pace_s_per_km   REAL,
        avg_hr              INTEGER,
        elevation_m         REAL,
        calories            REAL,
        steps               INTEGER,
        active_zone_minutes INTEGER,
        run_vo2max          REAL
    )''')
    con.commit()
    return con

# ---------------------------------------------------------------------------
# Google Health API helpers
# ---------------------------------------------------------------------------

def refresh_token():
    token_file = CONFIG_DIR / 'googlehealth_token.json'
    creds_file = CONFIG_DIR / 'google_credentials.json'
    token = json.loads(token_file.read_text())
    creds = json.loads(creds_file.read_text())['installed']
    data = urllib.parse.urlencode({
        'client_id': creds['client_id'],
        'client_secret': creds['client_secret'],
        'refresh_token': token['refresh_token'],
        'grant_type': 'refresh_token',
    }).encode()
    req = urllib.request.Request(creds['token_uri'], data=data, method='POST')
    resp = json.loads(urllib.request.urlopen(req).read())
    token.update(resp)
    token_file.write_text(json.dumps(token))
    return resp['access_token']

def gh_list(access_token, dtype, params, older_than=None):
    """Yield every data point of a type, following nextPageToken.

    Sleep and exercise reject time filters and come back newest first, 25 per
    page. For those, pass older_than(point): paging stops after a page made up
    entirely of points before the window. Callers still filter each point.
    """
    params = dict(params)
    while True:
        req = urllib.request.Request(API.format(dtype) + '?' + urllib.parse.urlencode(params), headers={
            'Authorization': 'Bearer ' + access_token,
            'Accept': 'application/json',
        })
        try:
            body = json.loads(urllib.request.urlopen(req).read())
        except urllib.error.HTTPError as e:
            print(f'  {dtype} error {e.code}: {e.read().decode()[:200]}', file=sys.stderr)
            return
        points = body.get('dataPoints', [])
        for pt in points:
            yield pt
        if not body.get('nextPageToken'):
            return
        if older_than and points and all(older_than(p) for p in points):
            return
        params['pageToken'] = body['nextPageToken']

def parse_time(ts):
    """RFC 3339 UTC timestamp, with or without fractional seconds."""
    return datetime.datetime.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S')

def seconds(duration):
    """Protobuf duration string such as '3600s'."""
    return float(duration[:-1]) if duration else 0.0

def local_date(ts, utc_offset):
    return str((parse_time(ts) + datetime.timedelta(seconds=seconds(utc_offset))).date())

def civil_date(d):
    return '%04d-%02d-%02d' % (d['year'], d['month'], d['day'])

# ---------------------------------------------------------------------------
# Daily metrics
# ---------------------------------------------------------------------------

def sync_days(con, access_token, dates):
    dates = sorted(dates)
    # Pad a day so points stamped late in the evening UTC but on the next
    # local date are not missed.
    since = str(datetime.date.fromisoformat(dates[0]) - datetime.timedelta(days=1)) + 'T00:00:00Z'
    days = {d: {'sleep': None, 'rhr': None, 'hrv_daily': None, 'hrv': [],
                'steps_fitbit': 0, 'steps_other': 0} for d in dates}

    # Sleep, attributed to the local date you woke up. Keep the longest
    # session per date so a nap does not replace the night.
    for pt in gh_list(access_token, 'sleep', {'pageSize': 25},
                      older_than=lambda p: p.get('sleep', {}).get('interval', {}).get('endTime', '') < since):
        s = pt.get('sleep', {})
        interval = s.get('interval', {})
        if not interval.get('endTime'):
            continue
        d = local_date(interval['endTime'], interval.get('endUtcOffset'))
        if d not in days:
            continue
        stages = {'DEEP': 0, 'LIGHT': 0, 'REM': 0, 'AWAKE': 0}
        for stage in s.get('stages', []):
            if stage.get('type') in stages and stage.get('startTime') and stage.get('endTime'):
                span = parse_time(stage['endTime']) - parse_time(stage['startTime'])
                stages[stage['type']] += int(span.total_seconds() // 60)
        total = stages['DEEP'] + stages['LIGHT'] + stages['REM']
        best = days[d]['sleep']
        if best is None or total > best['total']:
            days[d]['sleep'] = {'start': interval.get('startTime'), 'end': interval['endTime'],
                                'total': total, 'deep': stages['DEEP'], 'light': stages['LIGHT'],
                                'rem': stages['REM'], 'awake': stages['AWAKE']}

    for pt in gh_list(access_token, 'daily-resting-heart-rate',
                      {'filter': f'daily_resting_heart_rate.date >= "{dates[0]}"', 'pageSize': 1000}):
        r = pt.get('dailyRestingHeartRate', {})
        d = civil_date(r['date']) if r.get('date') else None
        if d in days and r.get('beatsPerMinute'):
            days[d]['rhr'] = int(r['beatsPerMinute'])

    for pt in gh_list(access_token, 'daily-heart-rate-variability',
                      {'filter': f'daily_heart_rate_variability.date >= "{dates[0]}"', 'pageSize': 1000}):
        h = pt.get('dailyHeartRateVariability', {})
        d = civil_date(h['date']) if h.get('date') else None
        if d in days and h.get('averageHeartRateVariabilityMilliseconds') is not None:
            days[d]['hrv_daily'] = float(h['averageHeartRateVariabilityMilliseconds'])

    for pt in gh_list(access_token, 'heart-rate-variability',
                      {'filter': f'heart_rate_variability.sample_time.physical_time >= "{since}"', 'pageSize': 10000}):
        h = pt.get('heartRateVariability', {})
        sample_time = h.get('sampleTime', {})
        value = h.get('rootMeanSquareOfSuccessiveDifferencesMilliseconds')
        if value is None or not sample_time.get('physicalTime'):
            continue
        d = local_date(sample_time['physicalTime'], sample_time.get('utcOffset'))
        if d in days:
            days[d]['hrv'].append(float(value))

    # The phone also reports steps. Prefer the band's count so a walk with
    # both is not counted twice; fall back to other sources when it has none.
    for pt in gh_list(access_token, 'steps',
                      {'filter': f'steps.interval.start_time >= "{since}"', 'pageSize': 10000}):
        s = pt.get('steps', {})
        interval = s.get('interval', {})
        if not interval.get('startTime'):
            continue
        d = local_date(interval['startTime'], interval.get('startUtcOffset'))
        if d in days:
            key = 'steps_fitbit' if pt.get('dataSource', {}).get('platform') == 'FITBIT' else 'steps_other'
            days[d][key] += int(s.get('count', 0))

    for d in dates:
        day = days[d]
        sleep = day['sleep'] or {}
        hrv = day['hrv']
        hrv_avg = day['hrv_daily'] if day['hrv_daily'] is not None else (sum(hrv) / len(hrv) if hrv else None)
        steps = day['steps_fitbit'] or day['steps_other'] or None
        con.execute('''INSERT INTO daily
            (date, sleep_start, sleep_end, sleep_total_min, sleep_deep_min, sleep_light_min,
             sleep_rem_min, sleep_awake_min, rhr, hrv_rmssd_avg, hrv_rmssd_min, hrv_rmssd_max, steps)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
                sleep_start=excluded.sleep_start, sleep_end=excluded.sleep_end,
                sleep_total_min=excluded.sleep_total_min, sleep_deep_min=excluded.sleep_deep_min,
                sleep_light_min=excluded.sleep_light_min, sleep_rem_min=excluded.sleep_rem_min,
                sleep_awake_min=excluded.sleep_awake_min, rhr=excluded.rhr,
                hrv_rmssd_avg=excluded.hrv_rmssd_avg, hrv_rmssd_min=excluded.hrv_rmssd_min,
                hrv_rmssd_max=excluded.hrv_rmssd_max, steps=excluded.steps''',
            (d, sleep.get('start'), sleep.get('end'), sleep.get('total') or None,
             sleep.get('deep') or None, sleep.get('light') or None, sleep.get('rem') or None,
             sleep.get('awake') or None, day['rhr'],
             round(hrv_avg, 1) if hrv_avg is not None else None,
             round(min(hrv), 1) if hrv else None, round(max(hrv), 1) if hrv else None, steps))
        print(f'  {d}: sleep={sleep.get("total", 0)}min (D={sleep.get("deep", 0)} L={sleep.get("light", 0)} '
              f'R={sleep.get("rem", 0)}) rhr={day["rhr"]} hrv={hrv_avg and round(hrv_avg, 1)} steps={steps}')
    con.commit()

# ---------------------------------------------------------------------------
# Workouts
# ---------------------------------------------------------------------------

def sync_exercises(con, access_token, days):
    since = str(datetime.date.today() - datetime.timedelta(days=days)) + 'T00:00:00Z'
    count = 0
    for pt in gh_list(access_token, 'exercise', {'pageSize': 25},
                      older_than=lambda p: p.get('exercise', {}).get('interval', {}).get('startTime', '') < since):
        e = pt.get('exercise', {})
        interval = e.get('interval', {})
        start = interval.get('startTime', '')
        if not pt.get('name') or start < since:
            continue
        m = e.get('metricsSummary', {})

        def number(key, scale=1.0):
            return float(m[key]) * scale if m.get(key) is not None else None

        def integer(key):
            return int(m[key]) if m.get(key) is not None else None

        duration = seconds(e.get('activeDuration'))
        if not duration and interval.get('endTime'):
            duration = (parse_time(interval['endTime']) - parse_time(start)).total_seconds()
        row = (pt['name'], local_date(start, interval.get('startUtcOffset')), start, interval.get('endTime'),
               int(seconds(interval.get('startUtcOffset'))), e.get('exerciseType'), e.get('displayName'),
               pt.get('dataSource', {}).get('recordingMethod'), duration or None,
               number('distanceMillimeters', 0.001), number('averagePaceSecondsPerMeter', 1000),
               integer('averageHeartRateBeatsPerMinute'), number('elevationGainMillimeters', 0.001),
               number('caloriesKcal'), integer('steps'), integer('activeZoneMinutes'), number('runVo2Max'))
        con.execute('''INSERT INTO exercises
            (id, date, start_time, end_time, utc_offset_s, exercise_type, display_name, recording_method,
             duration_s, distance_m, avg_pace_s_per_km, avg_hr, elevation_m, calories, steps,
             active_zone_minutes, run_vo2max)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                date=excluded.date, start_time=excluded.start_time, end_time=excluded.end_time,
                utc_offset_s=excluded.utc_offset_s, exercise_type=excluded.exercise_type,
                display_name=excluded.display_name, recording_method=excluded.recording_method,
                duration_s=excluded.duration_s, distance_m=excluded.distance_m,
                avg_pace_s_per_km=excluded.avg_pace_s_per_km, avg_hr=excluded.avg_hr,
                elevation_m=excluded.elevation_m, calories=excluded.calories, steps=excluded.steps,
                active_zone_minutes=excluded.active_zone_minutes, run_vo2max=excluded.run_vo2max''', row)
        count += 1
    con.commit()
    print(f'  workouts: {count} in the last {days} days')

# ---------------------------------------------------------------------------
# Raw summary
# ---------------------------------------------------------------------------

def write_raw(con):
    today = datetime.date.today()
    rows = con.execute('SELECT * FROM daily ORDER BY date DESC LIMIT 14').fetchall()
    cols = [d[0] for d in con.execute('SELECT * FROM daily LIMIT 0').description]

    lines = [f'# Health sync — {today}', '',
             '## Daily metrics (last 14 days)', '']
    for r in rows:
        d = dict(zip(cols, r))
        lines.append(
            f"**{d['date']}** — "
            f"sleep {d['sleep_total_min'] or '-'}min "
            f"(deep {d['sleep_deep_min'] or '-'} light {d['sleep_light_min'] or '-'} rem {d['sleep_rem_min'] or '-'}) | "
            f"RHR {d['rhr'] or '-'}bpm | "
            f"HRV {d['hrv_rmssd_avg'] or '-'}ms | "
            f"steps {d['steps'] or '-'}"
        )

    lines += ['', '## Workouts (last 14 days)', '']
    workouts = con.execute('''SELECT date, display_name, exercise_type, duration_s, distance_m,
        avg_pace_s_per_km, avg_hr FROM exercises WHERE date >= ? ORDER BY start_time DESC''',
        (str(today - datetime.timedelta(days=13)),)).fetchall()
    if not workouts:
        lines.append('No workouts.')
    for date, name, exercise_type, duration, distance, pace, hr in workouts:
        parts = [f'{round(duration / 60)}min' if duration else '-']
        if distance:
            parts.append(f'{distance / 1000:.2f}km')
        if pace and distance:
            parts.append(f'{int(pace // 60)}:{int(pace % 60):02d}/km')
        if hr:
            parts.append(f'avg HR {hr}')
        lines.append(f"**{date}** — {name or exercise_type} | " + ' | '.join(parts))

    out = RAW_DIR / f'health-{today}.md'
    out.write_text('\n'.join(lines) + '\n')
    print(f'Wrote {out}')

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    print('Starting health sync...')
    con = db_connect()
    try:
        access_token = refresh_token()
        today = datetime.date.today()
        sync_days(con, access_token, [str(today - datetime.timedelta(days=i)) for i in range(days_back)])
        # Backfill two months of workouts on first run, then re-read a week so
        # late uploads and edits are picked up.
        empty = con.execute('SELECT COUNT(*) FROM exercises').fetchone()[0] == 0
        sync_exercises(con, access_token, 60 if empty else 7)
    except Exception as e:
        print(f'Sync error: {e}', file=sys.stderr)
        import traceback; traceback.print_exc()
    write_raw(con)
    con.close()
    print('Done.')
