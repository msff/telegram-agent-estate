"""Interpreter bootstrap — every shell entry point must find its own python.

WHY THIS FILE EXISTS. The five shell entry points each began by shelling out to
`/usr/bin/env python3` to parse the instance YAML. That works on THIS mac and
nowhere else: the launchd plists run `zsh -l`, a login dotfile prepends a
Homebrew python, and that python happens to have PyYAML installed globally.
Under the PATH the plists actually stamp — and under a clean macOS install,
where `/usr/bin/python3` is a Command Line Tools stub — `import yaml` fails and
every entry point dies before it has read a single field.

The failure is not symmetrical across the six call sites, and the asymmetry is
the reason these tests are integration tests rather than a unit test of the
extraction:

  - `supervisor.sh`'s config load dies loudly (a `set -u` unset-parameter abort).
  - `supervisor.sh`'s BACKLOG PROBE dies silently. Its stderr is swallowed by
    `2>/dev/null`, an empty reading is indistinguishable from "backlog is zero",
    and an empty reading RESETS the deaf-poller counter. A broken probe does not
    report; it disables the watchdog and keeps saying everything is fine.

So the tests drive the real scripts under a sanitized PATH and read what they
produce, rather than asserting on the shape of the code.

HOW THE SCRIPTS ARE MADE SAFE TO RUN. Each test config points `python:` at a
STUB interpreter — a tiny shell script that logs its own `$0` and argv and then
either execs the real interpreter (for `-` and `-c`, the two forms that are the
bootstrap and the backlog probe) or exits 0 (for everything else, i.e. every
form that would start a poller, a drain, a `claude` turn, or pytest). The stub's
logged `$0` is also the assertion for the expansion tests: it is literally the
path the entry point resolved, so it can be compared against what
`instance_config._expand()` produces for the same YAML scalar.
"""
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

import instance_config

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
LIBEXEC = PLUGIN_ROOT / "libexec"
REQUIREMENTS = PLUGIN_ROOT / "requirements.txt"

# The five shell entry points, and an argv that makes each one reach — and then
# stop just after — its config load. `--dry-run` and the stub interpreter are
# what keep these from installing launchd jobs or spending a claude turn.
ENTRY_POINTS = {
    "supervisor.sh": lambda cfg: [cfg],
    "housekeeping.sh": lambda cfg: [cfg],
    "install-instance.sh": lambda cfg: [cfg, "--dry-run"],
    "run-job.sh": lambda cfg: [cfg, "morning"],
    "parity.sh": lambda cfg: [cfg],
}

# The plan's literal gate: no Homebrew, no login dotfile, nothing but the two
# directories a bare macOS ships. `real_sanitized_path` additionally shadows
# `python3` with a stub that fails the way a Command Line Tools stub fails,
# which is the state of a mac nobody has run `xcode-select --install` on.
BARE_PATH = "/usr/bin:/bin"


# --- harness -----------------------------------------------------------------

def _write_exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def make_stub_python(path: Path) -> Path:
    """A stand-in for an instance's venv interpreter.

    Logs `$0` (the resolved interpreter path — the thing under test) plus argv,
    then execs the REAL interpreter only for the two argv shapes that are part
    of the bootstrap contract:

        `-`   the heredoc config read every entry point performs
        `-c`  supervisor.sh's getWebhookInfo backlog probe

    Everything else — `-m pytest`, `turn_runner.py rotate`, `poller.py`,
    `send.py` — is logged and exits 0, so a test never starts a daemon, spends a
    turn, or sends a Telegram message.
    """
    return _write_exec(path, f"""#!/bin/sh
{{ printf '%s' "$0"; for a in "$@"; do printf '\\t%s' "$a"; done; printf '\\n'; }} \\
  >> "$ESTATE_STUB_LOG"
case "${{1:-}}" in
  -|-c) exec '{sys.executable}' "$@" ;;
esac
exit 0
""")


