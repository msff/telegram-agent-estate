#!/bin/zsh
# One instance's daily maintenance window. Called by supervisor.sh once a day,
# on the first loop pass after `housekeeping.hour`.
#
# Usage:  housekeeping.sh <instance.yaml>
#
# Split out of the supervisor loop at U13 so it can also be run by hand during
# an incident without restarting the daemon — the old version was inline, which
# meant the only way to force a rotation was to wait for 03:00 or fake the stamp.
#
# ORDER MATTERS. Rotation runs FIRST: it fires one silent flush turn that distils
# the session into the handoff file, then drops the session id so the next turn
# re-bootstraps from that file, and compacts the WAL — all under the drain lock,
# so it can never race a live turn. Everything after it is cleanup that must not
# run while a turn might still be writing.
set -u

CONFIG="${1:-${ESTATE_INSTANCE_CONFIG:-}}"
if [[ -z "$CONFIG" ]]; then
  echo "usage: housekeeping.sh <instance.yaml>" >&2
  exit 2
fi
CONFIG="${CONFIG/#\~/$HOME}"
[[ -r "$CONFIG" ]] || { echo "unreadable instance config: $CONFIG" >&2; exit 2; }
export ESTATE_INSTANCE_CONFIG="$CONFIG"

BINDIR="${0:A:h}"

# Resolve THIS instance's interpreter out of its own config before doing
# anything else. The ambient `python3` is not usable — see estate-bootstrap.zsh.
source "$BINDIR/estate-bootstrap.zsh" \
  || { echo "missing $BINDIR/estate-bootstrap.zsh" >&2; exit 2; }
estate_resolve_python "$CONFIG" || exit $?

eval "$("$VENV_PY" - "$BINDIR" "$CONFIG" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import instance_config
c = instance_config.load(sys.argv[2])
def q(v):
    return "'" + str(v).replace("'", "'\\''") + "'"
print(f"NAME={q(c.name)}")
print(f"WORKDIR={q(c.workdir)}")
print(f"VENV_PY={q(c.python)}")
print(f"LOGDIR={q(c.log_dir)}")
print(f"AGENT_STATE={q(c.agent_state_dir)}")
print(f"OWNER_CHAT={q(c.owner_chat_id)}")
print(f"LOG_CAP={q(c.log_cap_bytes)}")
print(f"HK_SCRIPT={q(c.housekeeping_script or '')}")
PY
)" || { echo "failed to load instance config $CONFIG" >&2; exit 2; }

RUNNER="$BINDIR/turn_runner.py"
TAG="--instance=$NAME"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "housekeeping window ($NAME): rotate + prune/compact/sweep"
cd "$WORKDIR" || { log "missing workdir $WORKDIR"; exit 1; }

# 1. Rotation: flush the handoff, drop the session, compact the WAL, prune the
#    handoff. Failure here is survivable — the handoff keeps its last good state
#    and the next rotation retries.
"$VENV_PY" "$RUNNER" "$TAG" --config "$CONFIG" rotate --chat-id "$OWNER_CHAT" \
  || log "rotation failed — the handoff keeps its last good state"

# 2. The instance's own extra maintenance, if it declares one.
#
#    The failure flag below is written but the caller still stamps the day as
#    done — DELIBERATE. A stamp-on-success design re-fires the whole sequence
#    (including a rotation and its billed turn) on every loop pass for the rest
#    of the day whenever this script fails persistently. Failure is surfaced
#    instead, via a flag the agent reads.
if [[ -n "$HK_SCRIPT" ]]; then
  if [[ -x "$HK_SCRIPT" || -f "$HK_SCRIPT" ]]; then
    if ! "$VENV_PY" "$HK_SCRIPT"; then
      log "instance housekeeping script FAILED — flagging for triage"
      echo "$(date) $HK_SCRIPT failed — see daemon.log" \
        > "$AGENT_STATE/housekeeping-failed"
    fi
  else
    log "configured housekeeping script not found: $HK_SCRIPT"
  fi
fi

# 3. Cap the logs this instance owns.
#
#    In-place truncation, never `mv`: the writers hold O_APPEND fds, so the next
#    write lands at the new EOF. Rotating by rename would strand those fds — the
#    processes would keep writing to an unlinked inode and the "rotated" log
#    would stay empty forever while the disk kept filling.
for f in "$LOGDIR/poller.log" "$LOGDIR/drain.log" "$LOGDIR/daemon.log"; do
  size=$(stat -f %z "$f" 2>/dev/null || echo 0)
  if [[ "$size" -gt "$LOG_CAP" ]]; then
    log "truncating ${f:t} ($size bytes > $LOG_CAP)"
    : > "$f" 2>/dev/null || true
  fi
done

# 4. Sweep the media inbox (>14 days).
INBOX="$AGENT_STATE/inbox"
if [[ -d "$INBOX" ]]; then
  swept=$(find "$INBOX" -type f -mtime +14 -print -delete 2>/dev/null | wc -l | tr -d ' ')
  [[ "$swept" -gt 0 ]] && log "swept $swept inbox file(s) older than 14d"
fi

log "housekeeping done ($NAME)"
