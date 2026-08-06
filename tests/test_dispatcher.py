"""U4/KTD5: `bin/` holds one namespaced command; the stack lives in `libexec/`.

WHY THIS FILE EXISTS. Claude Code adds an enabled plugin's `bin/` to the Bash
tool's PATH for every session — verified live on the machine this was written
on. While this plugin sat with twelve executables in `bin/`, enabling it put
`send.py`, `guard.py`, `parity.sh` and `supervisor.sh` into the shell of anyone
who installed it. None of those names is ours to take.

So the split is a property to hold, not a one-time move, and it decomposes into
three assertions that can each regress independently:

  1. `bin/` stays a directory with one file in it. Adding a second executable
     there is a one-line change that no other test would notice.
  2. The dispatcher is transparent. It exists only to route, so an argument it
     mangles or an exit code it swallows is a pure loss. The exit codes matter
     past tidiness: `run-job.sh` carries the guard's idempotency contract
     (0 enqueued · 3 marker · 4 lease · 5 too early) and a collapsed 3 is a
     second morning brief sent to a user who already had one.
  3. The rendered plists point into `libexec/`. launchd holds an absolute path
     copied at install time; if the installer's derivation still said `bin/`,
     every supervisor and every scheduled job would fail to exec, and a
     scheduled job that cannot exec fails silently.

HOW THE DISPATCHER IS EXERCISED. It resolves `libexec/` relative to itself
(a seam U7 replaces with the runtime stamp), so a scratch tree with the real
dispatcher in `bin/` and a recording stub in `libexec/` tests the real
resolution path without running an installer or spending a turn.
"""
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import instance_config

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
BIN = PLUGIN_ROOT / "bin"
LIBEXEC = PLUGIN_ROOT / "libexec"
DISPATCHER = BIN / "telegram-agent-estate"


def run(argv, **kw):
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                          timeout=120, **kw)


def combined(res):
    return res.stdout + res.stderr


# --- 1. `bin/` is one file ----------------------------------------------------

def test_bin_holds_exactly_one_command_and_it_is_executable():
    """The R6 assertion, stated the way PATH sees it.

    Dot-prefixed entries are excluded from the name check because they are
    Finder and tooling litter that git already ignores — but they are still
    checked for the executable bit, since a hidden executable is every bit as
    resolvable as a visible one to anyone who types its name.
    """
    entries = sorted(BIN.iterdir())
    visible = [p for p in entries if not p.name.startswith(".")]
    assert [p.name for p in visible] == ["telegram-agent-estate"], (
        "bin/ is on every installing user's PATH; it may hold the dispatcher "
        f"and nothing else. Found: {[p.name for p in visible]}")
    assert DISPATCHER.is_file()
    assert os.access(DISPATCHER, os.X_OK), "the one command in bin/ is not executable"
    for p in entries:
        if p != DISPATCHER:
            assert not os.access(p, os.X_OK), f"{p.name} is executable inside bin/"


def test_no_implementation_file_was_left_behind_in_bin():
    """The move is only worth anything if it was complete.

    A forwarding shim in `bin/` was explicitly allowed as a temporary bridge
    while the live plists were re-rendered; this asserts none survived, because
    one that ships defeats the entire unit.
    """
    stragglers = [p.name for p in BIN.iterdir()
                  if p.suffix in (".py", ".sh", ".zsh") or p.is_dir()]
    assert stragglers == [], f"implementation left in bin/: {stragglers}"


# --- 2. the dispatcher is transparent ----------------------------------------

def scratch_tree(tmp_path, targets):
    """A plugin-shaped directory: the real dispatcher, stub targets beside it.

    `targets` maps a libexec filename to the exit code its stub should return.
    Each stub writes its argv, one argument per line, to `<tmp>/argv-<name>`.

    `estate-runtime.zsh` is copied in because the dispatcher sources it to
    decide WHICH libexec to run (U7/KTD1) and refuses to run without it — a
    tree missing it is an incomplete plugin, not a plugin that falls back. The
    scratch tree is not cache-resident and no stamp is in play, so the rule
    resolves to this tree, which is what these tests are about.
    """
    bin_dir, libexec = tmp_path / "bin", tmp_path / "libexec"
    bin_dir.mkdir()
    libexec.mkdir()
    resolver = libexec / "estate-runtime.zsh"
    resolver.write_text((LIBEXEC / "estate-runtime.zsh").read_text(encoding="utf-8"),
                        encoding="utf-8")
    dest = bin_dir / DISPATCHER.name
    dest.write_text(DISPATCHER.read_text(encoding="utf-8"), encoding="utf-8")
    dest.chmod(0o755)
    for target, rc in targets.items():
        stub = libexec / target
        stub.write_text(
            "#!/bin/zsh\n"
            f'printf "%s\\n" "$@" > {tmp_path}/argv-{target}\n'
            f"exit {rc}\n", encoding="utf-8")
        stub.chmod(0o755)
    return dest


