#!/bin/zsh
# Uninstall one instance: stop it, remove what this plugin installed, and leave
# everything that was the user's before it arrived.
#
# Usage:  uninstall-instance.sh <instance.yaml> [--yes] [--dry-run]
#                               [--purge-state] [--purge-secrets]
#                               [--keep-claude-md]
#
# ------------------------------------------------------------------------------
# WHAT THIS REMOVES, AND WHAT IT REFUSES TO REMOVE
#
# Removed by default, because this plugin created all of it:
#   - the supervisor plist and every job plist under `<launchd_prefix>.<name>`
#   - this instance's running poller and supervisor
#   - the two marker-delimited blocks in the project's CLAUDE.md
#
# NOT removed unless asked, because it is evidence or it is a secret:
#   --purge-state    the gateway state dir and logs. The receipt WAL holds
#                    messages that were never answered and the send journal is
#                    how a retry knows a message already went out; deleting them
#                    is a decision, not a step.
#   --purge-secrets  the token file and the permission allowlist.
#
# NEVER removed, at all, by any flag: the workdir and everything in it beyond
# those two marker pairs, the venv, and the handoff file. The handoff is the
# agent's own memory, it lives in the user's repo, and an uninstall is not the
# moment to decide a year of context is worthless. Nor does removing the token
# file revoke the token — only BotFather does that, and the last section of the
# plan says so.
#
# THE DEFAULT IS A PLAN, NOT AN ACTION. With no `--yes` this prints what it
# would do and exits 3. `--dry-run` prints the same plan and exits 0: one of
# those is a refusal for want of confirmation and the other is a question that
# was asked and answered, and a caller branching on the status deserves to be
# able to tell them apart.
#
# IT WORKS AFTER THE VENV IS GONE. Deleting the venv is a natural first move
# when tearing something down, and an uninstaller that then cannot remove the
# launchd jobs it installed is worse than useless — it leaves a timer running
# with no way to stop it. So the config is read through the shared sed reader
# when the interpreter named in it will not run, which is enough to identify the
# instance, unload its jobs, and clean the CLAUDE.md (`claude-md-block.py` is
# stdlib-only for exactly this moment). The purges need the full config and say
# so rather than guessing at a path.
#
# EXIT CODES:
#   0  done, or nothing was installed, or `--dry-run` printed the plan
#   1  the work failed
#   2  usage, or the instance config cannot be read
#   3  a plan was printed and nothing was changed — re-run with --yes
#   4  refused: a plist under this name belongs to a DIFFERENT instance config
set -u

YES="" DRY_RUN="" PURGE_STATE="" PURGE_SECRETS="" KEEP_CLAUDE_MD="" CONFIG=""
usage() {
  cat <<'EOF'
usage: uninstall-instance.sh <instance.yaml> [options]

  --yes             actually do it (without this, the plan is printed)
  --dry-run         print the plan and exit 0
  --purge-state     also delete the gateway state dir and logs
  --purge-secrets   also delete the token file and the permission allowlist
  --keep-claude-md  leave the CLAUDE.md blocks in place
EOF
}
for arg in "$@"; do
  case "$arg" in
    --yes) YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --purge-state) PURGE_STATE=1 ;;
    --purge-secrets) PURGE_SECRETS=1 ;;
    --keep-claude-md) KEEP_CLAUDE_MD=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) print -ru2 -- "uninstall-instance.sh: unknown option '$arg'"; usage >&2; exit 2 ;;
    *) CONFIG="$arg" ;;
  esac
done
[[ -z "$CONFIG" ]] && { usage >&2; exit 2 }
CONFIG="${CONFIG/#\~/$HOME}"
CONFIG="${CONFIG:A}"
if [[ ! -r "$CONFIG" ]]; then
  print -ru2 -- "unreadable instance config: $CONFIG"
  print -ru2 -- "  The config is what identifies the instance — its name is where the"
  print -ru2 -- "  launchd labels, the state dir and the process patterns all come from,"
  print -ru2 -- "  so there is nothing to derive them from without it. If the config is"
  print -ru2 -- "  gone for good, unload and delete the plists by hand:"
  print -ru2 -- "    launchctl unload ~/Library/LaunchAgents/<prefix>.<name>*.plist"
  print -ru2 -- "    rm ~/Library/LaunchAgents/<prefix>.<name>*.plist"
  exit 2