def make_decoy_bin(path: Path) -> Path:
    """A PATH entry whose `python3` fails the way a clean mac's does.

    `/usr/bin/python3` on a machine without Command Line Tools is a stub that
    refuses to run. Shadowing `python3` this way turns "the ambient interpreter
    lacks PyYAML" into "there is no ambient interpreter at all", which is the
    harsher and more honest version of the same gate.
    """
    path.mkdir(parents=True, exist_ok=True)
    _write_exec(path / "python3", """#!/bin/sh
echo "xcrun: error: invalid active developer path (test decoy)" >&2
exit 127
""")
    return path


class Instance:
    """One throwaway instance: config, workdir, stub interpreter, HOME, env."""

    def __init__(self, root: Path, *, python_value=None, name="alpha", extra=None):
        self.root = root
        self.home = root / "home"
        self.workdir = root / "work"
        self.logdir = root / "logs"
        self.statedir = root / "state"
        self.agents = root / "LaunchAgents"
        self.stub_log = root / "stub.log"
        self.decoy = make_decoy_bin(root / "decoybin")
        for d in (self.home, self.workdir, self.logdir, self.statedir, self.agents):
            d.mkdir(parents=True, exist_ok=True)

        # The stub lives under HOME so a config can name it as `~/…` or `$HOME/…`
        # and the resulting path is still real. The directory name carries a
        # space on purpose — a value that survives one shell hop but not two is
        # the classic way this kind of extraction breaks.
        self.stub = make_stub_python(self.home / "venv dir" / "bin" / "python3")

        (self.workdir / "prompts").mkdir(parents=True, exist_ok=True)
        (self.workdir / "prompts" / "morning.md").write_text("say hi\n", encoding="utf-8")
        # Pre-stamp housekeeping so supervisor.sh's daily window cannot fire
        # mid-test and turn a probe into a rotation.
        (self.workdir / "state").mkdir(parents=True, exist_ok=True)
        (self.workdir / "state" / "last-housekeeping-day").write_text(
            time.strftime("%Y-%m-%d"), encoding="utf-8")

        self.token_file = root / "token.env"
        self.token_file.write_text("TELEGRAM_BOT_TOKEN=111:test\n", encoding="utf-8")
        self.token_file.chmod(0o600)

        self.python_value = (python_value if python_value is not None
                             else str(self.stub))
        data = {
            "name": name,
            "label": "Alpha Bot",
            "workdir": str(self.workdir),
            "python": self.python_value,
            "telegram": {"owner_chat_id": 111, "token_file": str(self.token_file)},
            "runtime": {"state_dir": str(self.statedir), "log_dir": str(self.logdir)},
            "housekeeping": {"hour": 3},
            "schedules": [{"job": "morning", "hour": 9, "minute": 0,
                           "prompt_file": "prompts/morning.md"}],
        }
        if extra:
            data.update(extra)
        self.config = root / "instance.yaml"
        # `width` is not cosmetic. PyYAML's emitter line-FOLDS a long plain
        # scalar at a space, so a dumped config can legally carry `python:` on
        # two lines — which no single-line extraction can read. Real configs are
        # hand-written or rendered from templates/instance.yaml and never fold;
        # this keeps the harness honest about that, and
        # `test_a_folded_python_scalar_fails_loudly` pins what happens when
        # something does emit one.
        self.config.write_text(
            yaml.safe_dump(data, sort_keys=False, width=10 ** 6), encoding="utf-8")

    def rewrite_python_scalar(self, raw_line):
        """Replace the emitted `python:` line with a hand-written one.

        Quoting, inline comments and `~` are things a hand-edited config has and
        a dumped one never does, and they are exactly what the extraction has to
        get right.
        """
        text = self.config.read_text(encoding="utf-8")
        text = re.sub(r"^python:.*$", raw_line, text, count=1, flags=re.M)
        self.config.write_text(text, encoding="utf-8")

    def env(self, *, path=None, extra=None):
        env = {
            "PATH": path or f"{self.decoy}:{BARE_PATH}",
            "HOME": str(self.home),
            "ESTATE_STUB_LOG": str(self.stub_log),
            "ESTATE_LAUNCH_AGENTS": str(self.agents),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            "LANG": "en_US.UTF-8",
        }
        if extra:
            env.update(extra)
        return env

    def stub_calls(self):
        if not self.stub_log.exists():
            return []
        return [line.split("\t")
                for line in self.stub_log.read_text(encoding="utf-8").splitlines()
                if line]

    def expected_python(self):
        """What `instance_config._expand()` makes of this config's scalar.

        Loaded with HOME pointed at the instance's own home, because that is the
        environment the entry point resolved the same scalar under.
        """
        prev = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        try:
            return str(instance_config.load(str(self.config)).python)
        finally:
            if prev is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = prev


