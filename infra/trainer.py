#!/usr/bin/env python3
"""
Personal trainer data: gym logging, weigh-ins, and a combined training summary.

Shares health.db with sync-health.py (Fitbit via Google Health, Strava).
Standard library only; runs on Python 3.8.

  trainer.py log-gym --json '{"type": "push", "exercises": [...]}'   (or JSON on stdin)
  trainer.py gym [--days 28]
  trainer.py history "Bench press" [--days 180]
  trainer.py delete-gym ID
  trainer.py weigh-in 82.4 [--date YYYY-MM-DD]
  trainer.py summary [--days 14]

Gym JSON shape:
  {"date": "2026-09-14", "type": "push", "duration_min": 60, "rpe": 8,
   "notes": "felt strong",
   "exercises": [
     {"name": "Bench press", "sets": [{"reps": 8, "weight_kg": 80}, {"reps": 6, "weight_kg": 85, "rpe": 9}]},
     {"name": "Dips", "sets": 3, "reps": 10},
     {"name": "Cable fly", "sets": 3, "reps": 12, "weight_kg": 15}
   ]}
"""
import argparse, datetime, json, pathlib, sqlite3, sys

DB_PATH = pathlib.Path.home() / '.local' / 'share' / 'agent-wiki' / 'health.db'


