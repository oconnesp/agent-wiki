#!/usr/bin/env bash
# Install or update the claude-jobs runner, CLI, template unit, and the specs,
# prompts, validators and handlers shipped in this directory.
#
# Idempotent: re-run after every git pull. It never schedules a job (use
# `claude-jobs install <name>`) and never overwrites runtime.env.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
config_dir="${CLAUDE_JOBS_CONFIG_DIR:-$HOME/.config/claude-jobs}"
lib_dir="$HOME/.local/lib/claude-jobs"
bin_dir="$HOME/.local/bin"
unit_dir="$HOME/.config/systemd/user"

install -d -m 700 "$config_dir" "$config_dir/prompts" "$config_dir/validators" "$config_dir/handlers"
install -d -m 755 "$lib_dir" "$bin_dir" "$unit_dir" "$HOME/.local/state/claude-jobs"

install -m 755 "$here/bin/claude-job-run.sh" "$lib_dir/claude-job-run.sh"
install -m 755 "$here/bin/claude-jobs.sh" "$bin_dir/claude-jobs"
install -m 644 "$here/systemd/claude-job@.service" "$unit_dir/claude-job@.service"

shopt -s nullglob
for file in "$here"/jobs/*.job "$here"/prompts/*; do
    case "$file" in
        *.job) install -m 600 "$file" "$config_dir/$(basename "$file")" ;;
        *) install -m 600 "$file" "$config_dir/prompts/$(basename "$file")" ;;
    esac
done
for file in "$here"/validators/*; do
    install -m 700 "$file" "$config_dir/validators/$(basename "$file")"
done
for file in "$here"/handlers/*; do
    install -m 700 "$file" "$config_dir/handlers/$(basename "$file")"
done

if [[ ! -f "$config_dir/runtime.env" ]]; then
    install -m 600 /dev/null "$config_dir/runtime.env"
    printf '# Host-specific values for claude-jobs (chat ids). Never commit.\n' >"$config_dir/runtime.env"
    printf 'Created %s\n' "$config_dir/runtime.env"
fi

systemctl --user daemon-reload

cat <<EOF
Installed claude-jobs. Nothing is scheduled until you run:
  claude-jobs list
  claude-jobs run <name> --dry-run
  claude-jobs install <name>
EOF