def run_entry(inst, script, *, path=None, args=None, env_extra=None, timeout=60):
    """Run one entry point to completion. `supervisor.sh` gets special handling."""
    argv = [str(LIBEXEC / script)] + (args if args is not None
                                  else ENTRY_POINTS[script](str(inst.config)))
    env = inst.env(path=path, extra=env_extra)
    if script == "supervisor.sh":
        return _run_supervisor(inst, argv, env, timeout=timeout)
    return subprocess.run(argv, cwd=str(inst.workdir), env=env,
                          capture_output=True, text=True, timeout=timeout)


def _run_supervisor(inst, argv, env, *, timeout=30, until=None):
    """Start the supervisor, wait for evidence, then stop it.

    The supervisor is an infinite loop by design, so "did it load its config" is
    read out of its daemon.log rather than out of an exit status.

    The default wait is for the log line AND both of tick 1's background spawns
    (`ensure_poller`, `ensure_drain`) to have reached the stub log. They are
    `nohup`ed, so without that a run can be stopped between the two and produce
    output that differs from an identical run purely by timing.
    """
    daemon_log = inst.logdir / "daemon.log"
    # Output goes to FILES, and the whole thing runs in its own session.
    # `sleep 30` at the bottom of the supervisor loop is a child that inherits
    # whatever stdout it was given and is not signalled when its parent is: with
    # pipes, reading them back blocks for the rest of that sleep. The session
    # also gives the nohup'ed poller/drain spawns something to be cleaned up by.
    out_path = inst.root / "supervisor.out"
    err_path = inst.root / "supervisor.err"
    with open(out_path, "w") as out_f, open(err_path, "w") as err_f:
        proc = subprocess.Popen(argv, cwd=str(inst.workdir), env=env,
                                stdout=out_f, stderr=err_f, text=True,
                                start_new_session=True)

    def _read(p):
        return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""

    def ready():
        if until is not None:
            return until in _read(daemon_log)
        spawns = _read(inst.stub_log)
        return ("gateway supervisor up" in _read(daemon_log)
                and "poller.py" in spawns and "turn_runner.py" in spawns)

    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if ready():
                break
            time.sleep(0.2)
    finally:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
    return subprocess.CompletedProcess(
        argv, proc.returncode, _read(out_path) + _read(daemon_log), _read(err_path))


def combined(res):
    return (res.stdout or "") + (res.stderr or "")


def evidence(inst, res):
    """Everything the run left behind: output plus the stub interpreter's log.

    `run-job.sh` prints nothing on the happy path — it hands off to
    `turn_runner.py` and exits with its status — so its only evidence of having
    loaded a config is the argv it built. Reading both keeps one assertion
    honest across all five entry points.
    """
    log = (inst.stub_log.read_text(encoding="utf-8", errors="replace")
           if inst.stub_log.exists() else "")
    return combined(res) + log


# --- the gate: every entry point works with nothing but /usr/bin and /bin -----

