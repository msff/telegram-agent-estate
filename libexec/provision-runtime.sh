#!/bin/zsh
# Provision — or upgrade — the pinned checkout that launchd executes.
#
# Usage:  provision-runtime.sh provision [options]
#         provision-runtime.sh upgrade   [options]
#
#   --source <url|path>  where to clone from (default: this plugin checkout)
#   --ref <tag|branch|sha>  what to check out (default: the source's HEAD)
#   --copy               skip git entirely and copy the tree (no upgrade path)
#   --no-copy-fallback   fail instead of copying when git cannot clone
#   --force              adopt a moved tag / re-provision an identical pin
#   --no-restart         upgrade: verify and swap, but leave the daemons alone
#   --status             print the current stamp and exit
#   --dry-run            say what would happen; change nothing
#
# ------------------------------------------------------------------------------
# WHY A SEPARATE CHECKOUT AT ALL (KTD1)
#
# Installed plugins land in `~/.claude/plugins/cache/<marketplace>/<plugin>/
# <version>/` — a new directory per version, old ones left behind. A launchd job
# pointed there keeps executing a stale version forever without ever failing,
# which is the failure this estate exists to prevent. The cache carries the
# skill, templates and docs; this script provisions the code the daemons run.
#
# THE PIN IS A COMMIT SHA, NEVER A TAG NAME. A tag is mutable and can be moved
# server-side with no signal on this machine, and what sits behind it here is
# code that runs as the user, on a timer, unattended. A tag names a release; the
# SHA is what you are running, so the SHA is what the stamp records. Asking for
# a tag is fine — it is resolved at provision time and the resolution is what is
# kept. If the same tag later resolves to a different SHA, that is reported and
# refused, not adopted.
#
# THE SOURCE DEFAULTS TO THE LOCAL CHECKOUT. Provisioning from a local path is
# first-class, not a development hack: it is how the first install works before
# a remote exists, how an air-gapped machine works, and how anyone verifying a
# change provisions the tree they just read.
#
# EXIT CODES — this script's own, documented because callers branch on them:
#   0  provisioned, upgraded, or already at the requested pin (a no-op says so)
#   1  the work failed (clone, checkout, copy, swap)
#   2  usage, or a refusal: a synced target, or an upgrade with no upgrade path
#   3  refused: the requested TAG has moved since the stamp was written
#   4  refused: the checked-out HEAD is not the SHA that was asked for
#      (an upgrade stops here, BEFORE restarting anything)
set -u

SELF="${0:A}"
LIBEXEC="${SELF:h}"
PLUGIN_ROOT="${LIBEXEC:h}"

source "$LIBEXEC/estate-runtime.zsh" \
  || { print -ru2 -- "missing $LIBEXEC/estate-runtime.zsh"; exit 2 }

usage() {
  cat <<'EOF'
usage: provision-runtime.sh provision [options]
       provision-runtime.sh upgrade   [options]

  --source <url|path>     where to clone from (default: this plugin checkout)
  --ref <tag|branch|sha>  what to check out (default: the source's HEAD)
  --copy                  skip git entirely and copy the tree (no upgrade path)
  --no-copy-fallback      fail instead of copying when git cannot clone
  --force                 adopt a moved tag / re-provision an identical pin
  --no-restart            upgrade: verify and swap, but leave the daemons alone
  --status                print the current stamp and exit
  --dry-run               say what would happen; change nothing

The pin recorded in the stamp is a resolved commit SHA, never a tag name.
EOF
}

VERB=""
case "${1:-}" in
  # A BARE `provision` / `upgrade` IS REFUSED, and that is not pedantry about
  # arguments. Every option below has a default, so a bare invocation is
  # indistinguishable from a mis-fire — and what it would do is write a checkout
  # that launchd then executes unattended. It cost this unit one real
  # `~/.local/share/` runtime, created by a test that only meant to assert the
  # verb was still a stub. Say what to provision, or ask what is there.
  provision|upgrade)
    if (( $# == 1 )); then
      print -ru2 -- "provision-runtime.sh: '$1' needs at least one explicit argument."
      print -ru2 -- "  It writes the checkout launchd executes; that should be deliberate."
      print -ru2 -- "  Try:  --status            what is provisioned right now"
      print -ru2 -- "        --dry-run           what this would do"
      print -ru2 -- "        --ref <tag>         provision that release"
      print -ru2 -- "        --source <url|path> --ref <tag>"
      exit 2
    fi
    VERB="$1"; shift ;;
  -h|--help) usage; exit 0 ;;
  "") usage >&2; exit 2 ;;
  *) print -ru2 -- "provision-runtime.sh: first argument must be 'provision' or 'upgrade', got '${1}'"
     usage >&2; exit 2 ;;
