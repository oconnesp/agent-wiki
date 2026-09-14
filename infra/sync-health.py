#!/usr/bin/env python3
"""
Health data sync: Google Health API v4 (Fitbit Air) + Strava → SQLite + raw/
Runs twice daily via systemd timer.
"""
import json, os, sqlite3, urllib.request, urllib.parse, urllib.error
import datetime, pathlib, sys

CONFIG_DIR = pathlib.Path.home() / '.config' / 'agent-wiki' / 'health'
DB_PATH    = pathlib.Path.home() / '.local' / 'share' / 'agent-wiki' / 'health.db'
RAW_DIR    = pathlib.Path.home() / 'agent-wiki' / 'raw'

DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def db_connect():
    con = sqlite3.connect(DB_PATH)
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
    con.execute('''CREATE TABLE IF NOT EXISTS activities (
        strava_id        INTEGER PRIMARY KEY,
        date             TEXT NOT NULL,
        start_time_local TEXT,
        name             TEXT,
        sport_type       TEXT,
        distance_m       REAL,
        moving_time_s    INTEGER,
        elapsed_time_s   INTEGER,
        elevation_m      REAL,
        avg_hr           REAL,
        max_hr           REAL,
        avg_speed_mps    REAL,
        suffer_score     REAL
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

def gh_get(access_token, dtype, params=''):
    url = f'https://health.googleapis.com/v4/users/me/dataTypes/{dtype}/dataPoints'
    if params:
        url += '?' + params
    req = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + access_token,
        'Accept': 'application/json',
    })
    try:
        return json.loads(urllib.request.urlopen(req).read()).get('dataPoints', [])
    except urllib.error.HTTPError as e:
        print(f'  {dtype} error {e.code}: {e.read().decode()[:100]}', file=sys.stderr)
        return []

def isodate(ts):
    return ts[:10]

def duration_min(start, end):
    fmt = '%Y-%m-%dT%H:%M:%SZ'
    try:
        s = datetime.datetime.strptime(start, fmt)
        e = datetime.datetime.strptime(end, fmt)
        return int((e - s).total_seconds() / 60)
    except Exception:
        return 0

# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_day(con, access_token, date_str):
    # Filter string — civil date filter
    day_filter = urllib.parse.quote(f'sleep.interval.civil_start_time >= "{date_str}T00:00:00" AND sleep.interval.civil_start_time < "{date_str}T23:59:59"')

    # Sleep
    sleep_start = sleep_end = None
    total_min = deep_min = light_min = rem_min = awake_min = 0
    pts = gh_get(access_token, 'sleep')
    for pt in pts:
        s = pt.get('sleep', {})
        interval = s.get('interval', {})
        start = interval.get('startTime', '')
        end   = interval.get('endTime', '')
        if not end or isodate(end) != date_str:
            continue
        sleep_start = start
        sleep_end   = end
        for stage in s.get('stages', []):
            st = stage.get('type', '')
            mins = duration_min(stage.get('startTime', ''), stage.get('endTime', ''))
            if st == 'DEEP':   deep_min  += mins
            elif st == 'LIGHT': light_min += mins
            elif st == 'REM':   rem_min   += mins
            elif st == 'AWAKE': awake_min += mins
        total_min = deep_min + light_min + rem_min

    # HRV
    hrv_vals = []
    for pt in gh_get(access_token, 'heart-rate-variability'):
        v = pt.get('heartRateVariability', {}).get('rootMeanSquareOfSuccessiveDifferencesMilliseconds')
        ts = pt.get('heartRateVariability', {}).get('sampleTime', {}).get('physicalTime', '')
        if v is not None and ts[:10] == date_str:
            hrv_vals.append(float(v))
    hrv_avg = round(sum(hrv_vals)/len(hrv_vals), 1) if hrv_vals else None
    hrv_min = round(min(hrv_vals), 1) if hrv_vals else None
    hrv_max = round(max(hrv_vals), 1) if hrv_vals else None

    # RHR (minimum HR of the day)
    hr_vals = []
    for pt in gh_get(access_token, 'heart-rate'):
        bpm = pt.get('heartRate', {}).get('beatsPerMinute')
        ts  = pt.get('heartRate', {}).get('sampleTime', {}).get('physicalTime', '')
        if bpm is not None and ts[:10] == date_str:
            hr_vals.append(int(bpm))
    rhr = min(hr_vals) if hr_vals else None

    # Steps
    steps_total = 0
    for pt in gh_get(access_token, 'steps'):
        ts = pt.get('steps', {}).get('interval', {}).get('startTime', '')
        if ts[:10] == date_str:
            steps_total += int(pt.get('steps', {}).get('count', 0))

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
        (date_str, sleep_start, sleep_end, total_min or None, deep_min or None,
         light_min or None, rem_min or None, awake_min or None,
         rhr, hrv_avg, hrv_min, hrv_max, steps_total or None))
    con.commit()

    print(f'  {date_str}: sleep={total_min}min (D={deep_min} L={light_min} R={rem_min}) '
          f'rhr={rhr} hrv={hrv_avg} steps={steps_total}')

# ---------------------------------------------------------------------------
# Strava
# ---------------------------------------------------------------------------

def strava_access_token():
    token_file = CONFIG_DIR / 'strava_token.json'
    token = json.loads(token_file.read_text())
    if token.get('expires_at', 0) > datetime.datetime.now().timestamp() + 300:
        return token['access_token']
    data = urllib.parse.urlencode({
        'client_id': token['client_id'],
        'client_secret': token['client_secret'],
        'refresh_token': token['refresh_token'],
        'grant_type': 'refresh_token',
    }).encode()
    req = urllib.request.Request('https://www.strava.com/oauth/token', data=data, method='POST')
    resp = json.loads(urllib.request.urlopen(req).read())
    # Strava rotates refresh tokens; persist the new one before anything else.
    token.update({k: resp[k] for k in ('access_token', 'refresh_token', 'expires_at')})
    token_file.write_text(json.dumps(token))
    return resp['access_token']

def sync_strava(con, access_token):
    # Backfill two months on first run, then re-read a week so edits and late
    # uploads (HR, titles) are picked up.
    empty = con.execute('SELECT COUNT(*) FROM activities').fetchone()[0] == 0
    days = 60 if empty else 7
    after = int((datetime.datetime.now() - datetime.timedelta(days=days)).timestamp())
    page = 1
    while True:
        url = ('https://www.strava.com/api/v3/athlete/activities?'
               + urllib.parse.urlencode({'after': after, 'per_page': 100, 'page': page}))
        req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + access_token})
        activities = json.loads(urllib.request.urlopen(req).read())
        for a in activities:
            start_local = a.get('start_date_local', a['start_date'])
            con.execute('''INSERT INTO activities
                (strava_id, date, start_time_local, name, sport_type, distance_m, moving_time_s,
                 elapsed_time_s, elevation_m, avg_hr, max_hr, avg_speed_mps, suffer_score)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(strava_id) DO UPDATE SET
                    date=excluded.date, start_time_local=excluded.start_time_local,
                    name=excluded.name, sport_type=excluded.sport_type,
                    distance_m=excluded.distance_m, moving_time_s=excluded.moving_time_s,
                    elapsed_time_s=excluded.elapsed_time_s, elevation_m=excluded.elevation_m,
                    avg_hr=excluded.avg_hr, max_hr=excluded.max_hr,
                    avg_speed_mps=excluded.avg_speed_mps, suffer_score=excluded.suffer_score''',
                (a['id'], start_local[:10], start_local, a.get('name'),
                 a.get('sport_type') or a.get('type'), a.get('distance'), a.get('moving_time'),
                 a.get('elapsed_time'), a.get('total_elevation_gain'), a.get('average_heartrate'),
                 a.get('max_heartrate'), a.get('average_speed'), a.get('suffer_score')))
        con.commit()
        print(f'  Strava page {page}: {len(activities)} activities (last {days} days)')
        if len(activities) < 100:
            break
        page += 1

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

    lines += ['', '## Strava activities (last 14 days)', '']
    acts = con.execute('''SELECT date, name, sport_type, distance_m, moving_time_s, avg_hr
        FROM activities WHERE date >= ? ORDER BY start_time_local DESC''',
        (str(today - datetime.timedelta(days=13)),)).fetchall()
    if not acts:
        lines.append('No activities.')
    for date, name, sport, dist, secs, hr in acts:
        dist_s = f'{dist / 1000:.2f}km' if dist else '-'
        secs_s = f'{round(secs / 60)}min' if secs else '-'
        hr_s = round(hr) if hr else '-'
        lines.append(f"**{date}** — {name} ({sport}) | {dist_s} | {secs_s} | avg HR {hr_s}")

    out = RAW_DIR / f'health-{today}.md'
    out.write_text('\n'.join(lines) + '\n')
    print(f'Wrote {out}')

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    print('Starting health sync...')
    con = db_connect()
    try:
        access_token = refresh_token()
        today = datetime.date.today()
        for i in range(3):
            d = str(today - datetime.timedelta(days=i))
            sync_day(con, access_token, d)
    except Exception as e:
        print(f'Sync error: {e}', file=sys.stderr)
        import traceback; traceback.print_exc()
    try:
        print('Syncing Strava...')
        sync_strava(con, strava_access_token())
    except urllib.error.HTTPError as e:
        # A 401/403 body names the problem, e.g. a missing activity:read_all scope.
        print(f'Strava error {e.code}: {e.read().decode()[:300]}', file=sys.stderr)
    except Exception as e:
        print(f'Strava error: {e}', file=sys.stderr)
        import traceback; traceback.print_exc()
    write_raw(con)
    con.close()
    print('Done.')
