# Interpreter bootstrap, sourced by every shell entry point.
#
#   BINDIR="${0:A:h}"
#   source "$BINDIR/estate-bootstrap.zsh"
#   estate_resolve_python "$CONFIG"      # sets VENV_PY, or fails with a message
#
# NOT executable and NOT a command: it defines one function and returns. (That
# also keeps it inert in `bin/`, which Claude puts on the Bash tool's PATH.)
#
# ------------------------------------------------------------------------------
# WHY THIS EXISTS
#
# Every entry point needs exactly one thing before it can do anything: the
# instance's venv interpreter. Until now each of them got there by shelling out
# to `/usr/bin/env python3` and importing `instance_config`, which imports yaml.
#
# That worked on ONE mac, for a reason nobody chose: the launchd plists run
# `zsh -l`, a login dotfile prepends a Homebrew python, and that python happened
# to have PyYAML installed globally. Under the PATH the plists actually stamp
# (`/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:$HOME/.local/bin`) the
# import fails; on a mac without Command Line Tools `/usr/bin/python3` is a stub
# that refuses to run at all. Either way the entry point dies before reading a
# single field — and `supervisor.sh`'s backlog probe, whose stderr is swallowed
# by `2>/dev/null` and whose empty reading RESETS the deaf-poller counter, dies
# without saying anything.
#
# So: read one line with sed, re-enter through the interpreter it names, and let
# every subsequent config read happen under a python the manifest guarantees.
# Rejected alternatives (KTD4): vendoring a YAML parser, requiring a second
# bootstrap venv, pinning Homebrew's python.
#
# WHY NOT FIVE COPIES OF THIS. The expansion below has to agree with
# `instance_config._expand()` exactly, in all five entry points, forever — and
# supervisor.sh's own comment already states the rule this follows: "a second
# parser is a second set of defaults to drift out of sync with". One copy is
# testable for that agreement; five copies are testable only for the one you
# remember to change.

# estate_resolve_python <instance.yaml>
#
# Sets the global VENV_PY. Returns 2 (with a message on stderr) when the config
# names no interpreter, or names one that cannot be executed.
estate_resolve_python() {
  local cfg="$1" raw p

  # ONE value, ONE line, read with sed — deliberately not a YAML parse.
  #
  # `^python:` anchors at column 0, which is the whole of "top-level key" in
  # YAML: a nested `python:` is indented and a commented one starts with `#`,
  # so neither can match. The substitutions that follow reproduce the small
  # part of YAML scalar semantics a path can use — an inline comment must be
  # preceded by whitespace (so `/a#b` stays a path), surrounding quotes are
  # stripped, trailing blanks go. Cross-checked against PyYAML on each of those
  # shapes; see tests/test_bootstrap.py.
  #
  # THE ONE CONSTRAINT THIS PLACES ON A CONFIG: `python:` must be written on a
  # single line. YAML lets a plain scalar continue on the next, more-indented
  # line, and PyYAML's emitter FOLDS one there by default for anything past 80
  # columns — so a config generated with `yaml.safe_dump(...)` and a long venv
  # path can legally arrive folded. Anything that writes a config must emit it
  # unfolded (`width=` on the dump, or a template). A folded value truncates at
  # the fold, which is not executable, so it fails with the message below rather
  # than quietly resolving to something else.
  raw=$(sed -n '/^python:/{
      s/^python://
      s/[[:space:]][[:space:]]*#.*$//
      s/^[[:space:]]*//
      s/[[:space:]]*$//
      s/^"\(.*\)"$/\1/
      s/^'\''\(.*\)'\''$/\1/
      p
      q
  }' "$cfg" 2>/dev/null)

  if [[ -z "$raw" ]]; then
    print -ru2 -- "instance config $cfg has no usable top-level 'python:' field"
    print -ru2 -- "  Every entry point re-enters through that interpreter, so there is"
    print -ru2 -- "  nothing to fall back to. Set it to the absolute path of this"
    print -ru2 -- "  instance's venv python3."
    return 2
  fi

  # Reapply `~` and `$VAR` expansion so this lands on the same absolute path
  # `instance_config._expand()` does — expanduser, then expandvars, then
  # abspath. The shipped template writes home-relative paths, and a quoted `~`
  # is not expanded by zsh at all when used as a command, so an unexpanded
  # scalar is an unresolvable literal rather than a loud error.
  #
  # `:a` and NOT `:A`: abspath normalises `..` lexically and leaves symlinks
  # alone, which is what `_expand()` does and what a venv shim needs to stay a
  # venv shim. `:A` resolves the symlink to the SYSTEM python — the U14 outage.
  #
  # Two documented divergences from expandvars, both harmless because both end
  # at the same "python not executable" message: an undefined `$VAR` expands to
  # empty here and stays literal in python, and `~user` (as opposed to `~/`) is
  # not supported.
  p="$raw"
  case "$p" in
    '~')   p="$HOME" ;;
    '~/'*) p="$HOME/${p#\~/}" ;;
  esac
  p=$(print -r -- "${(e)p}" 2>/dev/null) || p="$raw"
  VENV_PY="${p:a}"

  # `-f` as well as `-x`: a directory is executable, and a value that expanded
  # to nothing would otherwise resolve to the cwd and pass.
  if [[ ! -f "$VENV_PY" || ! -x "$VENV_PY" ]]; then
    print -ru2 -- "python not executable: $VENV_PY (from 'python:' in $cfg)"
    return 2
  fi
  return 0
}