esac

SOURCE="${ESTATE_RUNTIME_SOURCE:-$PLUGIN_ROOT}"
REF="${ESTATE_RUNTIME_REF:-}"
COPY=""
NO_FALLBACK=""
FORCE=""
NO_RESTART=""
STATUS=""
DRY_RUN=""

while (( $# )); do
  case "$1" in
    --source) SOURCE="${2:-}"; shift 2 ;;
    --source=*) SOURCE="${1#*=}"; shift ;;
    --ref) REF="${2:-}"; shift 2 ;;
    --ref=*) REF="${1#*=}"; shift ;;
    --copy) COPY=1; shift ;;
    --no-copy-fallback) NO_FALLBACK=1; shift ;;
    --force) FORCE=1; shift ;;
    --no-restart) NO_RESTART=1; shift ;;
    --status) STATUS=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) print -ru2 -- "provision-runtime.sh: unknown option '$1'"; usage >&2; exit 2 ;;
  esac
done

# A local source is expanded the way every other path in this stack is; a URL is
# left alone. `:a` and not `:A` — see instance_config._expand() on why symlinks
# are not followed.
case "$SOURCE" in
  *://*|*@*:*) ;;                       # scheme://... or git@host:path
  *) SOURCE="${SOURCE/#\~/$HOME}"; SOURCE="${SOURCE:a}" ;;
esac

ROOT="$(estate_runtime_root)"
TARGET="$(estate_runtime_dir)"
STAMP="$(estate_stamp_file)"
GIT="${ESTATE_GIT:-git}"
AGENTS="${ESTATE_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}"
LAUNCHCTL="${ESTATE_LAUNCHCTL:-launchctl}"

say()  { print -r  -- "[runtime] $*"; }
warn() { print -ru2 -- "[runtime] $*"; }

have_git() { command -v "$GIT" >/dev/null 2>&1; }

# --- the stamp ----------------------------------------------------------------

OLD_PATH="$(estate_stamp_field "$STAMP" path)"       || OLD_PATH=""
OLD_SHA="$(estate_stamp_field "$STAMP" sha)"         || OLD_SHA=""
OLD_REF="$(estate_stamp_field "$STAMP" ref)"         || OLD_REF=""
OLD_REF_FULL="$(estate_stamp_field "$STAMP" ref_full)" || OLD_REF_FULL=""
OLD_MECH="$(estate_stamp_field "$STAMP" mechanism)"  || OLD_MECH=""
OLD_SOURCE="$(estate_stamp_field "$STAMP" source)"   || OLD_SOURCE=""

if [[ -n "$STATUS" ]]; then
  if [[ -z "$OLD_MECH" ]]; then
    say "no runtime provisioned (no stamp at $STAMP)"
    exit 0
  fi
  say "stamp     $STAMP"
  say "path      $OLD_PATH"
  say "mechanism $OLD_MECH"
  say "source    $OLD_SOURCE"
  say "ref       ${OLD_REF:-(none)}${OLD_REF_FULL:+  ($OLD_REF_FULL)}"
  say "sha       ${OLD_SHA:-(none — a copied runtime is not pinned)}"
  say "version   $(estate_stamp_field "$STAMP" plugin_version)"
  say "at        $(estate_stamp_field "$STAMP" provisioned_at)"
  [[ -d "$TARGET/libexec" ]] || warn "the checkout is MISSING — re-provision"
  exit 0
fi

json_esc() {
  local s="${1:-}"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  print -r -- "$s"
}

