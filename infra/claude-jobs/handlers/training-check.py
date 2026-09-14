#!/usr/bin/env python3
"""
Daily training-plan check for the claude-jobs runner.

  training-check.py context   PRE_COMMAND: print the active plan page and the
                              last two weeks of training data for the prompt.
                              Exits 3 when no page is tagged active-plan, so
                              the runner skips the job without calling Claude.
  training-check.py           POST_COMMAND: read the model's answer on stdin,
                              rewrite the plan block and change log, append to
                              wiki/log.md, and print the Telegram message.
                              Prints nothing when the plan is unchanged.

The model never writes files. This handler does, under the agent-wiki
operation lock, and rolls back if the wiki linter fails. Runs on Python 3.8.
"""
import datetime, fcntl, hashlib, os, pathlib, re, subprocess, sys, time

REPO = pathlib.Path(os.environ.get('AGENT_WIKI_REPO') or pathlib.Path.home() / 'agent-wiki')
WIKI = REPO / 'wiki'
RUNTIME = pathlib.Path(os.environ.get('XDG_RUNTIME_DIR') or '/run/user/%d' % os.getuid()) / 'agent-wiki'
STATE = pathlib.Path(os.environ.get('CLAUDE_JOBS_STATE_DIR')
                     or pathlib.Path.home() / '.local' / 'state' / 'claude-jobs') / 'training-check'
PLAN_START, PLAN_END = '<!-- plan:start -->', '<!-- plan:end -->'
CHANGES_END = '<!-- changes:end -->'
SKIP = 3


def active_plan():
    for path in sorted(WIKI.glob('*.md')):
        text = path.read_text(encoding='utf-8')
        frontmatter = re.match(r'\A---\n(.*?)\n---\n', text, re.S)
        if frontmatter and 'active-plan' in frontmatter.group(1) and PLAN_START in text and PLAN_END in text:
            return path, text
    return None, None


def plan_block(text):
    return text.split(PLAN_START, 1)[1].split(PLAN_END, 1)[0]


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def context():
    path, text = active_plan()
    if path is None:
        print('no wiki page tagged active-plan with plan markers; nothing to check', file=sys.stderr)
        return SKIP
    # Remember the block the model is shown, so a chat edit made while the
    # model is thinking is not overwritten.
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / 'plan.sha256').write_text(digest(plan_block(text)))

    today = datetime.date.today()
    summary = subprocess.run(
        [sys.executable, str(REPO / 'infra' / 'trainer.py'), 'summary', '--days', '14'],
        stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout
    print('TODAY: %s (%s)' % (today, today.strftime('%A')))
    print('WEEK_START: %s' % (today - datetime.timedelta(days=today.weekday())))
    print('PLAN_PAGE: wiki/%s' % path.name)
    print('\n=== PLAN PAGE ===\n' + text)
    print('\n=== TRAINING DATA ===\n' + summary)
    return 0


def parse(answer):
    status = re.search(r'^STATUS:\s*(changed|unchanged)\s*$', answer, re.M)
    if not status:
        raise ValueError('answer has no STATUS line')
    summary = re.search(r'^SUMMARY:[ \t]*(.*)$', answer, re.M)
    body = re.search(r'^=== PLAN ===[ \t]*\n(.*?)^=== MESSAGE ===[ \t]*\n?(.*)\Z', answer, re.S | re.M)
    if not body:
        raise ValueError('answer is missing the PLAN or MESSAGE section')
    return (status.group(1), summary.group(1).strip() if summary else '',
            body.group(1).strip(), body.group(2).strip())


def wiki_lock(timeout=900):
    """Take the operation lock git sync and the Telegram session share, and
    wait for any Claude turn in progress to finish."""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    handle = open(str(RUNTIME / 'operation.lock'), 'w')
    deadline = time.time() + timeout
    while True:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if not (RUNTIME / 'busy').exists():
            return handle
        fcntl.flock(handle, fcntl.LOCK_UN)
        if time.time() > deadline:
            handle.close()
            raise RuntimeError('the Telegram session stayed busy for %ds' % timeout)
        time.sleep(15)


def apply(answer):
    status, summary, plan, message = parse(answer)
    if status == 'unchanged':
        return 0
    if not re.match(r'Week of \d{4}-\d\d-\d\d', plan) or not message:
        raise ValueError('a changed plan needs a block starting "Week of YYYY-MM-DD" and a message')
    today = str(datetime.date.today())
    # The change log adds its own date; drop one the model may have prefixed.
    summary = re.sub(r'^\d{4}-\d\d-\d\d:?\s*', '', summary) or 'Plan adjusted'

    if os.environ.get('CLAUDE_JOB_DRY_RUN') == '1':
        print('[dry run] change: %s\n\n%s\n\nMessage:\n%s' % (summary, plan, message))
        return 0

    lock = wiki_lock()
    try:
        path, text = active_plan()
        seen = STATE / 'plan.sha256'
        if path is None or not seen.exists() or digest(plan_block(text)) != seen.read_text().strip():
            print('plan page changed during the check; leaving it for the next run', file=sys.stderr)
            return 0

        log_path = WIKI / 'log.md'
        originals = {path: text, log_path: log_path.read_text(encoding='utf-8')}

        head, rest = text.split(PLAN_START, 1)
        updated = head + PLAN_START + '\n' + plan + '\n' + PLAN_END + rest.split(PLAN_END, 1)[1]
        updated = re.sub(r'^updated: .*$', 'updated: ' + today, updated, count=1, flags=re.M)
        change = '- %s: %s' % (today, summary)
        if CHANGES_END in updated:
            updated = updated.replace(CHANGES_END, change + '\n' + CHANGES_END, 1)
        else:
            updated = updated.rstrip('\n') + '\n' + change + '\n'
        path.write_text(updated, encoding='utf-8')
        log_path.write_text(
            originals[log_path].rstrip('\n')
            + '\n\n## [%s] training-check | %s\n- Updated this week\'s plan on %s.\n' % (today, summary, path.stem),
            encoding='utf-8')

        lint = subprocess.run([sys.executable, str(REPO / 'infra' / 'wiki_lint.py'), str(REPO)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        if lint.returncode != 0:
            for original_path, original in originals.items():
                original_path.write_text(original, encoding='utf-8')
            raise RuntimeError('wiki lint failed; changes rolled back:\n' + lint.stdout[-800:])
        seen.write_text(digest(plan_block(updated)))
    finally:
        lock.close()

    print(message)
    return 0


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['context']:
            sys.exit(context())
        sys.exit(apply(sys.stdin.read()))
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as e:
        print('training-check: %s' % e, file=sys.stderr)
        sys.exit(1)
