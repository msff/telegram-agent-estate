#!/bin/zsh
# Run the parity suite against one instance — the self-test a freshly installed
# instance runs to prove itself, and the CLI-version tripwire's gate.
#
# Usage:  parity.sh <instance.yaml> [--real]
#
# TWO MODES, AND THE DIFFERENCE IS THE POINT:
#
#   fast (default) — injected fake invoker, ~1s, no tokens, no sends. Catches
#     regressions in the WAL, coalescing, retry and dead-letter logic.
#
#   --real — drives actual `claude -p` turns and SENDS `[PARITY]` messages to
#     the owner chat. This is the only mode that can prove the permission
#     allowlist is complete, because a missing rule shows up as a turn that
#     exits 0 and sends nothing — invisible to fast mode by construction, since
#     fast mode's invoker never consults the allowlist at all.
#
# Real mode earned its cost on the first instance immediately: it caught a
# rotation reporting success after writing nothing, and an allowlist gap that
# every fast run had been green about.
set -u

CONFIG=""
REAL=""
for arg in "$@"; do
  case "$arg" in
    --real) REAL=1 ;;
    -h|--help) echo "usage: parity.sh <instance.yaml> [--real]"; exit 0 ;;
    *) CONFIG="$arg" ;;
  esac
done
[[ -z "$CONFIG" ]] && { echo "usage: parity.sh <instance.yaml> [--real]" >&2; exit 2; }
CONFIG="${CONFIG/#\~/$HOME}"
CONFIG="${CONFIG:A}"
[[ -r "$CONFIG" ]] || { echo "unreadable instance config: $CONFIG" >&2; exit 2; }
export ESTATE_INSTANCE_CONFIG="$CONFIG"

BINDIR="${0:A:h}"
PLUGIN_ROOT="${BINDIR:h}"

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
print(f"VENV_PY={q(c.python)}")
print(f"WORKDIR={q(c.workdir)}")
PY
)" || { echo "failed to load instance config $CONFIG" >&2; exit 2; }

echo "[parity:$NAME] config=$CONFIG mode=${REAL:+real}${REAL:-fast}"

# The turn's billing must stay on the subscription (KTD1). An API key in the
# environment would silently move a --real run onto metered credits.
unset ANTHROPIC_API_KEY
# And the bot token must not reach a turn: the auto-loading channel plugin would
# poll it and 409-war this instance's live poller.
unset TELEGRAM_BOT_TOKEN

cd "$WORKDIR" || { echo "missing workdir $WORKDIR" >&2; exit 1; }

args=(-m pytest "$PLUGIN_ROOT/tests/test_gateway_parity.py" -q)
[[ -n "$REAL" ]] && args+=(--real)

PYTHONPATH="$BINDIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV_PY" "${args[@]}"
rc=$?
if (( rc == 0 )); then
  echo "[parity:$NAME] PASS"
else
  echo "[parity:$NAME] FAIL (exit $rc)" >&2
fi
exit $rc
