# Runtime-pin resolution. Sourced — never run — by the dispatcher, the
# installer and the provisioner.
#
#   source "$LIBEXEC/estate-runtime.zsh"
#   estate_resolve_libexec "$PLUGIN_ROOT"   # sets ESTATE_LIBEXEC_DIR/_ORIGIN
#
# ------------------------------------------------------------------------------
# WHY THIS EXISTS (KTD1)
#
# An installed plugin lands in `~/.claude/plugins/cache/<marketplace>/<plugin>/
# <version>/` — A NEW DIRECTORY PER VERSION, with the old ones left behind. A
# launchd daemon pointed at one of those does not break loudly when the plugin
# updates. It keeps executing a version nobody chose, forever, silently, which
# is the exact failure this estate was built to eliminate.
#
# So the cache carries the skill, the templates and the docs, and a SEPARATE
# pinned checkout — provisioned by `provision-runtime.sh`, recorded in a stamp
# file — is what launchd executes and what an upgrade, and only an upgrade,
# moves.
#
# That split creates a second question, which is what this file answers: when
# somebody types `telegram-agent-estate parity`, WHICH copy runs? The dispatcher
# is necessarily cache-resident (that is the directory Claude Code puts on
# PATH). If it ran the code next to itself, the one command a user runs to
# confirm the daemon is healthy would be verifying a different tree from the one
# the daemon executes — the single place the disagreement is visible would be
# the one place that cannot see it.
#
# ------------------------------------------------------------------------------
# THE RULE, AND THE TENSION IT RESOLVES
#
# Read literally, KTD1 says: resolve through the stamp, and fail closed when
# there is no stamp. Applied everywhere that also breaks every development
# checkout — including this repo and its own test suite, where no stamp exists
# and self-relative resolution is the only correct answer. Both halves matter,
# so the rule is conditioned on WHERE THE CALLER LIVES:
#
#   1. $ESTATE_LIBEXEC set          -> that, verbatim. The explicit override
#                                      wins over everything, so the strict path
#                                      is testable and an operator can pin by
#                                      hand.
#   2. caller inside a plugin cache -> the stamp, or FAIL with a provision
#                                      instruction. Never the cache's own copy:
#                                      that is precisely the staleness above.
#   3. anywhere else (a checkout)   -> self-relative. A checkout is by
#                                      definition the code the person is looking
#                                      at; silently redirecting it elsewhere
#                                      would be the same class of surprise,
#                                      inverted.
#
# Case 3 is not silent when it diverges: if a runtime stamp exists and names a
# different tree, the caller is told on stderr which pin the daemons are on. The
# answer stays "your checkout" — it just stops being a secret that the daemons
# are running something else.

# estate_runtime_root
#
# Where the pinned checkout and its stamp live. `~/.local/share/` is the
# convention the per-instance venvs already follow, and it is deliberately not
# under any directory a sync client watches: a checkout that a daemon executes
# must not be rewritten by a third party mid-read.
#
# $ESTATE_RUNTIME_ROOT overrides it. That exists for the test suite and for an
# operator who keeps runtimes elsewhere; it moves the checkout AND the stamp
# together, because the two are only meaningful as a pair.
estate_runtime_root() {
  print -r -- "${ESTATE_RUNTIME_ROOT:-$HOME/.local/share/telegram-agent-estate}"
}

# estate_runtime_dir / estate_stamp_file — the pair, derived from the root.
estate_runtime_dir() { print -r -- "$(estate_runtime_root)/runtime"; }
estate_stamp_file()  { print -r -- "$(estate_runtime_root)/runtime.json"; }

# estate_stamp_field <stamp-file> <key>
#
# One string value out of the stamp, with sed. NOT a JSON parse, and for the
# same reason `estate-bootstrap.zsh` does not parse YAML (KTD4): the dispatcher
# has no instance config yet, therefore no venv interpreter, therefore no
# guaranteed python. The stamp is written by `provision-runtime.sh` as one
# `"key": "value"` per line with quotes and backslashes escaped, so this is
# reading back a format we control on both ends — it is not a general reader.
estate_stamp_field() {
  local stamp="${1:-}" key="${2:-}"
  [[ -n "$stamp" && -r "$stamp" ]] || return 1
  sed -n "s/.*\"$key\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" \
      "$stamp" 2>/dev/null | head -1
}

