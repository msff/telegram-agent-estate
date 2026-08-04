#!/bin/zsh
# Fire one scheduled job for one instance. Called by that job's launchd plist.
#
# Usage:  run-job.sh <instance.yaml> <job-name>
#
# Looks the job up in the instance config's `schedules:` list, reads its prompt
# file, and hands it to `turn_runner run-job`, which guards -> writes the lease
# -> appends a scheduler entry to the receipt WAL -> drains synchronously.
#
# THE EXIT CODE IS THE IDEMPOTENCY CONTRACT, and it is passed through unchanged:
#   0  enqueued and drained
#   3  marker exists — already sent today
#   4  fresh lease — a send is in flight
#   5  before the job's validity window (`not_before`)
# launchd fires a missed job once on wake; 3 and 5 are how that duplicate is
# absorbed rather than answered twice.
set -u

CONFIG="${1:-}"
JOB="${2:-}"
if [[ -z "$CONFIG" || -z "$JOB" ]]; then
  echo "usage: run-job.sh <instance.yaml> <job-name>" >&2
  exit 2
fi
CONFIG="${CONFIG/#\~/$HOME}"
[[ -r "$CONFIG" ]] || { echo "unreadable instance config: $CONFIG" >&2; exit 2; }
export ESTATE_INSTANCE_CONFIG="$CONFIG"

LIBEXEC="${0:A:h}"
PLUGIN_ROOT="${LIBEXEC:h}"

# Resolve THIS instance's interpreter out of its own config before doing
# anything else. The ambient `python3` is not usable — see estate-bootstrap.zsh.
source "$LIBEXEC/estate-bootstrap.zsh" \
  || { echo "missing $LIBEXEC/estate-bootstrap.zsh" >&2; exit 2; }
estate_resolve_python "$CONFIG" || exit $?

# Resolve the schedule entry in python — it owns the config schema, and the
# prompt file may be plugin-relative.
eval "$("$VENV_PY" - "$LIBEXEC" "$CONFIG" "$JOB" "$PLUGIN_ROOT" <<'PY'
import sys, os
sys.path.insert(0, sys.argv[1])
import instance_config

c = instance_config.load(sys.argv[2])
job, plugin_root = sys.argv[3], sys.argv[4]

def q(v):
    return "'" + str(v).replace("'", "'\\''") + "'"

entry = next((s for s in c.schedules if s.get("job") == job), None)
if entry is None:
    known = ", ".join(sorted(str(s.get("job")) for s in c.schedules)) or "(none)"
    print(f"echo 'no schedule named {job!r} in {c.source} — known: {known}' >&2")
    print("exit 2")
    raise SystemExit(0)

pf = entry.get("prompt_file")
if not pf:
    print(f"echo 'schedule {job!r} has no prompt_file' >&2")
    print("exit 2")
    raise SystemExit(0)
p = os.path.expanduser(str(pf))
if not os.path.isabs(p):
    # Instance-relative first, then plugin-relative: an instance's own prompts
    # live with the instance, but the shipped defaults live with the plugin.
    cand = os.path.join(str(c.workdir), p)
    p = cand if os.path.exists(cand) else os.path.join(plugin_root, p)
if not os.path.exists(p):
    print(f"echo 'prompt file for {job!r} not found: {p}' >&2")
    print("exit 2")
    raise SystemExit(0)

print(f"NAME={q(c.name)}")
print(f"VENV_PY={q(c.python)}")
print(f"WORKDIR={q(c.workdir)}")
print(f"PROMPT_FILE={q(p)}")
print(f"NOT_BEFORE={q(entry.get('not_before') or '')}")
print(f"SILENT={q('1' if entry.get('silent') else '')}")
print(f"CHAT={q(c.owner_chat_id)}")
PY
)" || exit $?

PROMPT=$(cat "$PROMPT_FILE") || { echo "cannot read $PROMPT_FILE" >&2; exit 2; }

args=("$LIBEXEC/turn_runner.py" "--instance=$NAME" --config "$CONFIG" run-job
      --job "$JOB" --prompt "$PROMPT" --chat-id "$CHAT")
[[ -n "$NOT_BEFORE" ]] && args+=(--not-before "$NOT_BEFORE")
[[ -n "$SILENT" ]] && args+=(--silent)

cd "$WORKDIR" || { echo "missing workdir $WORKDIR" >&2; exit 1; }
"$VENV_PY" "${args[@]}"
exit $?