# The stamp is the only durable record of which code the daemons run, so it is
# written LAST and atomically: a stamp that exists always describes a checkout
# that is fully in place. The inverse — a checkout with no stamp — is how an
# interrupted provision is detected on the next run.
write_stamp() {
  local mech="$1" src="$2" ref="$3" ref_full="$4" sha="$5"
  local version="" tmp="$STAMP.tmp.$$"
  version=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
            "$TARGET/.claude-plugin/plugin.json" 2>/dev/null | head -1)
  {
    print -r -- "{"
    print -r -- "  \"schema\": \"1\","
    print -r -- "  \"path\": \"$(json_esc "$TARGET")\","
    print -r -- "  \"mechanism\": \"$(json_esc "$mech")\","
    print -r -- "  \"upgradable\": \"$([[ "$mech" == git ]] && print -n yes || print -n no)\","
    print -r -- "  \"source\": \"$(json_esc "$src")\","
    print -r -- "  \"ref\": \"$(json_esc "$ref")\","
    print -r -- "  \"ref_full\": \"$(json_esc "$ref_full")\","
    print -r -- "  \"sha\": \"$(json_esc "$sha")\","
    print -r -- "  \"plugin_version\": \"$(json_esc "$version")\","
    print -r -- "  \"provisioned_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
    if [[ "$mech" == copy ]]; then
      print -r -- "  \"note\": \"copied, not cloned: this runtime has NO upgrade path. Re-provision with git available to get one.\""
    else
      print -r -- "  \"note\": \"the pin is the sha, not the ref. Upgrades are explicit: telegram-agent-estate upgrade --ref <tag>.\""
    fi
    print -r -- "}"
  } > "$tmp" || return 1
  mv -f "$tmp" "$STAMP"
}

# --- refusals that come before any work --------------------------------------

if reason=$(estate_synced_path_reason "$ROOT"); then
  warn "REFUSING to provision into $reason:"
  warn "    $ROOT"
  warn "  A sync client rewrites files underneath the process reading them, and"
  warn "  the daemons read this checkout on every turn. Point \$ESTATE_RUNTIME_ROOT"
  warn "  at a local path — the default, ~/.local/share/telegram-agent-estate, is one."
  exit 2
fi

# --- partial-state detection --------------------------------------------------
#
# Two shapes, one cause: something died between the swap and the stamp, or
# between the stamp and the swap. Neither is recoverable by inspection, and both
# are cheap to redo, so both are reported and discarded rather than adopted.
PARTIAL=""
for stale in "$ROOT"/runtime.incoming.*(N) "$ROOT"/runtime.old.*(N); do
  say "discarding leftovers from an interrupted provision: ${stale:t}"
  [[ -n "$DRY_RUN" ]] || rm -rf "$stale"
  PARTIAL=1
done

if [[ -d "$TARGET" && -z "$OLD_MECH" ]]; then
  say "a checkout exists at $TARGET with no stamp — an interrupted provision."
  say "  discarding it and provisioning again."
  [[ -n "$DRY_RUN" ]] || rm -rf "$TARGET"
  PARTIAL=1
elif [[ -n "$OLD_MECH" && ! -d "$TARGET/libexec" ]]; then
  say "the stamp names a checkout that is not there ($TARGET) — re-provisioning."
  OLD_SHA=""; OLD_REF=""; OLD_MECH=""
  PARTIAL=1
fi

# --- what are we being asked for? --------------------------------------------

REQ_SHA=""
REF_FULL=""
REF_KIND="none"          # tag | branch | sha | head | none

