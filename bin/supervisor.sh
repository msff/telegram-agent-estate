#!/bin/zsh
# Supervise ONE instance's gateway. Driven by that instance's launchd plist:
#   ~/Library/LaunchAgents/<launchd_prefix>.<name>.plist
#
# Usage:  supervisor.sh <instance.yaml>
#
# Keeps the poller alive, backstops the drain, watches for a deaf-but-alive
# poller, runs the daily housekeeping window, and holds turns when the claude
# CLI self-updates until the parity suite passes.
#
# ------------------------------------------------------------------------------
# WHAT CHANGED IN THE MOVE TO THE PLUGIN, AND WHY IT IS THE RISKY PART
#
# Every process-matching pattern in the original was keyed on an absolute script
# path — correct precisely because each instance had its own checkout, so
# `pgrep -f /…/first-instance/daemon/telegram_poll.py` could not possibly
# match the second-instance bot. Under the plugin BOTH instances execute
# `<plugin>/bin/poller.py`, so that same pattern now matches both, and:
#
#   - `poller_pid` would return the OTHER instance's poller, so this instance
#     would never notice its own was dead;
#   - the deaf-poller watchdog would kill the OTHER instance's healthy poller;
#   - `ensure_drain`'s `pgrep -f "turn_runner.py drain"` would see the
#     neighbour's drain and skip its own, quietly, every tick, for as long as
#     the neighbour stayed busy.
#
# So every probe here matches on `--instance=<name>`, which the poller and
# runner carry in argv and cross-check against their loaded config. The rule for
# anything added later: NEVER match on a path under bin/, always on the tag.
# ------------------------------------------------------------------------------
set -u

CONFIG="${1:-${ESTATE_INSTANCE_CONFIG:-}}"
if [[ -z "$CONFIG" ]]; then
  echo "usage: supervisor.sh <instance.yaml>" >&2
  exit 2
fi
CONFIG="${CONFIG/#\~/$HOME}"
[[ -r "$CONFIG" ]] || { echo "unreadable instance config: $CONFIG" >&2; exit 2; }
export ESTATE_INSTANCE_CONFIG="$CONFIG"

BINDIR="${0:A:h}"
PLUGIN_ROOT="${BINDIR:h}"

# One python call reads the whole config; the shell never re-parses YAML — a
# second parser is a second set of defaults to drift out of sync with.
eval "$(/usr/bin/env python3 - "$BINDIR" "$CONFIG" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import instance_config
c = instance_config.load(sys.argv[2])
def q(v):
    return "'" + str(v).replace("'", "'\\''") + "'"
print(f"NAME={q(c.name)}")
print(f"LABEL={q(c.label)}")
print(f"WORKDIR={q(c.workdir)}")
print(f"VENV_PY={q(c.python)}")
print(f"LOGDIR={q(c.log_dir)}")
print(f"GW_STATE={q(c.gateway_state_dir)}")
print(f"AGENT_STATE={q(c.agent_state_dir)}")
print(f"OWNER_CHAT={q(c.owner_chat_id)}")
print(f"TOKEN_FILE={q(c.token_file)}")
print(f"CHANNEL_STATE_DIR={q(c.channel_state_dir)}")
print(f"HOUSEKEEPING_HOUR={q(c.housekeeping_hour)}")
print(f"LOG_CAP={q(c.log_cap_bytes)}")
print(f"HK_SCRIPT={q(c.housekeeping_script or '')}")
PY
)" || { echo "failed to load instance config $CONFIG" >&2; exit 2; }

DAEMON_LOG="$LOGDIR/daemon.log"
POLLER="$BINDIR/poller.py"
RUNNER="$BINDIR/turn_runner.py"
TAG="--instance=$NAME"

mkdir -p "$LOGDIR" "$GW_STATE" "$AGENT_STATE"
cd "$WORKDIR" || { echo "[$(date)] missing workdir $WORKDIR" >> "$DAEMON_LOG"; exit 1; }

log() { echo "[$(date)] $*" >> "$DAEMON_LOG"; }

# The auto-loading official telegram channel plugin starts polling ANY token it
# finds in the channel state dir, which would 409-war this instance's own
# poller — and on a multi-instance mac the default state dir belongs to whichever
# instance got there first, so it could arm itself with the OTHER bot's token.
# Point it at this instance's own directory and leave a blank .env there.
export TELEGRAM_STATE_DIR="$CHANNEL_STATE_DIR"
mkdir -p "$CHANNEL_STATE_DIR"
printf '# Intentionally blank. The token lives in %s, NOT here — see\n# supervisor.sh: this neutralizes the auto-loading official telegram plugin.\n' \
  "$TOKEN_FILE" > "$CHANNEL_STATE_DIR/.env"