# estate_is_cache_resident <path> — true when <path> is inside a plugin cache.
#
# `<...>/plugins/cache/<marketplace>/<plugin>/<version>/` is the shape Claude
# Code installs into. Matching the segment rather than an absolute prefix keeps
# this true for a relocated `~/.claude` and testable without one.
estate_is_cache_resident() {
  case "${1:-}" in
    */plugins/cache/*) return 0 ;;
  esac
  return 1
}

# estate_synced_path_reason <path>
#
# Prints the offending service and returns 0 when the path is inside a folder a
# sync client rewrites. A runtime there is not a runtime: the daemon reads these
# files on every turn, and a sync daemon that "restores" one mid-read produces
# an outage whose cause is invisible from the logs. The same class of failure
# already cost this project a rebase and a venv.
estate_synced_path_reason() {
  case "${1:-}" in
    *Dropbox*)                            print -r -- "Dropbox"; return 0 ;;
    *"Google Drive"*|*GoogleDrive*)       print -r -- "Google Drive"; return 0 ;;
    *iCloud*|*"Library/Mobile Documents"*) print -r -- "iCloud Drive"; return 0 ;;
    *"Library/CloudStorage"*)             print -r -- "a CloudStorage provider"; return 0 ;;
  esac
  return 1
}

# estate_resolve_libexec <plugin_root>
#
# Sets ESTATE_LIBEXEC_DIR and ESTATE_LIBEXEC_ORIGIN (env|stamp|self).
# Returns 2, with the instruction on stderr, when the caller is cache-resident
# and no usable runtime exists.
estate_resolve_libexec() {
  local plugin_root="${1:-}" stamp runtime sha
  ESTATE_LIBEXEC_DIR=""
  ESTATE_LIBEXEC_ORIGIN=""

  if [[ -n "${ESTATE_LIBEXEC:-}" ]]; then
    ESTATE_LIBEXEC_DIR="${ESTATE_LIBEXEC:a}"
    ESTATE_LIBEXEC_ORIGIN="env"
    return 0
  fi

  stamp="$(estate_stamp_file)"
  runtime="$(estate_stamp_field "$stamp" path)" || runtime=""
  sha="$(estate_stamp_field "$stamp" sha)" || sha=""

  if estate_is_cache_resident "$plugin_root"; then
    if [[ -n "$runtime" && -d "$runtime/libexec" ]]; then
      ESTATE_LIBEXEC_DIR="$runtime/libexec"
      ESTATE_LIBEXEC_ORIGIN="stamp"
      return 0
    fi
    print -ru2 -- "telegram-agent-estate: no provisioned runtime to run."
    print -ru2 -- ""
    print -ru2 -- "  This copy of the plugin lives in the plugin cache:"
    print -ru2 -- "    $plugin_root"
    print -ru2 -- "  Every plugin update installs into a NEW cache directory and leaves the"
    print -ru2 -- "  old one behind, so a daemon pointed here would go on executing a version"
    print -ru2 -- "  you no longer have installed. The daemons run a pinned checkout instead,"
    print -ru2 -- "  and this command refuses to verify the cache's copy in its place."
    if [[ -n "$runtime" ]]; then
      print -ru2 -- ""
      print -ru2 -- "  The stamp at $stamp names a runtime that is not there:"
      print -ru2 -- "    $runtime"
    fi
    print -ru2 -- ""
    print -ru2 -- "  Provision one:"
    print -ru2 -- "    telegram-agent-estate provision --source <url-or-path> --ref <tag>"
    print -ru2 -- ""
    print -ru2 -- "  Or name a tree explicitly (development, and only that):"
    print -ru2 -- "    ESTATE_LIBEXEC=<checkout>/libexec telegram-agent-estate ..."
    return 2
  fi

  ESTATE_LIBEXEC_DIR="$plugin_root/libexec"
  ESTATE_LIBEXEC_ORIGIN="self"

  # Permissive, but not quiet. A checkout runs its own code — and says so when
  # that is not the code the daemons are on.
  if [[ -n "$runtime" && -d "$runtime/libexec" \
        && "$runtime/libexec" != "$ESTATE_LIBEXEC_DIR" ]]; then
    print -ru2 -- "note: running this checkout ($plugin_root), not the provisioned runtime."
    print -ru2 -- "      The daemons execute $runtime${sha:+ at $sha}."
  fi
  return 0
}