resolve_ref() {
  local out line sha name peeled="" tagged="" branch=""
  local -a lines
  if [[ -z "$REF" ]]; then
    out=$("$GIT" ls-remote "$SOURCE" HEAD 2>/dev/null) || return 1
    # `lines=(${(f)out})` and not `${${(f)out}[1]}`: nested, the subscript
    # indexes the first CHARACTER of the scalar, so the pin silently became "7".
    # It cost nothing only because the copy fallback caught the failed checkout.
    lines=(${(f)out})
    (( ${#lines} )) || return 1
    REQ_SHA="${lines[1]%%[[:space:]]*}"
    [[ -n "$REQ_SHA" ]] || return 1
    REF_FULL="HEAD"; REF_KIND="head"
    return 0
  fi
  out=$("$GIT" ls-remote "$SOURCE" "refs/tags/$REF" "refs/tags/$REF^{}" \
                                   "refs/heads/$REF" 2>/dev/null) || out=""
  for line in ${(f)out}; do
    sha="${line%%[[:space:]]*}"
    name="${line##*[[:space:]]}"
    case "$name" in
      "refs/tags/$REF^{}") peeled="$sha" ;;
      "refs/tags/$REF")    tagged="$sha" ;;
      "refs/heads/$REF")   branch="$sha" ;;
    esac
  done
  # A peeled entry is the COMMIT an annotated tag points at; the unpeeled one is
  # the tag object itself, which is not what a checkout lands on.
  if [[ -n "$peeled" ]]; then
    REQ_SHA="$peeled"; REF_FULL="refs/tags/$REF"; REF_KIND="tag"
  elif [[ -n "$tagged" ]]; then
    REQ_SHA="$tagged"; REF_FULL="refs/tags/$REF"; REF_KIND="tag"
  elif [[ -n "$branch" ]]; then
    REQ_SHA="$branch"; REF_FULL="refs/heads/$REF"; REF_KIND="branch"
  else
    # Not a ref the source publishes. Treat it as a raw SHA and let the
    # checkout be the judge — `git checkout <sha>` fails loudly on a typo.
    REQ_SHA="$REF"; REF_FULL=""; REF_KIND="sha"
  fi
  return 0
}

MECH="git"
if [[ -n "$COPY" ]]; then
  MECH="copy"
elif ! have_git; then
  if [[ -n "$NO_FALLBACK" ]]; then
    warn "git is not available ($GIT) and --no-copy-fallback was given."
    exit 1
  fi
  warn "git is not available ($GIT) — falling back to a copy."
  MECH="copy"
elif ! resolve_ref; then
  if [[ -n "$NO_FALLBACK" ]]; then
    warn "cannot reach the source to resolve a pin: $SOURCE"
    exit 1
  fi
  warn "cannot reach $SOURCE to resolve a pin — falling back to a copy."
  MECH="copy"
fi

if [[ "$MECH" == copy ]]; then
  REQ_SHA=""; REF_FULL=""; REF_KIND="none"
fi

# --- upgrade has preconditions provisioning does not -------------------------

if [[ "$VERB" == upgrade ]]; then
  if [[ -z "$OLD_MECH" ]]; then
    warn "nothing to upgrade: no runtime is provisioned."
    warn "  telegram-agent-estate provision --source <url-or-path> --ref <tag>"
    exit 2
  fi
  if [[ "$OLD_MECH" == copy && "$MECH" == copy ]]; then
    warn "this runtime was COPIED, not cloned, and a copy has no upgrade path."
    warn "  Re-provision with git available:"
    warn "    telegram-agent-estate provision --source <url-or-path> --ref <tag>"
    exit 2
  fi
fi

# --- the moved-tag check ------------------------------------------------------
#
# A tag is the one ref kind whose SHA is supposed to be stable. A branch moving
# is a branch doing its job; a tag moving means the release you pinned is not
# the release you would get now, and adopting that silently is exactly what
# pinning to a SHA was meant to prevent.
if [[ "$REF_KIND" == tag && -n "$OLD_SHA" && -n "$REQ_SHA" \
      && "$OLD_REF" == "$REF" && "$OLD_SHA" != "$REQ_SHA" && -z "$FORCE" ]]; then
  warn "the tag '$REF' has MOVED since this runtime was provisioned."
  warn "    stamped: $OLD_SHA"
  warn "    now:     $REQ_SHA"
  warn "  A tag can be moved server-side with no signal here, and this checkout"
  warn "  runs as you, on a timer. Read the diff between those two commits before"
  warn "  accepting it. Then either:"
  warn "    telegram-agent-estate $VERB --ref $REQ_SHA     # pin the commit"
  warn "    telegram-agent-estate $VERB --ref $REF --force # accept the move"
  exit 3