@pytest.mark.parametrize("script", sorted(ENTRY_POINTS))
def test_entry_point_loads_its_config_under_a_sanitized_path(tmp_path, script):
    """No Homebrew, no login dotfile, and `python3` on PATH refuses to run.

    This is the whole of R3 in one assertion: the entry point must resolve its
    interpreter from the config it was handed, not from the environment it
    happened to be started in.
    """
    inst = Instance(tmp_path)
    res = run_entry(inst, script)
    out = evidence(inst, res)
    assert "ModuleNotFoundError" not in out, out
    assert "Traceback" not in out, out
    assert "invalid active developer path" not in out, out
    # Having a config is what makes the instance name reachable at all — it is
    # printed, logged, or carried into the argv the entry point builds.
    assert "alpha" in out, out


@pytest.mark.parametrize("script", sorted(ENTRY_POINTS))
def test_sanitized_path_reports_the_same_values_as_a_full_path(tmp_path, script):
    """Same config, two PATHs, same output.

    A script that silently degraded under the sanitized PATH — loading defaults
    instead of the config, say — would pass the assertion above and fail here.
    """
    bare = Instance(tmp_path / "bare")
    full = Instance(tmp_path / "full")
    res_bare = run_entry(bare, script)
    res_full = run_entry(full, script, path=os.environ.get("PATH", BARE_PATH))

    def normalise(res, inst):
        text = evidence(inst, res)
        text = text.replace(str(inst.root), "<ROOT>")
        # Timestamps: supervisor and housekeeping stamp every log line.
        text = re.sub(r"\[[A-Z][a-z]{2} [A-Z][a-z]{2} .*?\]", "[TS]", text)
        text = re.sub(r"\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\]", "[TS]", text)
        return text

    assert normalise(res_bare, bare) == normalise(res_full, full)


def test_bare_usr_bin_path_is_enough(tmp_path):
    """The plan's literal PATH, with no decoy shadowing `python3`.

    Kept separate from the parametrized gate because it proves a different
    thing: even where an ambient `python3` DOES exist, nothing may depend on
    what is installed into it.
    """
    inst = Instance(tmp_path)
    res = run_entry(inst, "housekeeping.sh", path=BARE_PATH)
    assert res.returncode == 0, combined(res)
    assert "housekeeping done (alpha)" in combined(res)


# --- the silent one: supervisor.sh's backlog probe ---------------------------

def test_backlog_probe_returns_a_number_under_a_sanitized_path(tmp_path):
    """An empty probe reading resets the deaf-poller counter.

    That is why this test exists separately from the config-load gate: a probe
    that returns "" does not fail, it just quietly turns the deaf-poller
    watchdog off while every other health signal keeps reading green. The probe
    is exercised end to end — fake `curl`, real process for `pgrep` to find —
    and the assertion is the number landing in the daemon log.
    """
    inst = Instance(tmp_path)
    _write_exec(inst.decoy / "curl", """#!/bin/sh
echo '{"ok":true,"result":{"pending_update_count":7}}'
""")
    # A process the supervisor's poller probe will accept: argv matches
    # `poller.py.*--instance=alpha` and `comm` is a python.
    fake_poller = tmp_path / "poller.py"
    fake_poller.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
    poller = subprocess.Popen([sys.executable, str(fake_poller), "--instance=alpha"])
    try:
        res = _run_supervisor(
            inst, [str(LIBEXEC / "supervisor.sh"), str(inst.config)], inst.env(),
            timeout=45, until="backlog=")
    finally:
        poller.terminate()
        poller.wait(timeout=10)
    out = combined(res)
    assert "backlog=7" in out, out


# --- how the `python:` scalar is written -------------------------------------

