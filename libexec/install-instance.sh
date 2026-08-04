#!/bin/zsh
# Install (or re-install) one instance's launchd jobs from its config.
#
# Usage:  install-instance.sh <instance.yaml> [--dry-run] [--no-load]
#
# Renders templates/plists/*.tmpl into ~/Library/LaunchAgents and loads them.
# Idempotent: re-running after a config edit unloads, re-renders, and reloads.
#
# COPY, NEVER SYMLINK. `launchctl load` of a symlink whose target lives in
# Dropbox fails with EIO on macOS 15, and the failure is silent enough to look
# like "the job just didn't fire".
#
# REFUSES TO CLOBBER ANOTHER INSTANCE. Everything launchd-visible is namespaced
# by `name`, so installing two instances that share a name would have the second
# silently take over the first's labels, logs, and schedule. That check is the
# one thing this script will not let you override.
set -u

DRY_RUN=""
NO_LOAD=""
CONFIG=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-load) NO_LOAD=1 ;;
    -h|--help) echo "usage: install-instance.sh <instance.yaml> [--dry-run] [--no-load]"; exit 0 ;;
    *) CONFIG="$arg" ;;
  esac
done
[[ -z "$CONFIG" ]] && { echo "usage: install-instance.sh <instance.yaml> [--dry-run] [--no-load]" >&2; exit 2; }
CONFIG="${CONFIG/#\~/$HOME}"
CONFIG="${CONFIG:A}"
[[ -r "$CONFIG" ]] || { echo "unreadable instance config: $CONFIG" >&2; exit 2; }

# The directory this script sits in is what gets stamped into every plist as an
# absolute path, so launchd holds a copy of it until the next install. It is
# `libexec/` and not `bin/` since U4 (KTD5) — the plists deliberately do not
# route through the `bin/` dispatcher, because a scheduled job must not depend
# on PATH, and `exec`ing one more process buys nothing here.
LIBEXEC="${0:A:h}"
PLUGIN_ROOT="${LIBEXEC:h}"
# Overridable so the two-instance isolation drill can render real plists into a
# scratch directory. Nothing else should set it: an instance installed outside
# ~/Library/LaunchAgents is never loaded by launchd, so it looks installed and
# silently never fires.
AGENTS="${ESTATE_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}"

# Resolve THIS instance's interpreter out of its own config before doing
# anything else. The ambient `python3` is not usable — see estate-bootstrap.zsh.
source "$LIBEXEC/estate-bootstrap.zsh" \
  || { echo "missing $LIBEXEC/estate-bootstrap.zsh" >&2; exit 2; }
estate_resolve_python "$CONFIG" || exit $?

eval "$("$VENV_PY" - "$LIBEXEC" "$CONFIG" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import instance_config
c = instance_config.load(sys.argv[2])
def q(v):
    return "'" + str(v).replace("'", "'\\''") + "'"
print(f"NAME={q(c.name)}")
print(f"LABEL_PREFIX={q(c.launchd_prefix)}")
print(f"LOGDIR={q(c.log_dir)}")
print(f"WORKDIR={q(c.workdir)}")
print(f"VENV_PY={q(c.python)}")
print(f"TOKEN_FILE={q(c.token_file)}")
print(f"GW_STATE={q(c.gateway_state_dir)}")
PY
)" || { echo "failed to load instance config $CONFIG" >&2; exit 2; }

say() { echo "[install:$NAME] $*"; }
run() { if [[ -n "$DRY_RUN" ]]; then echo "  would: $*"; else eval "$@"; fi }

# --- preflight ---------------------------------------------------------------
# `python not executable` is no longer checked here: estate_resolve_python
# enforces it above, before the config can even be read, and emits the same
# wording. Repeating it here would be unreachable.
fail=0
[[ -d "$WORKDIR" ]] || { say "ERROR: workdir does not exist: $WORKDIR"; fail=1; }

case "$VENV_PY" in
  *Dropbox*|*"Google Drive"*|*iCloud*|*"Library/Mobile Documents"*)
    say "ERROR: python lives in a synced folder ($VENV_PY)."
    say "       Synced venvs corrupt, and a lock file on a synced volume is not a lock."
    fail=1 ;;
esac

if [[ -f "$TOKEN_FILE" ]]; then
  perms=$(stat -f %A "$TOKEN_FILE" 2>/dev/null || echo "")
  [[ "$perms" == "600" ]] || say "WARNING: $TOKEN_FILE is mode $perms, expected 600"
else
  say "WARNING: token file missing: $TOKEN_FILE (the poller will exit until it exists)"
fi