fi

# --- the no-op ----------------------------------------------------------------

if [[ -z "$FORCE" && "$MECH" == git && -n "$REQ_SHA" && "$OLD_MECH" == git \
      && "$OLD_SHA" == "$REQ_SHA" && -d "$TARGET/libexec" ]]; then
  say "already at $REQ_SHA${REF:+ ($REF)} — nothing to do."
  say "  $TARGET"
  if [[ "$VERB" == upgrade && -z "$NO_RESTART" ]]; then
    say "  (no restart: the code did not change)"
  fi
  exit 0
fi

if [[ -n "$DRY_RUN" ]]; then
  say "would $VERB $TARGET"
  say "  from      $SOURCE"
  say "  mechanism $MECH"
  say "  ref       ${REF:-HEAD}"
  say "  sha       ${REQ_SHA:-(copy — unpinned)}"
  [[ -n "$OLD_SHA" ]] && say "  replacing $OLD_SHA"
  exit 0
fi

# --- do the work --------------------------------------------------------------

INCOMING="$ROOT/runtime.incoming.$$"
mkdir -p "$ROOT" || { warn "cannot create $ROOT"; exit 1 }
rm -rf "$INCOMING"

cleanup_incoming() { rm -rf "$INCOMING"; }

clone_runtime() {
  # A full clone, deliberately: `--depth 1` cannot check out an arbitrary SHA,
  # and the SHA is the pin. Cloning a local path hardlinks its objects, so the
  # cost of "full" against a local source is close to nothing.
  "$GIT" clone --quiet --no-checkout "$SOURCE" "$INCOMING" || return 1
  "$GIT" -C "$INCOMING" checkout --quiet --detach "$REQ_SHA" || return 1
  return 0
}

copy_runtime() {
  mkdir -p "$INCOMING" || return 1
  cp -R "$SOURCE/." "$INCOMING/" || return 1
  # `.git` is a directory in a clone and a FILE in a worktree; both go.
  find "$INCOMING" \( -name .git -o -name __pycache__ -o -name .pytest_cache \
                      -o -name '.DS_Store' \) -prune -exec rm -rf {} + 2>/dev/null
  return 0
}

if [[ "$MECH" == git ]]; then
  if ! clone_runtime; then
    cleanup_incoming
    if [[ -n "$NO_FALLBACK" ]]; then
      warn "clone failed: $SOURCE at ${REF:-HEAD}"
      exit 1
    fi
    warn "clone failed ($SOURCE at ${REF:-HEAD}) — falling back to a copy."
    warn "  A COPIED RUNTIME HAS NO UPGRADE PATH. Re-provision once git can"
    warn "  reach the source; \`provision --status\` will keep saying 'copy'"
    warn "  until you do."
    MECH="copy"; REQ_SHA=""; REF_FULL=""; REF_KIND="none"
  fi
fi

if [[ "$MECH" == copy ]]; then
  if ! copy_runtime; then
    cleanup_incoming
    warn "could not copy $SOURCE -> $INCOMING"
    exit 1
  fi
fi

# --- verify the pin BEFORE anything is swapped or restarted -------------------
#
# FIRST, ahead of the shape check below. A checkout that did not happen leaves a
# tree that is also the wrong shape, and "no libexec/ here" would send someone
# looking at the source repo when the actual fault is that HEAD never moved.
#
# The whole point of recording a SHA is that it can be checked. `git checkout`
# reporting success is not the same claim as "HEAD is the commit you asked for":
# a raced index, a hook, a filter, or a truncated fetch can all leave HEAD
# elsewhere. An upgrade that restarted daemons on an unverified tree would be
# the auto-update-by-accident this design exists to forbid.
if [[ "$MECH" == git ]]; then
  # An abbreviated SHA is a legitimate way to ask, but it is not a legitimate
  # thing to RECORD: the stamp has to name the commit unambiguously and forever.
  # Expanding it inside the clone we just made is not a weakening of the check —
  # an abbreviation that resolves to some other commit still fails below.
  if [[ "$REF_KIND" == sha ]]; then
    FULL=$("$GIT" -C "$INCOMING" rev-parse --verify --quiet "${REQ_SHA}^{commit}" 2>/dev/null)
    [[ -n "$FULL" ]] && REQ_SHA="$FULL"
  fi
  HEAD_SHA=$("$GIT" -C "$INCOMING" rev-parse HEAD 2>/dev/null)
  if [[ -z "$HEAD_SHA" || "$HEAD_SHA" != "$REQ_SHA" ]]; then
    cleanup_incoming
    warn "REFUSING: the checked-out HEAD is not the commit that was requested."
    warn "    requested: ${REQ_SHA:-(none)}"
    warn "    HEAD:      ${HEAD_SHA:-(unreadable)}"
    warn "  Nothing was swapped and no daemon was restarted."
    exit 4
  fi