def test_dispatcher_forwards_arguments_verbatim(tmp_path):
    """Including the ones a careless `$*` would eat: spaces, flags, empties."""
    disp = scratch_tree(tmp_path, {"install-instance.sh": 0})
    args = ["/a path/with spaces.yaml", "--dry-run", "", "--no-load"]
    res = run([disp, "install", *args])
    assert res.returncode == 0, combined(res)
    seen = (tmp_path / "argv-install-instance.sh").read_text(
        encoding="utf-8").split("\n")[:-1]
    assert seen == args


@pytest.mark.parametrize("rc", [0, 1, 2, 3, 4, 5, 42])
def test_dispatcher_forwards_exit_codes_unchanged(tmp_path, rc):
    """3/4/5 are the guard's idempotency contract and are load-bearing.

    A scheduled job's plist reads them to tell "already sent today" from "a send
    is in flight" from "too early"; collapsing any of them to a generic failure
    turns duplicate suppression into a duplicate send.
    """
    disp = scratch_tree(tmp_path, {"parity.sh": rc})
    assert run([disp, "parity", "cfg.yaml"]).returncode == rc


def test_dispatcher_reports_a_missing_libexec_target(tmp_path):
    """The failure mode U7's stamp resolution will make common: a `bin/` whose
    sibling `libexec/` is not the one it expected. It must say so, not exec into
    nothing and leave zsh's 127 to be interpreted."""
    disp = scratch_tree(tmp_path, {})
    res = run([disp, "install", "cfg.yaml"])
    assert res.returncode == 2
    assert "libexec/install-instance.sh" in combined(res)


# --- 3. the dispatcher's own error surface -----------------------------------

def test_dispatcher_without_a_subcommand_fails_with_usage():
    res = run([DISPATCHER])
    assert res.returncode != 0
    assert "usage: telegram-agent-estate" in combined(res)


def test_dispatcher_help_exits_zero_with_usage():
    for flag in ("-h", "--help", "help"):
        res = run([DISPATCHER, flag])
        assert res.returncode == 0, combined(res)
        assert "usage: telegram-agent-estate" in res.stdout


def test_dispatcher_rejects_an_unknown_subcommand():
    res = run([DISPATCHER, "supervisor.sh"])
    assert res.returncode != 0
    out = combined(res)
    assert "unknown subcommand" in out
    assert "usage: telegram-agent-estate" in out


def test_every_declared_subcommand_routes_somewhere(tmp_path):
    """KTD5 fixed the surface at five names; U8 landed the last of them.

    `uninstall` was the one that said "not available yet" from U4 until now, so
    what this asserts is the completion: every name the usage text prints has an
    implementation behind it, and none of them answers with a placeholder.
    """
    # Scraped from the printed usage, not from the source: what a user types is
    # what they were shown, and a name that only exists in a shell array is not
    # a name anyone can find.
    helptext = run([DISPATCHER, "--help"]).stdout
    names = set(re.findall(r"^  ([a-z-]+) ", helptext, re.M))
    assert names == {"provision", "upgrade", "install", "parity", "uninstall"}, names

    targets = {"install-instance.sh": 0, "parity.sh": 0, "provision-runtime.sh": 0,
               "uninstall-instance.sh": 0}
    disp = scratch_tree(tmp_path, targets)
    for cmd in sorted(names):
        res = run([disp, cmd, "cfg.yaml"])
        out = combined(res)
        assert "not available yet" not in out, f"{cmd} is still a placeholder"
        assert "unknown subcommand" not in out, f"{cmd} is printed but not routed"


def test_the_pending_mechanism_survives_its_last_member():
    """Declared-but-pending is a different answer from unknown: one says "wait",
    the other says "you typed it wrong", and getting the second for a name the
    README lists sends a user hunting for a typo that is not there.

    The `PENDING` map is empty now that `uninstall` has landed, so nothing
    exercises it end-to-end — which is exactly when a cleanup deletes it and the
    next declared-early verb silently reads as a typo. Asserting the mechanism
    is still wired keeps the shape available for that verb.
    """
    body = DISPATCHER.read_text(encoding="utf-8")
    assert "typeset -A PENDING=" in body
    assert "not available yet" in body