def connect(path=DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    con.executescript('''
        CREATE TABLE IF NOT EXISTS gym_sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            date         TEXT NOT NULL,
            type         TEXT,
            duration_min INTEGER,
            rpe          REAL,
            notes        TEXT,
            created_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS gym_sets (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES gym_sessions(id) ON DELETE CASCADE,
            position   INTEGER NOT NULL,
            exercise   TEXT NOT NULL,
            set_number INTEGER NOT NULL,
            reps       INTEGER,
            weight_kg  REAL,
            rpe        REAL,
            notes      TEXT
        );
        CREATE INDEX IF NOT EXISTS gym_sessions_date ON gym_sessions(date);
        CREATE INDEX IF NOT EXISTS gym_sets_exercise ON gym_sets(exercise COLLATE NOCASE);
        CREATE TABLE IF NOT EXISTS weigh_ins (
            date        TEXT PRIMARY KEY,
            weight_kg   REAL NOT NULL,
            measured_at TEXT NOT NULL
        );
    ''')
    return con


def has_table(con, name):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def since(days):
    return str(datetime.date.today() - datetime.timedelta(days=days - 1))


def fmt_num(x):
    if x is None:
        return '-'
    return ('%.1f' % x).rstrip('0').rstrip('.')


def epley(weight_kg, reps):
    if not weight_kg or not reps:
        return None
    return weight_kg * (1 + reps / 30.0) if reps > 1 else weight_kg


# ---------------------------------------------------------------------------
# Gym sessions
# ---------------------------------------------------------------------------

def expand_sets(exercise):
    """Accept an explicit list of sets or the shorthand sets/reps/weight_kg."""
    sets = exercise.get('sets')
    if isinstance(sets, list):
        return sets
    if isinstance(sets, int) and sets > 0:
        template = {k: exercise.get(k) for k in ('reps', 'weight_kg', 'rpe')}
        return [dict(template) for _ in range(sets)]
    raise ValueError('exercise %r needs "sets" as a list or a positive count' % exercise.get('name'))


def log_gym(con, data):
    exercises = data.get('exercises') or []
    if not exercises:
        raise ValueError('a gym session needs at least one exercise')
    date = data.get('date') or str(datetime.date.today())
    datetime.date.fromisoformat(date)

    rows = []
    for position, exercise in enumerate(exercises, 1):
        name = (exercise.get('name') or '').strip()
        if not name:
            raise ValueError('every exercise needs a name')
        for number, s in enumerate(expand_sets(exercise), 1):
            rows.append((position, name, s.get('set_number') or number, s.get('reps'),
                         s.get('weight_kg'), s.get('rpe'), s.get('notes')))

    with con:
        cur = con.execute(
            'INSERT INTO gym_sessions (date, type, duration_min, rpe, notes, created_at) VALUES (?,?,?,?,?,?)',
            (date, data.get('type'), data.get('duration_min'), data.get('rpe'), data.get('notes'),
             datetime.datetime.now().isoformat(timespec='seconds')))
        session_id = cur.lastrowid
        con.executemany(
            'INSERT INTO gym_sets (session_id, position, exercise, set_number, reps, weight_kg, rpe, notes) '
            'VALUES (?,?,?,?,?,?,?,?)', [(session_id,) + r for r in rows])
    return session_id


def with_sets(con, session_rows):
    sessions = [dict(r) for r in session_rows]
    for s in sessions:
        s['sets'] = [dict(r) for r in con.execute(
            'SELECT exercise, set_number, reps, weight_kg, rpe, notes FROM gym_sets '
            'WHERE session_id = ? ORDER BY position, set_number', (s['id'],))]
        s['volume_kg'] = round(sum((x['reps'] or 0) * (x['weight_kg'] or 0) for x in s['sets']), 1)
    return sessions


def load_sessions(con, days):
    return with_sets(con, con.execute(
        'SELECT * FROM gym_sessions WHERE date >= ? ORDER BY date DESC, id DESC', (since(days),)))


def load_session(con, session_id):
    return with_sets(con, con.execute('SELECT * FROM gym_sessions WHERE id = ?', (session_id,)))[0]


def session_lines(s):
    head = '**%s** #%d %s' % (s['date'], s['id'], s['type'] or 'session')
    extras = []
    if s['duration_min']:
        extras.append('%d min' % s['duration_min'])
    if s['rpe']:
        extras.append('RPE %s' % fmt_num(s['rpe']))
    if s['volume_kg']:
        extras.append('volume %s kg' % fmt_num(s['volume_kg']))
    lines = [head + (' — ' + ', '.join(extras) if extras else '')]

    by_exercise = []
    for x in s['sets']:
        if not by_exercise or by_exercise[-1][0] != x['exercise']:
            by_exercise.append((x['exercise'], []))
        load = '%sx%s' % (fmt_num(x['weight_kg']), x['reps']) if x['weight_kg'] else '%s' % (x['reps'] or '-')
        if x['rpe']:
            load += '@%s' % fmt_num(x['rpe'])
        by_exercise[-1][1].append(load)
    for name, sets in by_exercise:
        lines.append('- %s: %s' % (name, ', '.join(sets)))
    if s['notes']:
        lines.append('- notes: %s' % s['notes'])
    return lines


def exercise_history(con, exercise, days):
    rows = con.execute(
        'SELECT s.date, s.id, g.set_number, g.reps, g.weight_kg, g.rpe FROM gym_sets g '
        'JOIN gym_sessions s ON s.id = g.session_id '
        'WHERE g.exercise = ? COLLATE NOCASE AND s.date >= ? ORDER BY s.date, s.id, g.position, g.set_number',
        (exercise, since(days))).fetchall()
    out = []
    for r in rows:
        if not out or out[-1]['session_id'] != r['id']:
            out.append({'date': r['date'], 'session_id': r['id'], 'sets': [], 'best_e1rm_kg': None})
        out[-1]['sets'].append({'reps': r['reps'], 'weight_kg': r['weight_kg'], 'rpe': r['rpe']})
        e = epley(r['weight_kg'], r['reps'])
        if e and (out[-1]['best_e1rm_kg'] is None or e > out[-1]['best_e1rm_kg']):
            out[-1]['best_e1rm_kg'] = round(e, 1)
    return out


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def summary(con, days):
    start = since(days)
    result = {'from': start, 'to': str(datetime.date.today()),
              'daily': [], 'activities': [], 'gym_sessions': load_sessions(con, days), 'weigh_ins': []}
    if has_table(con, 'daily'):
        result['daily'] = [dict(r) for r in con.execute(
            'SELECT date, sleep_total_min, sleep_deep_min, sleep_rem_min, rhr, hrv_rmssd_avg, steps '
            'FROM daily WHERE date >= ? ORDER BY date DESC', (start,))]
    if has_table(con, 'activities'):
        result['activities'] = [dict(r) for r in con.execute(
            'SELECT date, start_time_local, name, sport_type, distance_m, moving_time_s, elevation_m, '
            'avg_hr, max_hr, suffer_score FROM activities WHERE date >= ? ORDER BY start_time_local DESC',
            (start,))]
    result['weigh_ins'] = [dict(r) for r in con.execute(
        'SELECT date, weight_kg FROM weigh_ins WHERE date >= ? ORDER BY date DESC', (start,))]
    return result


def summary_markdown(sm):
    lines = ['# Training summary %s to %s' % (sm['from'], sm['to']), '', '## Recovery (Fitbit)']
    if not sm['daily']:
        lines.append('No health data synced for this period.')
    for d in sm['daily']:
        lines.append('- %s: sleep %s min (deep %s, REM %s) | RHR %s | HRV %s ms | steps %s' % (
            d['date'], d['sleep_total_min'] or '-', d['sleep_deep_min'] or '-', d['sleep_rem_min'] or '-',
            d['rhr'] or '-', fmt_num(d['hrv_rmssd_avg']), d['steps'] or '-'))

    lines += ['', '## Cardio (Strava)']
    if not sm['activities']:
        lines.append('No Strava activities in this period.')
    for a in sm['activities']:
        dist = '%.2f km' % (a['distance_m'] / 1000) if a['distance_m'] else '-'
        mins = '%d min' % round(a['moving_time_s'] / 60) if a['moving_time_s'] else '-'
        pace = ''
        if a['distance_m'] and a['moving_time_s'] and a['sport_type'] in ('Run', 'TrailRun', 'VirtualRun'):
            secs = a['moving_time_s'] / (a['distance_m'] / 1000)
            pace = ' | %d:%02d /km' % (secs // 60, secs % 60)
        lines.append('- %s %s (%s): %s, %s%s | avg HR %s | elev %s m' % (
            a['date'], a['name'], a['sport_type'], dist, mins, pace,
            fmt_num(a['avg_hr']), fmt_num(a['elevation_m'])))

    lines += ['', '## Gym']
    if not sm['gym_sessions']:
        lines.append('No gym sessions logged in this period.')
    for s in sm['gym_sessions']:
        lines += session_lines(s)

    lines += ['', '## Body weight']
    if not sm['weigh_ins']:
        lines.append('No weigh-ins in this period.')
    for w in sm['weigh_ins']:
        lines.append('- %s: %s kg' % (w['date'], fmt_num(w['weight_kg'])))
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', type=pathlib.Path, default=DB_PATH)
    parser.add_argument('--json-output', action='store_true', help='print JSON instead of Markdown')
    sub = parser.add_subparsers(dest='command')
    sub.required = True

    p = sub.add_parser('log-gym', help='log a gym session')
    p.add_argument('--json', help='session JSON; read from stdin when omitted')
    p = sub.add_parser('gym', help='list recent gym sessions')
    p.add_argument('--days', type=int, default=28)
    p = sub.add_parser('history', help='sets and estimated 1RM for one exercise')
    p.add_argument('exercise')
    p.add_argument('--days', type=int, default=180)
    p = sub.add_parser('delete-gym', help='delete a gym session by id')
    p.add_argument('id', type=int)
    p = sub.add_parser('weigh-in', help='record body weight (one per day, latest wins)')
    p.add_argument('weight_kg', type=float)
    p.add_argument('--date')
    p = sub.add_parser('summary', help='recovery, cardio, gym and weight for recent days')
    p.add_argument('--days', type=int, default=14)

    args = parser.parse_args(argv)
    con = connect(args.db)

    def emit(obj, markdown):
        print(json.dumps(obj, indent=2) if args.json_output else markdown)

    try:
        if args.command == 'log-gym':
            data = json.loads(args.json if args.json else sys.stdin.read())
            s = load_session(con, log_gym(con, data))
            emit(s, 'Logged:\n' + '\n'.join(session_lines(s)))
        elif args.command == 'gym':
            sessions = load_sessions(con, args.days)
            emit(sessions, '\n\n'.join('\n'.join(session_lines(s)) for s in sessions)
                 or 'No gym sessions in the last %d days.' % args.days)
        elif args.command == 'history':
            hist = exercise_history(con, args.exercise, args.days)
            md = ['- %s: %s | best e1RM %s kg' % (
                h['date'],
                ', '.join('%sx%s' % (fmt_num(x['weight_kg']), x['reps']) for x in h['sets']),
                fmt_num(h['best_e1rm_kg'])) for h in hist]
            emit(hist, '\n'.join(md) or 'No sets of %r in the last %d days.' % (args.exercise, args.days))
        elif args.command == 'delete-gym':
            with con:
                n = con.execute('DELETE FROM gym_sessions WHERE id = ?', (args.id,)).rowcount
            emit({'deleted': n}, 'Deleted session #%d.' % args.id if n else 'No session #%d.' % args.id)
        elif args.command == 'weigh-in':
            date = args.date or str(datetime.date.today())
            datetime.date.fromisoformat(date)
            with con:
                con.execute('INSERT INTO weigh_ins (date, weight_kg, measured_at) VALUES (?,?,?) '
                            'ON CONFLICT(date) DO UPDATE SET weight_kg=excluded.weight_kg, '
                            'measured_at=excluded.measured_at',
                            (date, args.weight_kg, datetime.datetime.now().isoformat(timespec='seconds')))
            emit({'date': date, 'weight_kg': args.weight_kg},
                 'Recorded %s kg on %s.' % (fmt_num(args.weight_kg), date))
        elif args.command == 'summary':
            sm = summary(con, args.days)
            emit(sm, summary_markdown(sm))
    except (ValueError, KeyError, TypeError) as e:
        print('error: %s' % e, file=sys.stderr)
        return 2
    finally:
        con.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