# The token itself is NEVER exported. A turn inheriting it would arm that same
# plugin inside the turn's own process.
unset TELEGRAM_BOT_TOKEN
# Billing stays on the subscription (KTD1); an API key in the environment would
# silently move it to metered credits.
unset ANTHROPIC_API_KEY

kill_soft() {
  local pid="$1" i
  kill "$pid" 2>/dev/null || return 0
  for i in 1 2 3; do
    sleep 1
    kill -0 "$pid" 2>/dev/null || return 0
  done
  kill -9 "$pid" 2>/dev/null || true
}

# --- process probes ----------------------------------------------------------
# Match the instance TAG, and require the match to actually be a python process.
# The tag alone is still fooled by any shell whose command line mentions it — an
# operator running `pgrep -f -- --instance=foo` is itself such a shell, which is
# how the equivalent bug was noticed on the reading instance. Checking `comm`
# makes the probe about the process we care about rather than about strings that
# happen to contain its name.
poller_pid() {
  local pid comm
  for pid in ${(f)"$(pgrep -f "poller.py.*$TAG" 2>/dev/null || true)"}; do
    [[ -z "$pid" ]] && continue
    comm=$(ps -o comm= -p "$pid" 2>/dev/null)
    case "${comm:t}" in
      [Pp]ython*) echo "$pid"; return 0 ;;
    esac
  done
  return 1
}

ensure_poller() {
  if [[ -z "$(poller_pid)" ]]; then
    log "starting owned poller ($NAME)"
    ( cd "$WORKDIR" && nohup "$VENV_PY" "$POLLER" "$TAG" --config "$CONFIG" \
        >> "$LOGDIR/poller.log" 2>&1 & )
  fi
}

# Safety-net drain. The poller triggers a drain the moment it journals, and the
# drain re-folds before releasing its lock, so this is NOT the delivery path —
# it is the backstop for the two cases no trigger covers: entries left pending
# by a quota window that has since recovered, and a drain that died mid-turn.
# Hence a slow cadence; it is not a poll loop.
ensure_drain() {
  pgrep -f "turn_runner.py.*$TAG.*drain" >/dev/null 2>&1 && return 0
  ( cd "$WORKDIR" && nohup "$VENV_PY" "$RUNNER" "$TAG" --config "$CONFIG" drain \
      >> "$LOGDIR/drain.log" 2>&1 & )
}

notify_owner() {
  "$VENV_PY" "$BINDIR/send.py" "$1" >> "$DAEMON_LOG" 2>&1 \
    || log "owner notify failed: $1"
}

# --- CLI-version tripwire ----------------------------------------------------
# The verified `-p --resume` behaviour (returned-id chain, concurrency) is a
# property of the installed CLI with no documented stability contract, and the
# CLI self-updates silently. So: on a version change HOLD turns (inbound keeps
# journaling — nothing is lost), run the parity suite, and release only if it
# passes. A failure leaves turns held and tells the owner, which is the right
# failure mode: a bot that says nothing beats a bot answering wrongly.
CLI_VERSION_FILE="$GW_STATE/cli-version"
HOLD_FILE="$GW_STATE/turns-held"

check_cli_version() {
  local current previous
  current=$(claude --version 2>/dev/null | head -1) || return 0
  [[ -z "$current" ]] && return 0
  previous=$(cat "$CLI_VERSION_FILE" 2>/dev/null || echo "")
  [[ "$current" == "$previous" ]] && return 0
  if [[ -n "$previous" ]]; then
    log "claude CLI changed: '$previous' -> '$current' — holding turns, running parity"
    date -u +%Y-%m-%dT%H:%M:%SZ > "$HOLD_FILE"
    if "$VENV_PY" -m pytest "$PLUGIN_ROOT/tests/test_gateway_parity.py" -q \
         >> "$DAEMON_LOG" 2>&1; then
      rm -f "$HOLD_FILE"
      log "parity passed on '$current' — turns released"
    else
      log "parity FAILED on '$current' — turns stay held"
      notify_owner "⚠️ $LABEL: claude CLI updated to $current and the parity suite failed. Turns are held; inbound is still being journaled."
    fi
  fi
  echo "$current" > "$CLI_VERSION_FILE"
}