@pytest.mark.parametrize("scalar,label", [
    ("python: ~/venv dir/bin/python3", "tilde"),
    ("python: $HOME/venv dir/bin/python3", "dollar-home"),
    ("python: ${HOME}/venv dir/bin/python3", "braced-home"),
    ('python: "~/venv dir/bin/python3"', "quoted-tilde"),
    ("python: '~/venv dir/bin/python3'", "single-quoted-tilde"),
    ("python: ~/venv dir/bin/python3   # the instance venv", "trailing-comment"),
])
def test_scalar_resolves_exactly_as_expand_does(tmp_path, scalar, label):
    """`~`, `$VAR` and quotes must land on the same absolute path python does.

    The shipped template writes home-relative paths, so a raw YAML scalar handed
    to the shell unexpanded is an unresolvable literal — and a quoted `~` is not
    expanded by zsh at all when used as a command.
    """
    inst = Instance(tmp_path)
    inst.rewrite_python_scalar(scalar)
    res = run_entry(inst, "housekeeping.sh")
    assert res.returncode == 0, combined(res)
    calls = inst.stub_calls()
    assert calls, f"stub interpreter was never invoked ({label})"
    assert calls[0][0] == inst.expected_python()


def test_path_with_spaces_survives(tmp_path):
    """An absolute path containing a space, written plainly."""
    inst = Instance(tmp_path)
    inst.rewrite_python_scalar(f"python: {inst.stub}")
    assert " " in str(inst.stub)
    res = run_entry(inst, "housekeeping.sh")
    assert res.returncode == 0, combined(res)
    assert inst.stub_calls()[0][0] == inst.expected_python()


def test_only_the_top_level_key_is_read(tmp_path):
    """A `python:` in a comment or nested under another key is not the key.

    Both decoys are written ABOVE the real one, so an extraction that takes the
    first match anywhere in the file picks a decoy and the run fails.
    """
    inst = Instance(tmp_path)
    text = inst.config.read_text(encoding="utf-8")
    text = ("# python: /decoy/from/a/comment\n"
            "nested:\n"
            "  python: /decoy/from/a/block\n" + text)
    inst.config.write_text(text, encoding="utf-8")
    res = run_entry(inst, "housekeeping.sh")
    assert res.returncode == 0, combined(res)
    assert "/decoy/" not in combined(res)
    assert inst.stub_calls()[0][0] == inst.expected_python()


# --- the failure messages ----------------------------------------------------

@pytest.mark.parametrize("script", sorted(ENTRY_POINTS))
def test_missing_python_binary_says_so_instead_of_tracebacking(tmp_path, script):
    """The existing preflight wording, kept: `python not executable: <path>`."""
    inst = Instance(tmp_path, python_value="/nonexistent/venv/bin/python3")
    res = run_entry(inst, script)
    out = combined(res)
    assert res.returncode != 0, out
    assert "python not executable" in out, out
    assert "/nonexistent/venv/bin/python3" in out, out
    assert "Traceback" not in out, out


@pytest.mark.parametrize("script", sorted(ENTRY_POINTS))
@pytest.mark.parametrize("scalar", ["", "python:", "python: ''", "# python: /x/y"])
def test_absent_or_empty_python_names_the_field_and_the_file(tmp_path, script, scalar):
    """Naming both matters: the operator has several configs and one is wrong."""
    inst = Instance(tmp_path)
    inst.rewrite_python_scalar(scalar)
    res = run_entry(inst, script)
    out = combined(res)
    assert res.returncode != 0, out
    assert "python" in out, out
    cfg = str(inst.config)
    assert cfg in out or os.path.realpath(cfg) in out, out
    assert "Traceback" not in out, out


def test_a_folded_python_scalar_fails_loudly(tmp_path):
    """`python:` must be one line — and a config that folds it must SAY so.

    YAML permits a plain scalar to continue on the next, more-indented line, and
    PyYAML's emitter produces exactly that for a path longer than its default
    80-column width. Reading one line with sed (KTD4) cannot follow the fold, so
    the contract is that the field is written unfolded — anything generating a
    config has to emit it that way.

    What is pinned here is the failure MODE, not the limitation: the truncated
    path is not executable, so the run stops with a message quoting it. Silent
    truncation into some other interpreter is the outcome this excludes.
    """
    inst = Instance(tmp_path)
    head, tail = str(inst.stub).split("venv dir", 1)
    inst.rewrite_python_scalar(f"python: {head}venv\n  dir{tail}")
    res = run_entry(inst, "housekeeping.sh")
    out = combined(res)
    assert res.returncode != 0, out
    assert "python not executable" in out, out
    assert "Traceback" not in out, out


