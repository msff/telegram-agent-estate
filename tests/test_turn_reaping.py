"""A killed turn has to leave nothing behind, and has to say so somewhere.

Both halves come from one incident on the reading instance, 2026-08-04, in which
a turn hit its 900s timeout and:

  1. **its tool kept running.** `subprocess.run(timeout=)` kills the direct
     child and waits for it, but `claude` is a supervisor of its own tools and
     its grandchildren are not in that kill. They reparent to PID 1. The
     observed orphan went on making API calls for 19 more minutes and completed
     a write nobody was waiting for — after the turn had been journaled failed,
     so a retry could have started while it was still writing.

  2. **nothing logged it.** The poller's drain ran with `stdout=DEVNULL` and
     surfaced stderr only on a non-zero exit, so every drain triggered by an
     incoming message — which is every conversational turn — wrote nothing
     anywhere. `drain.log` had no line at the minute the turn was killed. The
     runner had logged it; there was no file underneath.

These are tested with real process trees and real files rather than with mocks,
because both bugs were in the seam between this code and the OS: a mock of
`Popen` would have happily reported that a grandchild it never created had been
killed.
"""
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml

import instance_config
import poller as telegram_poll
import turn_runner

OWNER = 10_000_000_001


def write_instance(tmp_path, name="reapinst"):
    (tmp_path / "repo" / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    data = {
        "name": name, "label": "Reap Test", "workdir": str(tmp_path / "repo"),
        "python": sys.executable,
        "telegram": {"owner_chat_id": OWNER, "token_file": str(tmp_path / "env")},
        "runtime": {"state_dir": str(tmp_path / "gw"),
                    "log_dir": str(tmp_path / "logs")},
    }
    p = tmp_path / f"{name}.yaml"
    p.write_text(yaml.safe_dump(data, width=10_000), encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _instance(tmp_path, monkeypatch):
    monkeypatch.setenv(instance_config.CONFIG_ENV, str(write_instance(tmp_path)))


def alive(pid):
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def wait_gone(pid, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


# --- the fake turn: a parent that spawns a tool and outlives nothing ----------

def make_tree_script(tmp_path, *, grandchild_ignores_term=False):
    """A stand-in for `claude`: spawns a long-running 'tool', reports both pids,
    then blocks forever.

    The tool writes its own pid and then sleeps — the shape of the real orphan,
    which was a sync script holding HTTPS connections. The parent never reaps
    it, exactly as the CLI does not.
    """
    # Assembled line by line rather than dedented from a template: an
    # interpolated block at column 0 makes the common prefix empty, `dedent`
    # then does nothing, and the tool dies on an IndentationError before it can
    # write the pid this test waits for — which reads exactly like the reaping
    # working.
    lines = ["import os, pathlib, signal, sys, time"]
    if grandchild_ignores_term:
        lines.append("signal.signal(signal.SIGTERM, signal.SIG_IGN)")
    lines += [
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))",
        "while True:",
        "    time.sleep(0.05)",
    ]
    tool = tmp_path / "tool.py"
    tool.write_text("\n".join(lines) + "\n", encoding="utf-8")

    script = tmp_path / "fake_claude.py"
    script.write_text(textwrap.dedent(f"""\
        import os, subprocess, sys, time
        pidfile = sys.argv[-1]
        subprocess.Popen([sys.executable, {str(tool)!r}, pidfile])
        print("started", flush=True)
        while True:
            time.sleep(0.05)
        """), encoding="utf-8")
    return script


def read_pid(path, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = Path(path).read_text().strip()
            if text:
                return int(text)
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError(f"the tool never wrote its pid to {path}")


# --- 1. the orphan ------------------------------------------------------------

def test_a_timed_out_turn_takes_its_whole_process_tree_with_it(tmp_path):
    """The incident, reproduced: without the process-group kill the tool below
    survives its parent and keeps running unsupervised."""
    pidfile = tmp_path / "tool.pid"
    script = make_tree_script(tmp_path)

    with pytest.raises(subprocess.TimeoutExpired):
        turn_runner._default_invoker(
            [sys.executable, str(script), str(pidfile)], 2)

    tool_pid = read_pid(pidfile)
    assert wait_gone(tool_pid), (
        f"the tool (pid {tool_pid}) outlived the turn that spawned it — it is "
        "now reparented to PID 1 and running unsupervised")


def test_a_tool_that_ignores_sigterm_is_still_killed(tmp_path):
    """SIGTERM is sent first so the CLI can flush the session state a later
    --resume needs. A tool that ignores it must not thereby survive."""
    pidfile = tmp_path / "tool.pid"
    script = make_tree_script(tmp_path, grandchild_ignores_term=True)

    with pytest.raises(subprocess.TimeoutExpired):
        turn_runner._default_invoker(
            [sys.executable, str(script), str(pidfile)], 2)

    tool_pid = read_pid(pidfile)
    assert wait_gone(tool_pid), f"pid {tool_pid} ignored SIGTERM and was never killed"


def test_the_turn_runs_in_its_own_process_group(tmp_path):
    """What makes the group kill safe. Without a new session the child shares
    the runner's group, and killing that group would take down the poller and
    the supervisor along with the turn."""
    script = tmp_path / "pgid.py"
    script.write_text("import os; print(os.getpgrp())\n", encoding="utf-8")

    res = turn_runner._default_invoker([sys.executable, str(script)], 30)

    assert res.returncode == 0, res.stderr
    assert int(res.stdout.strip()) != os.getpgrp(), (
        "the turn shares the runner's process group")


def test_a_timeout_is_still_reported_as_a_timeout(tmp_path):
    """The reaping must not change what the caller sees: `_backend_a` turns
    `TimeoutExpired` into the `timeout` error the retry and dead-letter path
    branch on."""
    script = tmp_path / "hang.py"
    script.write_text("import time\nwhile True: time.sleep(0.05)\n", encoding="utf-8")

    result = turn_runner._backend_a(
        "prompt", None,
        lambda argv, timeout: turn_runner._default_invoker(
            [sys.executable, str(script)], 2),
        2)

    assert result.ok is False
    assert result.error == "timeout"


def test_a_normal_turn_still_returns_stdout_stderr_and_status(tmp_path):
    """The contract `_backend_a` parses. Swapping `run` for `Popen` is exactly
    the kind of change that silently returns bytes, or drops stderr."""
    script = tmp_path / "ok.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.write('{\"result\": \"hi\", \"session_id\": \"s1\"}')\n"
        "sys.stderr.write('a warning')\n", encoding="utf-8")

    res = turn_runner._default_invoker([sys.executable, str(script)], 30)

    assert res.returncode == 0
    assert isinstance(res.stdout, str) and isinstance(res.stderr, str)
    assert '"result": "hi"' in res.stdout
    assert res.stderr == "a warning"


def test_a_nonzero_exit_is_passed_through_untouched(tmp_path):
    script = tmp_path / "fail.py"
    script.write_text("import sys; sys.stderr.write('boom'); sys.exit(7)\n",
                      encoding="utf-8")

    res = turn_runner._default_invoker([sys.executable, str(script)], 30)

    assert res.returncode == 7
    assert "boom" in res.stderr


# --- 2. the kill never turns on the runner ------------------------------------

def test_the_group_kill_refuses_to_fire_on_the_runners_own_group(monkeypatch):
    """The single most dangerous line in this file. If `start_new_session` ever
    stops taking effect, the child's pgid IS the runner's, and a killpg would
    take down the poller, the supervisor and every other turn on the machine.
    """
    killed = []
    monkeypatch.setattr(turn_runner.os, "getpgid", lambda pid: os.getpgrp())
    monkeypatch.setattr(turn_runner.os, "killpg",
                        lambda pgid, sig: killed.append((pgid, sig)))

    class Proc:
        pid = 4242
        def __init__(self): self.killed = False
        def kill(self): self.killed = True
        def wait(self, timeout=None): return 0

    proc = Proc()
    turn_runner._kill_process_group(proc)

    assert killed == [], "killpg fired on the runner's own process group"
    assert proc.killed, "the child was not killed by any means"


def test_the_group_kill_never_raises(monkeypatch):
    """It runs inside timeout handling. An exception here would replace a
    logged timeout with an unlogged crash, and the drain lock is held."""
    def boom(*a, **k):
        raise OSError("no such process")

    monkeypatch.setattr(turn_runner.os, "getpgid", boom)

    class Proc:
        pid = 4243
        def kill(self): pass
        def wait(self, timeout=None): return 0

    turn_runner._kill_process_group(Proc())  # must not raise


def test_sigterm_precedes_sigkill(monkeypatch):
    sent = []
    monkeypatch.setattr(turn_runner.os, "getpgid", lambda pid: 99_999)
    monkeypatch.setattr(turn_runner.os, "getpgrp", lambda: 1)
    monkeypatch.setattr(turn_runner.os, "killpg",
                        lambda pgid, sig: sent.append(sig))

    class Proc:
        pid = 4244
        def kill(self): pass
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("x", 1)

    turn_runner._kill_process_group(Proc())

    assert sent == [signal.SIGTERM, signal.SIGKILL]


# --- 3. the drain that logged nothing -----------------------------------------

def drain_log(tmp_path):
    return tmp_path / "logs" / "drain.log"


def run_trigger(tmp_path, body, **kw):
    """Drive `trigger_drain` against a stand-in runner and wait for it."""
    import asyncio

    runner = tmp_path / "fake_runner.py"
    runner.write_text(textwrap.dedent(body), encoding="utf-8")
    state = {}

    async def go():
        await telegram_poll.trigger_drain(state=state, runner=str(runner),
                                          python=sys.executable, **kw)
        await state["task"]

    asyncio.run(go())


def test_a_triggered_drain_writes_its_output_to_drain_log(tmp_path):
    """The hole the incident fell through: until U11 this was `DEVNULL`, so the
    conversational path — every drain a message triggers — logged nothing."""
    run_trigger(tmp_path, """
        print("turn_runner: drain {'status': 'ok', 'turns': 1}")
        """)

    text = drain_log(tmp_path).read_text(encoding="utf-8")
    assert "turn_runner: drain" in text


def test_a_failing_turns_log_line_reaches_the_file(tmp_path):
    """The specific line that was missing at 21:04 on 2026-08-04."""
    run_trigger(tmp_path, """
        print("turn_runner: turn failed (timeout) attempt 1/2")
        """)

    assert "turn failed (timeout)" in drain_log(tmp_path).read_text(encoding="utf-8")


def test_stderr_lands_in_the_same_file(tmp_path):
    """A traceback on stderr was previously kept only when the exit code was
    non-zero — and a drain that dead-letters a turn still exits 0."""
    run_trigger(tmp_path, """
        import sys
        sys.stderr.write("turn_runner: owner alert failed: nope\\n")
        """)

    assert "owner alert failed" in drain_log(tmp_path).read_text(encoding="utf-8")


def test_drains_append_rather_than_truncate(tmp_path):
    for n in (1, 2):
        run_trigger(tmp_path, f"""
            print("drain number {n}")
            """)

    text = drain_log(tmp_path).read_text(encoding="utf-8")
    assert "drain number 1" in text and "drain number 2" in text


def test_a_drain_still_runs_when_its_log_cannot_be_opened(tmp_path, monkeypatch):
    """Logging is not the point of a drain. If the log dir is unwritable the
    turn still has to happen."""
    marker = tmp_path / "it-ran"
    logdir = tmp_path / "logs"
    logdir.mkdir(exist_ok=True)
    (logdir / "drain.log").mkdir()  # a directory where the file should be

    run_trigger(tmp_path, f"""
        import pathlib
        pathlib.Path({str(marker)!r}).write_text("yes")
        """)

    assert marker.exists(), "an unwritable log stopped the drain from running"