fi

SELF_LIBEXEC="${0:A:h}"
source "$SELF_LIBEXEC/estate-runtime.zsh" \
  || { print -ru2 -- "missing $SELF_LIBEXEC/estate-runtime.zsh"; exit 2 }
estate_resolve_libexec "${SELF_LIBEXEC:h}" || exit $?
LIBEXEC="$ESTATE_LIBEXEC_DIR"
source "$LIBEXEC/estate-bootstrap.zsh" \
  || { print -ru2 -- "missing $LIBEXEC/estate-bootstrap.zsh"; exit 2 }
AGENTS="${ESTATE_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}"

# --- read the config, in whichever of the two modes is available --------------
#
# Full mode is the one that can honour a purge: only `instance_config` knows
# that an unset `channel_state_dir` falls back to the log dir, or what
# `state_dir` expands to. Degraded mode gets the three fields the sed reader can
# reach and is explicit about what that costs.
DEGRADED=""
NAME="" LABEL_PREFIX="" WORKDIR=""
STATE_DIR="" LOG_DIR="" CHANNEL_STATE_DIR="" TOKEN_FILE="" SETTINGS_FILE=""

if estate_resolve_python "$CONFIG" 2>/dev/null; then
  eval "$("$VENV_PY" - "$LIBEXEC" "$CONFIG" <<'PY' 2>/dev/null
import sys
sys.path.insert(0, sys.argv[1])
import instance_config
c = instance_config.load(sys.argv[2])
def q(v):
    return "'" + str(v).replace("'", "'\\''") + "'"
print(f"NAME={q(c.name)}")
print(f"LABEL_PREFIX={q(c.launchd_prefix)}")
print(f"WORKDIR={q(c.workdir)}")
print(f"STATE_DIR={q(c.gateway_state_dir)}")
print(f"LOG_DIR={q(c.log_dir)}")
print(f"CHANNEL_STATE_DIR={q(c.channel_state_dir)}")
print(f"TOKEN_FILE={q(c.token_file)}")
print(f"SETTINGS_FILE={q(c.permission_settings or '')}")
PY
)"
fi

if [[ -z "$NAME" ]]; then
  DEGRADED=1
  estate_config_raw  "$CONFIG" name;           NAME="$ESTATE_RAW"
  estate_config_raw  "$CONFIG" launchd_prefix; LABEL_PREFIX="$ESTATE_RAW"
  estate_config_path "$CONFIG" workdir;        WORKDIR="$ESTATE_PATH"
fi

if [[ -z "$NAME" || -z "$LABEL_PREFIX" ]]; then
  print -ru2 -- "instance config $CONFIG names no 'name:' and 'launchd_prefix:'"
  print -ru2 -- "  Together those two are the instance's launchd identity; without them"
  print -ru2 -- "  there is no way to tell this instance's jobs from a neighbour's."
  exit 2
fi

say() { print -r -- "[uninstall:$NAME] $*" }
plan() { print -r -- "  $*" }
FAILED=0
# Only ever reached past the confirmation gate, so it does not re-check `$YES`:
# a second guard here would be a second place for "did the user say yes?" to be
# answered, and the answer belongs in one place.
run() { eval "$@" || FAILED=1 }

# --- what is actually installed ----------------------------------------------
#
# By glob, not by re-reading `schedules:` from the config. A job deleted from
# the config since install still has its plist on disk and still fires — those
# orphans are the single most likely thing to outlive an uninstall that trusted
# the config to describe the machine.
typeset -a PLISTS
PLISTS=("$AGENTS/$LABEL_PREFIX.$NAME".plist(N) "$AGENTS/$LABEL_PREFIX.$NAME"-*.plist(N))