@pytest.mark.parametrize("script", sorted(ENTRY_POINTS))
def test_unreadable_config_keeps_its_existing_message(tmp_path, script):
    """This wording is how a misconfigured instance announces itself."""
    inst = Instance(tmp_path)
    missing = tmp_path / "not-there.yaml"
    args = ENTRY_POINTS[script](str(missing))
    res = run_entry(inst, script, args=args)
    out = combined(res)
    assert res.returncode != 0, out
    assert "unreadable instance config" in out, out


# --- no entry point may reach for the ambient interpreter again ---------------

@pytest.mark.parametrize("script", sorted(ENTRY_POINTS))
def test_no_shell_entry_point_shells_out_to_env_python3(script):
    """A regression guard for the six call sites, two of which are easy to miss.

    `supervisor.sh`'s backlog probe and `install-instance.sh`'s second heredoc
    are not the config bootstrap and were both live `/usr/bin/env python3` calls.
    """
    text = (LIBEXEC / script).read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "/usr/bin/env python3" not in code, script
    assert not re.search(r"(?<![-\w/])python3\s", code), script


# --- requirements.txt --------------------------------------------------------

def _requirement_lines():
    return [l.strip() for l in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")]


def test_requirements_declares_the_audited_set_with_floors_and_ceilings():
    """Every third-party import in libexec/ and tests/, pinned at both ends.

    An open ceiling is not a small risk here: `python-telegram-bot` rewrites its
    API across majors and `poller.py` imports narrow surfaces from it, so the
    version a stranger resolves six months from now is not the version this was
    tested against.
    """
    lines = _requirement_lines()
    named = {re.split(r"[<>=!\[]", l, maxsplit=1)[0].strip().lower(): l
             for l in lines}
    for pkg in ("pyyaml", "requests", "python-telegram-bot", "pytest"):
        assert pkg in named, f"{pkg} is imported but not declared"
        assert ">=" in named[pkg], f"{pkg} has no floor"
        assert "<" in named[pkg], f"{pkg} has no ceiling"


def test_requirements_records_the_python_floor_and_why_pytest_is_runtime():
    """Both facts have to survive in the file, not only in a commit message.

    The floor decides whether Homebrew is a prerequisite. And `pytest` is a
    RUNTIME dependency: supervisor.sh's CLI-version tripwire shells out to it,
    so a venv built for "just run the bot" holds every turn forever after the
    first `claude` self-update.
    """
    text = REQUIREMENTS.read_text(encoding="utf-8")
    assert re.search(r"3\.11", text), "the tested Python floor is not recorded"
    assert "runtime" in text.lower() and "pytest" in text
    assert "tripwire" in text.lower() or "cli-version" in text.lower()


@pytest.mark.skipif(os.environ.get("ESTATE_NETWORK_TESTS") != "1",
                    reason="builds a venv from PyPI: ESTATE_NETWORK_TESTS=1")
def test_requirements_builds_a_working_venv(tmp_path):
    """The claim the file makes, executed: fresh venv, four imports, pytest runs."""
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True,
                   capture_output=True)
    py = venv / "bin" / "python3"
    subprocess.run([str(py), "-m", "pip", "install", "-q", "-r", str(REQUIREMENTS)],
                   check=True, capture_output=True, timeout=600)
    subprocess.run([str(py), "-c", "import yaml, requests, telegram"], check=True,
                   capture_output=True)
    out = subprocess.run([str(py), "-m", "pytest", "--version"], check=True,
                         capture_output=True, text=True)
    assert "pytest" in (out.stdout + out.stderr).lower()
