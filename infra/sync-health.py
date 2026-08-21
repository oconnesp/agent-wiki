#!/usr/bin/env python3
"""
Health data sync: Google Health API v4 (Fitbit Air) → SQLite + raw/
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
    write_raw(con)
    con.close()
    print('Done.')
