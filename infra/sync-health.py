#!/usr/bin/env python3
"""
Health data sync: Google Fit + Strava → SQLite + raw/
Runs twice daily via systemd timer.
"""
import json, os, sqlite3, urllib.request, urllib.parse, datetime, pathlib, sys

CONFIG_DIR = pathlib.Path.home() / '.config' / 'agent-wiki' / 'health'
DB_PATH    = pathlib.Path.home() / '.local' / 'share' / 'agent-wiki' / 'health.db'
RAW_DIR    = pathlib.Path.home() / 'agent-wiki' / 'raw'

DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.execute('''CREATE TABLE IF NOT EXISTS daily (
        date        TEXT PRIMARY KEY,
        sleep_start TEXT, sleep_end TEXT,
        sleep_duration_min INTEGER,
        sleep_deep_min INTEGER, sleep_light_min INTEGER, sleep_rem_min INTEGER,
        rhr INTEGER,
        hrv_rmssd REAL,
        steps INTEGER,
        active_min INTEGER
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS heart_rate (
        ts   TEXT PRIMARY KEY,
        bpm  INTEGER
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS activities (
        strava_id    INTEGER PRIMARY KEY,
        date         TEXT,
        name         TEXT,
        sport        TEXT,
        distance_m   REAL,
        duration_s   INTEGER,
        elevation_m  REAL,
        avg_hr       INTEGER,
        max_hr       INTEGER,
        avg_pace_s   REAL,
        suffer_score INTEGER
    )''')
    con.commit()
    return con

# ---------------------------------------------------------------------------
# Google Fit helpers
# ---------------------------------------------------------------------------

def gfit_refresh_token():
    token_file = CONFIG_DIR / 'google_token.json'
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

def gfit_get(access_token, path, body=None):
    url = 'https://www.googleapis.com/fitness/v1/users/me/' + path
    data = json.dumps(body).encode() if body else None
    method = 'POST' if body else 'GET'
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': 'Bearer ' + access_token,
        'Content-Type': 'application/json',
    })
    return json.loads(urllib.request.urlopen(req).read())

def ms(dt): return int(dt.timestamp() * 1000)

def sync_google_fit(con, access_token, days=2):
    now = datetime.datetime.now(datetime.timezone.utc)
    for i in range(days):
        day = now.date() - datetime.timedelta(days=i)
        day_start = datetime.datetime.combine(day, datetime.time.min, tzinfo=datetime.timezone.utc)
        day_end   = datetime.datetime.combine(day, datetime.time.max, tzinfo=datetime.timezone.utc)

        # Steps
        steps = 0
        try:
            r = gfit_get(access_token, 'dataset:aggregate', {
                'aggregateBy': [{'dataTypeName': 'com.google.step_count.delta'}],
                'bucketByTime': {'durationMillis': 86400000},
                'startTimeMillis': ms(day_start),
                'endTimeMillis': ms(day_end),
            })
            for b in r.get('bucket', []):
                for ds in b.get('dataset', []):
                    for pt in ds.get('point', []):
                        steps += sum(v.get('intVal', 0) for v in pt.get('value', []))
        except Exception as e:
            print(f'  steps error: {e}', file=sys.stderr)

        # Heart rate (RHR = daily minimum)
        rhr = None
        try:
            r = gfit_get(access_token, 'dataset:aggregate', {
                'aggregateBy': [{'dataTypeName': 'com.google.heart_rate.bpm'}],
                'bucketByTime': {'durationMillis': 86400000},
                'startTimeMillis': ms(day_start),
                'endTimeMillis': ms(day_end),
            })
            mins = []
            for b in r.get('bucket', []):
                for ds in b.get('dataset', []):
                    for pt in ds.get('point', []):
                        for v in pt.get('value', []):
                            if 'fpVal' in v:
                                mins.append(v['fpVal'])
            if mins:
                rhr = int(min(mins))
        except Exception as e:
            print(f'  hr error: {e}', file=sys.stderr)

        # Sleep
        sleep_start = sleep_end = sleep_dur = sleep_deep = sleep_light = sleep_rem = None
        try:
            r = gfit_get(access_token, 'sessions', f'?startTime={day_start.isoformat()}&endTime={day_end.isoformat()}&activityType=72')
            sessions = r.get('session', [])
            if sessions:
                s = sessions[-1]
                sleep_start = datetime.datetime.fromtimestamp(int(s['startTimeMillis'])//1000, tz=datetime.timezone.utc).isoformat()
                sleep_end   = datetime.datetime.fromtimestamp(int(s['endTimeMillis'])//1000,   tz=datetime.timezone.utc).isoformat()
                sleep_dur   = (int(s['endTimeMillis']) - int(s['startTimeMillis'])) // 60000
        except Exception as e:
            print(f'  sleep error: {e}', file=sys.stderr)

        con.execute('''INSERT INTO daily
            (date, sleep_start, sleep_end, sleep_duration_min, sleep_deep_min, sleep_light_min, sleep_rem_min, rhr, steps)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
                sleep_start=excluded.sleep_start, sleep_end=excluded.sleep_end,
                sleep_duration_min=excluded.sleep_duration_min,
                sleep_deep_min=excluded.sleep_deep_min, sleep_light_min=excluded.sleep_light_min,
                sleep_rem_min=excluded.sleep_rem_min, rhr=excluded.rhr, steps=excluded.steps''',
            (str(day), sleep_start, sleep_end, sleep_dur, sleep_deep, sleep_light, sleep_rem, rhr, steps))
        print(f'  Google Fit {day}: steps={steps} rhr={rhr} sleep={sleep_dur}min')
    con.commit()

# ---------------------------------------------------------------------------
# Strava helpers
# ---------------------------------------------------------------------------

def strava_refresh_token():
    token_file = CONFIG_DIR / 'strava_token.json'
    token = json.loads(token_file.read_text())
    if token['expires_at'] > datetime.datetime.now().timestamp() + 300:
        return token['access_token']
    data = urllib.parse.urlencode({
        'client_id': token['client_id'],
        'client_secret': token['client_secret'],
        'refresh_token': token['refresh_token'],
        'grant_type': 'refresh_token',
    }).encode()
    req = urllib.request.Request('https://www.strava.com/oauth/token', data=data, method='POST')
    resp = json.loads(urllib.request.urlopen(req).read())
    token.update({'access_token': resp['access_token'], 'refresh_token': resp['refresh_token'], 'expires_at': resp['expires_at']})
    token_file.write_text(json.dumps(token))
    return resp['access_token']

def strava_get(access_token, path):
    url = 'https://www.strava.com/api/v3/' + path
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + access_token})
    return json.loads(urllib.request.urlopen(req).read())

