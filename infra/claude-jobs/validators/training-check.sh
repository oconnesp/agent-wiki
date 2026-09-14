#!/usr/bin/env bash
# Reject a plan check the handler could not apply. Reads the answer on stdin.
set -euo pipefail

answer="$(cat)"

status="$(grep -m1 -oE '^STATUS: *(changed|unchanged) *$' <<<"$answer" | sed -E 's/^STATUS: *//; s/ *$//' || true)"
if [[ -z "$status" ]]; then
    echo "Answer is missing a 'STATUS: changed|unchanged' line" >&2
    exit 1
fi

for marker in '=== PLAN ===' '=== MESSAGE ==='; do
    if ! grep -qxE "$marker *" <<<"$answer"; then
        echo "Answer is missing the '$marker' section" >&2
        exit 1
    fi
done

if [[ "$status" == "changed" ]] && ! grep -qE '^Week of [0-9]{4}-[0-9]{2}-[0-9]{2}' <<<"$answer"; then
    echo "A changed plan must start with a 'Week of YYYY-MM-DD' line" >&2
    exit 1
fi