# The clobber check. Any installed plist carrying this name but pointing at a
# DIFFERENT config is another instance wearing the same identity.
for existing in "$AGENTS/$LABEL_PREFIX.$NAME"*.plist(N); do
  other=$(sed -n 's|.*supervisor\.sh \([^<]*\)</string>.*|\1|p;s|.*run-job\.sh \([^ ]*\) .*|\1|p' "$existing" 2>/dev/null | head -1)
  if [[ -n "$other" && "$other" != "$CONFIG" ]]; then
    say "ERROR: $existing already belongs to a different instance config:"
    say "         installed: $other"
    say "         this run:  $CONFIG"
    say "       Two instances sharing the name '$NAME' would silently take over"
    say "       each other's labels, logs, locks and schedule. Rename one."
    fail=1
  fi
done

(( fail )) && { say "preflight failed — nothing installed"; exit 1; }

mkdir -p "$AGENTS" "$LOGDIR" "$GW_STATE"

# --- render ------------------------------------------------------------------
render() {
  # render <template> <dest> <k=v>...
  local tmpl="$1" dest="$2"; shift 2
  local body
  body=$(<"$tmpl")
  for pair in "$@"; do
    local k="${pair%%=*}" v="${pair#*=}"
    body="${body//\{\{$k\}\}/$v}"
  done
  if [[ -n "$DRY_RUN" ]]; then
    echo "  would write $dest"
  else
    print -r -- "$body" > "$dest"
  fi
}

COMMON=(
  "name=$NAME" "label_prefix=$LABEL_PREFIX" "plugin_libexec=$LIBEXEC"
  "instance_config=$CONFIG" "log_dir=$LOGDIR" "home=$HOME"
  "path=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:$HOME/.local/bin"
)

SUP_PLIST="$AGENTS/$LABEL_PREFIX.$NAME.plist"
say "supervisor -> $SUP_PLIST"
if [[ -z "$DRY_RUN" ]]; then
  launchctl unload "$SUP_PLIST" 2>/dev/null || true
fi
render "$PLUGIN_ROOT/templates/plists/supervisor.plist.tmpl" "$SUP_PLIST" "${COMMON[@]}"

# --- scheduled jobs ----------------------------------------------------------
JOBS=$("$VENV_PY" - "$LIBEXEC" "$CONFIG" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import instance_config
c = instance_config.load(sys.argv[2])
for s in c.schedules:
    job = s.get("job")
    if not job:
        continue
    print("\t".join([job, str(s.get("hour", 9)), str(s.get("minute", 0)),
                     str(s.get("weekday", ""))]))
PY
) || { say "ERROR: could not read schedules"; exit 2; }

if [[ -n "$JOBS" ]]; then
  while IFS=$'\t' read -r job hour minute weekday; do
    [[ -z "$job" ]] && continue
    dest="$AGENTS/$LABEL_PREFIX.$NAME-$job.plist"
    say "job '$job' ${weekday:+weekday=$weekday }$hour:$minute -> $dest"
    [[ -z "$DRY_RUN" ]] && { launchctl unload "$dest" 2>/dev/null || true; }
    render "$PLUGIN_ROOT/templates/plists/job.plist.tmpl" "$dest" \
      "${COMMON[@]}" "job=$job" "hour=$hour" "minute=$minute" "weekday=$weekday"
    if [[ -z "$DRY_RUN" ]]; then
      # The weekday block is a mustache-style conditional. It must be treated as
      # a TEXT range, not a line range: `{{/weekday}}` shares its line with the
      # `<key>Hour</key>` that follows it, so a line-based delete strips the hour
      # key and leaves its <integer> orphaned inside the dict — which plutil
      # rejects as "non-key inside <dict>". Caught by the lint below, which is
      # why it is here.
      if [[ -n "$weekday" ]]; then
        /usr/bin/perl -0pi -e 's/\{\{[#\/]weekday\}\}//g' "$dest"
      else
        /usr/bin/perl -0pi -e 's/\{\{#weekday\}\}.*?\{\{\/weekday\}\}//s' "$dest"
      fi
      plutil -lint "$dest" >/dev/null || { say "ERROR: $dest is not valid plist"; exit 1; }
    fi
  done <<< "$JOBS"
else
  say "no scheduled jobs declared"
fi

if [[ -z "$DRY_RUN" ]]; then
  plutil -lint "$SUP_PLIST" >/dev/null || { say "ERROR: $SUP_PLIST is not valid plist"; exit 1; }
fi

# --- load --------------------------------------------------------------------
if [[ -n "$NO_LOAD" || -n "$DRY_RUN" ]]; then
  say "not loading (--no-load/--dry-run). Load with:"
  say "  launchctl load -w $SUP_PLIST"
else
  for p in "$AGENTS/$LABEL_PREFIX.$NAME.plist" "$AGENTS/$LABEL_PREFIX.$NAME-"*.plist(N); do
    launchctl load -w "$p" && say "loaded ${p:t}" || say "WARNING: failed to load ${p:t}"
  done
fi

say "done. Verify with:  $LIBEXEC/parity.sh $CONFIG"