def sync_strava(con, access_token, days=2):
    after = int((datetime.datetime.now() - datetime.timedelta(days=days+1)).timestamp())
    activities = strava_get(access_token, f'athlete/activities?after={after}&per_page=30')
    for a in activities:
        date = a['start_date'][:10]
        pace = (a['moving_time'] / (a['distance'] / 1000)) if a.get('distance') else None
        con.execute('''INSERT INTO activities
            (strava_id, date, name, sport, distance_m, duration_s, elevation_m, avg_hr, max_hr, avg_pace_s, suffer_score)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(strava_id) DO UPDATE SET
                avg_hr=excluded.avg_hr, max_hr=excluded.max_hr, suffer_score=excluded.suffer_score''',
            (a['id'], date, a['name'], a['type'], a.get('distance'), a.get('moving_time'),
             a.get('total_elevation_gain'), a.get('average_heartrate'), a.get('max_heartrate'),
             pace, a.get('suffer_score')))
        print(f'  Strava {date}: {a["name"]} ({a["type"]}) {a.get("distance",0)/1000:.1f}km')
    con.commit()

# ---------------------------------------------------------------------------
# Write raw/ summary for wiki ingestion
# ---------------------------------------------------------------------------

def write_raw(con):
    today = datetime.date.today()
    rows = con.execute('''SELECT * FROM daily ORDER BY date DESC LIMIT 7''').fetchall()
    acts = con.execute('''SELECT * FROM activities ORDER BY date DESC LIMIT 10''').fetchall()

    lines = [f'# Health sync — {today}', '']
    lines.append('## Daily metrics (last 7 days)')
    lines.append('date | sleep_min | rhr | steps')
    lines.append('-----|-----------|-----|------')
    for r in rows:
        lines.append(f'{r[0]} | {r[3] or "-"} | {r[7] or "-"} | {r[9] or "-"}')

    lines += ['', '## Recent Strava activities']
    for a in acts:
        dist = f'{a[4]/1000:.1f}km' if a[4] else '-'
        lines.append(f'- {a[1]} {a[2]} ({a[3]}) {dist} avg_hr={a[7] or "-"}')

    out = RAW_DIR / f'health-{today}.md'
    out.write_text('\n'.join(lines))
    print(f'Wrote {out}')

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    print('Starting health sync...')
    con = db_connect()
    try:
        print('Syncing Google Fit...')
        gfit_token = gfit_refresh_token()
        sync_google_fit(con, gfit_token)
    except Exception as e:
        print(f'Google Fit error: {e}', file=sys.stderr)
    try:
        print('Syncing Strava...')
        strava_token = strava_refresh_token()
        sync_strava(con, strava_token)
    except Exception as e:
        print(f'Strava error: {e}', file=sys.stderr)
    write_raw(con)
    con.close()
    print('Done.')