def test_uninstall_routes_to_its_libexec_implementation(tmp_path):
    """The verb that turns a live instance off. Routing it to the wrong place —
    or nowhere — is how a user ends up unloading launchd jobs by hand."""
    disp = scratch_tree(tmp_path, {})
    res = run([disp, "uninstall", "cfg.yaml"])
    assert res.returncode == 2
    assert "libexec/uninstall-instance.sh" in combined(res)


# --- 4. what launchd is handed -----------------------------------------------

def build_instance(root, name, chat):
    """A complete, self-contained instance rooted at `root`."""
    root = Path(root)
    (root / "repo" / "prompts").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "gw").mkdir(parents=True, exist_ok=True)
    (root / "repo" / "prompts" / "job.txt").write_text("do it\n", encoding="utf-8")
    (root / "env").write_text(f"TELEGRAM_BOT_TOKEN=fake-{name}\n", encoding="utf-8")
    os.chmod(root / "env", 0o600)
    data = {
        "name": name, "label": f"{name.title()} Bot",
        "workdir": str(root / "repo"), "python": sys.executable,
        "telegram": {"owner_chat_id": chat, "token_file": str(root / "env")},
        "runtime": {"state_dir": str(root / "gw"), "log_dir": str(root / "logs")},
        "schedules": [{"job": "morning-digest", "hour": 9, "minute": 0,
                       "prompt_file": "prompts/job.txt"}],
    }
    p = root / f"{name}.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return instance_config.load(p)


@pytest.fixture
def rendered(tmp_path):
    """Install one instance into a scratch LaunchAgents dir and hand back the
    plists it wrote.

    `--no-load` rather than `--dry-run`: dry-run deliberately writes nothing (it
    prints `would write <dest>`), so it cannot be inspected for the path it
    would have stamped. `--no-load` renders the real files, lints them the way
    the installer does, and stops before `launchctl` — which is the strongest
    check available without touching this machine's loaded jobs.
    """
    inst = build_instance(tmp_path / "inst", "scratchbot", 10_000_000_002)
    agents = tmp_path / "agents"
    agents.mkdir()
    res = run([LIBEXEC / "install-instance.sh", inst.source, "--no-load"],
              env={**os.environ, "ESTATE_LAUNCH_AGENTS": str(agents)})
    assert res.returncode == 0, combined(res)
    plists = sorted(agents.glob("*.plist"))
    assert len(plists) == 2, [p.name for p in plists]
    return plists


def test_rendered_plists_exec_out_of_libexec(rendered):
    """launchd holds the absolute path copied at install time, so the
    installer's `${0:A:h}` derivation is what decides whether a supervisor and
    every scheduled job can exec at all."""
    for p in rendered:
        with open(p, "rb") as fh:
            args = plistlib.load(fh)["ProgramArguments"]
        cmd = args[-1]
        assert cmd.startswith(f"{LIBEXEC}/"), cmd
        assert f"{PLUGIN_ROOT}/bin/" not in cmd, cmd
    scripts = {plistlib.loads(p.read_bytes())["ProgramArguments"][-1].split()[0]
               for p in rendered}
    assert scripts == {f"{LIBEXEC}/supervisor.sh", f"{LIBEXEC}/run-job.sh"}


def test_rendered_plists_are_valid(rendered):
    """plutil is what the installer itself gates on; assert it independently so
    a template edit that breaks the XML cannot pass by way of a skipped lint."""
    for p in rendered:
        lint = run(["plutil", "-lint", p])
        assert lint.returncode == 0, combined(lint)


def test_dry_run_writes_no_plist_but_names_the_destination(tmp_path):
    """The other half of the `--no-load` choice above, stated so it stays true:
    an instance that looks installed but never fires is the failure this
    project keeps re-learning, and `--dry-run` must not be able to produce it.
    """
    inst = build_instance(tmp_path / "inst", "drybot", 10_000_000_003)
    agents = tmp_path / "agents"
    agents.mkdir()
    res = run([LIBEXEC / "install-instance.sh", inst.source, "--dry-run"],
              env={**os.environ, "ESTATE_LAUNCH_AGENTS": str(agents)})
    assert res.returncode == 0, combined(res)
    assert list(agents.glob("*.plist")) == []
    assert "would write" in res.stdout