fi

if [[ ! -d "$INCOMING/libexec" ]]; then
  cleanup_incoming
  warn "$SOURCE does not look like this plugin: no libexec/ in the provisioned tree."
  exit 1
fi

OLD_KEEP=""
if [[ -d "$TARGET" ]]; then
  OLD_KEEP="$ROOT/runtime.old.$$"
  mv "$TARGET" "$OLD_KEEP" || { cleanup_incoming; warn "cannot move aside $TARGET"; exit 1 }
fi
if ! mv "$INCOMING" "$TARGET"; then
  warn "cannot move $INCOMING into place at $TARGET"
  [[ -n "$OLD_KEEP" ]] && mv "$OLD_KEEP" "$TARGET"
  cleanup_incoming
  exit 1
fi
[[ -n "$OLD_KEEP" ]] && rm -rf "$OLD_KEEP"

write_stamp "$MECH" "$SOURCE" "$REF" "$REF_FULL" "$REQ_SHA" \
  || { warn "the checkout is in place but the stamp could not be written"; exit 1 }

if [[ -n "$OLD_SHA" && "$OLD_SHA" != "$REQ_SHA" ]]; then
  say "$VERB: $OLD_SHA -> ${REQ_SHA:-(copy — unpinned)}"
elif [[ -n "$PARTIAL" ]]; then
  say "$VERB: recovered and provisioned ${REQ_SHA:-(copy — unpinned)}"
else
  say "$VERB: ${REQ_SHA:-(copy — unpinned)}${REF:+ ($REF)}"
fi
say "  $TARGET"
say "  mechanism $MECH"
if [[ "$MECH" == copy ]]; then
  say "  NOTE: a copied runtime is not pinned and cannot be upgraded in place."
fi

# --- restart --------------------------------------------------------------

# Only supervisors. A scheduled job execs fresh out of the runtime on every
# fire, so it picks the new code up by itself; a supervisor is a loop that has
# already read the old one.
restart_supervisors() {
  local -a labels
  local p label
  for p in "$AGENTS"/*.plist(N); do
    grep -q -- "$TARGET/libexec/supervisor.sh" "$p" 2>/dev/null || continue
    label=$(/usr/bin/perl -0ne \
            'print $1 if m{<key>Label</key>\s*<string>([^<]*)</string>}' "$p" 2>/dev/null)
    [[ -n "$label" ]] && labels+=("$label")
  done
  if (( ${#labels} == 0 )); then
    say "no supervisor points at this runtime — nothing to restart."
    return 0
  fi
  for label in $labels; do
    if "$LAUNCHCTL" kickstart -k "gui/$UID/$label" >/dev/null 2>&1; then
      say "restarted $label"
    else
      warn "could not restart $label — do it by hand:"
      warn "    launchctl kickstart -k gui/$UID/$label"
    fi
  done
}

if [[ "$VERB" == upgrade ]]; then
  if [[ -n "$NO_RESTART" ]]; then
    say "not restarting (--no-restart). The daemons keep running the OLD code"
    say "until you restart them — that is what --no-restart means."
  else
    restart_supervisors
  fi
else
  say "nothing was restarted: provisioning does not touch running daemons."
  say "  Install an instance against it:  telegram-agent-estate install <instance.yaml>"
fi
exit 0