# --- daily housekeeping window -----------------------------------------------
# Sleep-proof wall-clock check, NOT StartCalendarInterval — which silently skips
# when the mac is asleep and does not catch up. Fires on the first loop pass
# after the configured hour when the stamp is older than today.
HOUSEKEEPING_STAMP="$AGENT_STATE/last-housekeeping-day"
maybe_housekeeping() {
  local today hour last
  today=$(date +%Y-%m-%d)
  hour=$(date +%H)
  [[ "$hour" -lt "$HOUSEKEEPING_HOUR" ]] && return 1
  last=$(cat "$HOUSEKEEPING_STAMP" 2>/dev/null || echo "")
  [[ "$last" == "$today" ]] && return 1
  "$BINDIR/housekeeping.sh" "$CONFIG" >> "$DAEMON_LOG" 2>&1 \
    || log "housekeeping returned non-zero — see above"
  echo "$today" > "$HOUSEKEEPING_STAMP"
  return 0
}

# --- poller health -----------------------------------------------------------
# A hung-but-alive poller (asyncio wedged, network half-dead) passes the pgrep
# check while messages pile up at Telegram. The honest signal is the backlog
# probe: getWebhookInfo pending_update_count is read-only and does not contend
# with the live poller. Backlog >=1 for 3 consecutive ticks -> restart.
#
# The token is read function-locally and NEVER exported, for the reason above.
instance_backlog() {
  local token resp
  token=$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$TOKEN_FILE" 2>/dev/null | head -1)
  token="${token//\"/}"
  token="${token//\'/}"
  [[ -z "$token" ]] && { echo ""; return }
  resp=$(curl -s --max-time 5 "https://api.telegram.org/bot${token}/getWebhookInfo" 2>/dev/null) || { echo ""; return }
  echo "$resp" | /usr/bin/env python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    print(d["result"]["pending_update_count"] if d.get("ok") else "")
except Exception:
    print("")' 2>/dev/null
}

# Backlog-driven restarts are CAPPED. A foreign poller holding the token 409s
# forever, and each restart only burns context; past the cap the watchdog holds
# and tells the owner once an hour instead of flapping.
RESTART_LOG="$GW_STATE/backlog-restarts.log"
backlog_restarts_last_hour() {
  local cutoff
  cutoff=$(( $(date +%s) - 3600 ))
  [[ -f "$RESTART_LOG" ]] || { echo 0; return }
  awk -v c="$cutoff" '$1 >= c' "$RESTART_LOG" 2>/dev/null | wc -l | tr -d ' '
}

# --- supervise ---------------------------------------------------------------
log "gateway supervisor up for '$NAME' (poller + turn runner)"
POLLER_DEAF_HITS=0
PREV_TICK=""
WAKE_HOLD_UNTIL=0
LAST_ALERT=0
TICK=0
while true; do
  TICK=$((TICK + 1))
  ensure_poller

  # Wake grace: after mac sleep the backlog is legitimately non-zero while the
  # poller's long-poll reconnects with backoff — don't count those ticks.
  NOW=$(date +%s)
  if [[ -n "$PREV_TICK" ]] && (( NOW - PREV_TICK > 90 )); then
    WAKE_HOLD_UNTIL=$(( NOW + 300 ))
    POLLER_DEAF_HITS=0
    log "wake detected (tick gap $((NOW - PREV_TICK))s), poller probe held 300s"
  fi
  PREV_TICK=$NOW

  POLLER_PID=$(poller_pid)
  if [[ -n "$POLLER_PID" ]] && (( NOW >= WAKE_HOLD_UNTIL )); then
    PENDING=$(instance_backlog)
    if [[ -n "$PENDING" && "$PENDING" -ge 1 ]]; then
      POLLER_DEAF_HITS=$((POLLER_DEAF_HITS + 1))
      log "poller alive but backlog=$PENDING (deaf hits=$POLLER_DEAF_HITS/3)"
      if (( POLLER_DEAF_HITS >= 3 )); then
        if (( $(backlog_restarts_last_hour) >= 2 )); then
          if (( NOW - LAST_ALERT > 3600 )); then
            log "backlog persists but restart cap reached — holding"
            notify_owner "⚠️ $LABEL: backlog persists and the poller has already been restarted twice this hour. Suspect a foreign poller on the same token (check for 409s), or rotate the token."
            LAST_ALERT=$NOW
          fi
        else
          log "poller deaf-but-alive, killing pid=$POLLER_PID (ensure_poller respawns)"
          echo "$NOW" >> "$RESTART_LOG"
          kill_soft "$POLLER_PID"
        fi
        POLLER_DEAF_HITS=0
      fi
    else
      POLLER_DEAF_HITS=0
    fi
  fi

  # Backstop drain every ~5 min (see ensure_drain: not the delivery path).
  (( TICK % 10 == 1 )) && ensure_drain

  # CLI-version tripwire every ~10 min.
  (( TICK % 20 == 1 )) && check_cli_version

  maybe_housekeeping || true

  sleep 30
done