# The clobber check from install-instance.sh, inverted. There it refuses to
# overwrite a plist belonging to another config; here it refuses to DELETE one.
# Same sed, deliberately: two readings of "which instance owns this plist" that
# could disagree would be worse than one that is wrong in both places.
typeset -a FOREIGN
for p in $PLISTS; do
  other=$(sed -n 's|.*supervisor\.sh \([^<]*\)</string>.*|\1|p;s|.*run-job\.sh \([^ ]*\) .*|\1|p' "$p" 2>/dev/null | head -1)
  [[ -n "$other" && "$other" != "$CONFIG" ]] && FOREIGN+=("$p -> $other")
done
if (( ${#FOREIGN} )); then
  say "REFUSED: a plist under this name belongs to a different instance config:"
  for f in $FOREIGN; do say "    $f"; done
  say "  this run: $CONFIG"
  say "  Removing it would tear down an instance you did not ask about. Point"
  say "  this at that config instead, or rename one of the two instances."
  exit 4
fi

# This instance's live processes. The poller is matched on its `--instance` tag
# and NEVER on a path under libexec/ — every instance executes the same files,
# so a path-keyed pattern is a pattern that kills the neighbours. The supervisor
# carries its config path in argv, which is just as instance-specific.
POLLER_PIDS=$(pgrep -f "poller\.py.*--instance=$NAME" 2>/dev/null | tr '\n' ' ')
SUPERVISOR_PIDS=$(pgrep -f "supervisor\.sh.*$CONFIG" 2>/dev/null | tr '\n' ' ')

CLAUDE_MD=""
[[ -n "$WORKDIR" && -f "$WORKDIR/CLAUDE.md" ]] && CLAUDE_MD="$WORKDIR/CLAUDE.md"

# --- the plan -----------------------------------------------------------------
say "instance config: $CONFIG"
[[ -n "$DEGRADED" ]] && {
  say "NOTE: the interpreter named in this config will not run, so only the"
  say "      fields a plain text read can reach are known. Enough to unload the"
  say "      jobs and clean the CLAUDE.md; not enough to purge anything."
}

if (( ${#PLISTS} )); then
  say "launchd jobs to unload and remove:"
  for p in $PLISTS; do plan "${p:t}"; done
else
  say "no launchd jobs installed under $LABEL_PREFIX.$NAME"
fi

if [[ -n "$POLLER_PIDS$SUPERVISOR_PIDS" ]]; then
  say "running processes to stop:"
  [[ -n "$SUPERVISOR_PIDS" ]] && plan "supervisor: $SUPERVISOR_PIDS"
  [[ -n "$POLLER_PIDS" ]] && plan "poller: $POLLER_PIDS"
fi

if [[ -n "$KEEP_CLAUDE_MD" ]]; then
  say "CLAUDE.md: left alone (--keep-claude-md)"
elif [[ -n "$CLAUDE_MD" ]]; then
  say "CLAUDE.md blocks to remove (ops, brief): $CLAUDE_MD"
  plan "everything outside the markers is left byte-identical"
fi

if [[ -n "$PURGE_STATE" ]]; then
  if [[ -n "$DEGRADED" ]]; then
    say "ERROR: --purge-state needs the full config, and this run is degraded."
    say "       Fix 'python:' (or recreate the venv) and re-run, rather than"
    say "       have this guess at which directory to delete."
    exit 2
  fi
  say "state to DELETE (--purge-state):"
  plan "$STATE_DIR"
  [[ "$LOG_DIR" != "$STATE_DIR"* ]] && plan "$LOG_DIR"
  [[ -n "$CHANNEL_STATE_DIR" && "$CHANNEL_STATE_DIR" != "$LOG_DIR" ]] && plan "$CHANNEL_STATE_DIR"
  plan "this discards the receipt WAL (unanswered messages) and the send journal"
else
  [[ -n "$STATE_DIR" ]] && say "state kept: $STATE_DIR (pass --purge-state to delete)"
fi

if [[ -n "$PURGE_SECRETS" ]]; then
  if [[ -n "$DEGRADED" ]]; then
    say "ERROR: --purge-secrets needs the full config, and this run is degraded."
    exit 2
  fi
  say "secrets to DELETE (--purge-secrets):"
  [[ -n "$TOKEN_FILE" ]] && plan "$TOKEN_FILE"
  [[ -n "$SETTINGS_FILE" ]] && plan "$SETTINGS_FILE"
else
  [[ -n "$TOKEN_FILE" ]] && say "secrets kept: $TOKEN_FILE (pass --purge-secrets to delete)"
fi

say "never touched: the workdir, the venv, and the handoff file"

if [[ -z "$YES" ]]; then
  if [[ -n "$DRY_RUN" ]]; then
    say "dry run — nothing changed."
    exit 0
  fi
  say "nothing has been changed. Re-run with --yes to apply this plan."
  exit 3
fi

# --- do it --------------------------------------------------------------------
for p in $PLISTS; do
  run "launchctl unload '$p' 2>/dev/null || true"
  run "rm -f '$p'"
  say "removed ${p:t}"
done

# TERM, then KILL what is left. launchctl unload should already have stopped the
# supervisor and taken the poller with it; this is for the survivors, and for the
# case where the jobs were never installed but a hand-started supervisor is up.
stop_matching() {
  local what="$1" pattern="$2" waited=0
  pgrep -f "$pattern" >/dev/null 2>&1 || return 0
  pkill -TERM -f "$pattern" 2>/dev/null || true
  while (( waited < 5 )) && pgrep -f "$pattern" >/dev/null 2>&1; do
    sleep 1
    (( waited++ ))
  done
  if pgrep -f "$pattern" >/dev/null 2>&1; then
    pkill -KILL -f "$pattern" 2>/dev/null || true
    say "stopped $what (needed SIGKILL)"
  else
    say "stopped $what"
  fi
}
stop_matching "supervisor" "supervisor\.sh.*$CONFIG"
stop_matching "poller" "poller\.py.*--instance=$NAME"

if [[ -z "$KEEP_CLAUDE_MD" && -n "$CLAUDE_MD" ]]; then
  # Whatever python can be found, because this one is stdlib-only on purpose and
  # the venv may be exactly what the user deleted first.
  BLOCK_PY="${VENV_PY:-}"
  [[ -x "$BLOCK_PY" ]] || BLOCK_PY=$(command -v python3 2>/dev/null)
  [[ -x "$BLOCK_PY" ]] || BLOCK_PY=/usr/bin/python3
  if [[ -x "$BLOCK_PY" ]]; then
    if "$BLOCK_PY" "$LIBEXEC/claude-md-block.py" remove --missing-ok \
         "$CLAUDE_MD" ops brief >/dev/null; then
      say "removed the ops and brief blocks from $CLAUDE_MD"
    else
      say "WARNING: could not clean $CLAUDE_MD — remove the two marker blocks by hand"
      FAILED=1
    fi
  else
    say "WARNING: no python3 found; remove the two marker blocks from $CLAUDE_MD by hand"
    FAILED=1
  fi
fi

if [[ -n "$PURGE_STATE" ]]; then
  for d in "$STATE_DIR" "$LOG_DIR" "$CHANNEL_STATE_DIR"; do
    [[ -n "$d" && -d "$d" ]] && { run "rm -rf '$d'"; say "deleted $d" }
  done
fi

if [[ -n "$PURGE_SECRETS" ]]; then
  for f in "$TOKEN_FILE" "$SETTINGS_FILE"; do
    [[ -n "$f" && -f "$f" ]] && { run "rm -f '$f'"; say "deleted $f" }
  done
  say "NOTE: deleting the token file does not revoke the token. If this bot is"
  say "      finished with, revoke or delete it in BotFather — until you do, the"
  say "      token in your shell history and backups still opens that chat."
fi

(( FAILED )) && { say "finished with errors — see the warnings above"; exit 1 }
say "done. The workdir, the venv and the handoff are untouched."
exit 0
