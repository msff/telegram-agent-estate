"""THE U13 GATE: two instances must not cross.

From `libexec/README.md`: *every string in the transplant table's right-hand column
comes from the instance config, and two mock instances must run side by side
without their sweeps, locks, or probes crossing.*

Why this file exists at all. Before the plugin, isolation was free — two
instances were two checkouts, so every path, every `pgrep` pattern, and every
lock differed by construction and no amount of carelessness could make them
collide. Consolidation destroys that guarantee and hands back the obligation to
manufacture it. Nothing in the ordinary test suite would notice its absence:
each instance passes all its own tests while quietly reading its neighbour's
marker, holding its neighbour's lock, or answering its neighbour's owner.

The failures this guards against are not crashes. They are a bot that goes
silent for a day because it read someone else's "already sent" marker, or a
second bot that never starts because it looked like a duplicate of the first.
Both present as healthy processes and clean logs.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import guard
import instance_config
import msg_index
import send
import turn_runner as tr

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
LIBEXEC = PLUGIN_ROOT / "libexec"


def build_instance(root, name, chat):
    """A complete, self-contained instance rooted at `root`."""
    root = Path(root)
    (root / "repo" / "state").mkdir(parents=True, exist_ok=True)
    (root / "repo" / "prompts").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "gw").mkdir(parents=True, exist_ok=True)
    (root / "repo" / "prompts" / "job.txt").write_text("do the thing\n", encoding="utf-8")
    (root / "env").write_text(f"TELEGRAM_BOT_TOKEN=fake-{name}\n", encoding="utf-8")
    os.chmod(root / "env", 0o600)
    data = {
        "name": name, "label": f"{name.title()} Bot",
        "workdir": str(root / "repo"), "python": sys.executable,
        "telegram": {"owner_chat_id": chat, "token_file": str(root / "env"),
                     "channel_state_dir": str(root / "chan")},
        "runtime": {"state_dir": str(root / "gw"), "log_dir": str(root / "logs")},
        "env_passthrough": ["TELEGRAM_STATE_DIR"],
        "schedules": [{"job": "morning-digest", "hour": 9, "minute": 0,
                       "prompt_file": "prompts/job.txt"}],
    }
    p = root / f"{name}.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return instance_config.load(p)


@pytest.fixture
def pair(tmp_path):
    """Alpha and Beta: different names, chats, workdirs, and state roots — i.e.
    exactly the situation the estate is for."""
    return (build_instance(tmp_path / "alpha", "alpha", 111),
            build_instance(tmp_path / "beta", "beta", 222))


# --- 1. no derived name is shared --------------------------------------------

def test_no_state_path_is_shared(pair):
    a, b = pair
    paths = lambda c: {
        "gateway": c.gateway_state_dir, "agent": c.agent_state_dir,
        "log": c.log_dir, "handoff": c.handoff_path, "token": c.token_file,
        "workdir": c.workdir, "channel": c.channel_state_dir,
    }
    pa, pb = paths(a), paths(b)
    for key in pa:
        assert pa[key] != pb[key], f"{key} is shared between instances"
        assert not str(pa[key]).startswith(str(pb[key])), f"{key} nests inside beta's"
        assert not str(pb[key]).startswith(str(pa[key])), f"{key} nests inside alpha's"


def test_no_launchd_label_is_shared(pair):
    a, b = pair
    assert a.launchd_label() != b.launchd_label()
    for job in ("morning-digest", "evening", "weekly"):
        assert a.launchd_label(job) != b.launchd_label(job)


# --- 2. probes select exactly one instance ------------------------------------

def test_process_probes_do_not_match_the_other_instance(pair):
    """Both instances run the SAME libexec/poller.py, so a probe keyed on the script
    path matches both. A supervisor restarting its own poller would then reap
    its neighbour's — and the neighbour's supervisor would restart it, forever."""
    a, b = pair
    argv_a = f"{sys.executable} {LIBEXEC}/poller.py {a.instance_tag()} --config {a.source}"
    argv_b = f"{sys.executable} {LIBEXEC}/poller.py {b.instance_tag()} --config {b.source}"

    assert re.search(a.poller_match(), argv_a)
    assert not re.search(a.poller_match(), argv_b)
    assert re.search(b.poller_match(), argv_b)
    assert not re.search(b.poller_match(), argv_a)


def test_drain_probe_does_not_match_the_other_instance(pair):
    """The supervisor's `pgrep -f "turn_runner.py.*$TAG.*drain"`. Shared, it
    would make one instance skip its own backstop drain for as long as the
    other stayed busy — silently, every tick."""
    a, b = pair
    pat = lambda c: f"turn_runner.py.*{re.escape(c.instance_tag())}.*drain"
    argv_a = f"{sys.executable} {LIBEXEC}/turn_runner.py {a.instance_tag()} --config {a.source} drain"
    argv_b = f"{sys.executable} {LIBEXEC}/turn_runner.py {b.instance_tag()} --config {b.source} drain"
    assert re.search(pat(a), argv_a) and not re.search(pat(a), argv_b)
    assert re.search(pat(b), argv_b) and not re.search(pat(b), argv_a)


# --- 3. locks do not block each other -----------------------------------------

def test_drain_locks_are_independent(pair, monkeypatch):
    """Instance A holding its drain lock must not stop B from draining. A shared
    lock path reads as "the other bot is mid-turn" and makes each instance skip
    its own work."""
    a, b = pair
    monkeypatch.setenv(instance_config.CONFIG_ENV, str(a.source))
    fd_a = tr._acquire_lock()
    assert fd_a is not None

    monkeypatch.setenv(instance_config.CONFIG_ENV, str(b.source))
    fd_b = tr._acquire_lock()
    assert fd_b is not None, "beta's drain was blocked by alpha's lock"

    monkeypatch.setenv(instance_config.CONFIG_ENV, str(a.source))
    assert tr._acquire_lock() is None, "a genuine duplicate of alpha must still block"
    fd_a.close()
    fd_b.close()


# --- 4. the quiet catastrophe: markers ----------------------------------------

def test_a_marker_written_by_one_instance_does_not_silence_the_other(pair):
    """The worst failure in this file, because it looks like nothing at all.
    "morning-digest" is not an unusual job name. Under a shared marker root,
    A writes its marker at 09:00 and B's 09:00 guard reads it, concludes its own
    digest already went out, and skips. Every process healthy, every log clean,
    one bot simply silent for the day."""
    from datetime import datetime
    a, b = pair
    today = datetime.now().strftime("%Y-%m-%d")

    guard.marker_path("morning-digest", today, a).write_text("sent", encoding="utf-8")
    assert guard.guard("morning-digest", cfg=a) == guard.GUARD_MARKER
    assert guard.guard("morning-digest", cfg=b) == guard.GUARD_OK


def test_a_lease_held_by_one_instance_does_not_block_the_other(pair):
    a, b = pair
    guard.lease_path("morning-digest", a).write_text("busy", encoding="utf-8")
    assert guard.guard("morning-digest", cfg=a) == guard.GUARD_LEASE_FRESH
    assert guard.guard("morning-digest", cfg=b) == guard.GUARD_OK


# --- 5. WAL, sessions, journals -----------------------------------------------

def test_wal_and_session_map_do_not_cross(pair, monkeypatch):
    a, b = pair
    monkeypatch.setenv(instance_config.CONFIG_ENV, str(a.source))
    tr.journal_inbound(1, "message", a.owner_chat_id, "alpha's message")
    tr.set_session(a.owner_chat_id, "sess-alpha")

    monkeypatch.setenv(instance_config.CONFIG_ENV, str(b.source))
    assert tr.pending_entries() == [], "beta sees alpha's pending work"
    assert tr.get_session(b.owner_chat_id) is None
    tr.set_session(b.owner_chat_id, "sess-beta")

    monkeypatch.setenv(instance_config.CONFIG_ENV, str(a.source))
    assert tr.get_session(a.owner_chat_id) == "sess-alpha", "beta overwrote alpha's session"
    assert len(tr.pending_entries()) == 1


def test_send_journals_do_not_cross(pair):
    a, b = pair
    msg_index.append_raw(a.owner_chat_id, 1, "alpha's reply", cfg=a)
    assert msg_index.get_message(a.owner_chat_id, 1, cfg=a) is not None
    assert msg_index.get_message(a.owner_chat_id, 1, cfg=b) is None


# --- 6. the security boundary --------------------------------------------------

def test_neither_instance_can_send_into_the_others_chat(pair):
    a, b = pair
    assert send.allowed_chat_ids(a) == {111}
    assert send.allowed_chat_ids(b) == {222}
    with pytest.raises(send.SendError, match="not on the owner allowlist"):
        send.reply("cross-talk", chat_id=b.owner_chat_id, cfg=a,
                   sender=lambda cid, t: pytest.fail("reached the network"))


def test_each_instance_reads_only_its_own_token(pair):
    from estate_client import load_secrets
    a, b = pair
    assert load_secrets(cfg=a)["TELEGRAM_BOT_TOKEN"] == "fake-alpha"
    assert load_secrets(cfg=b)["TELEGRAM_BOT_TOKEN"] == "fake-beta"


def test_a_tag_config_mismatch_is_refused_by_both_entry_points(pair):
    """Pairing one instance's tag with another's config is the most dangerous
    misconfiguration available — the process would answer as the wrong bot."""
    a, b = pair
    for script in ("poller.py", "turn_runner.py"):
        argv = [sys.executable, str(LIBEXEC / script), f"--instance={a.name}",
                "--config", str(b.source)]
        if script == "turn_runner.py":
            argv.append("drain")
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 2, f"{script} accepted a mismatched identity"
        assert "confused identity" in proc.stderr


# --- 7. rotation, the destructive path ----------------------------------------

def test_rotating_one_instance_leaves_the_others_handoff_untouched(pair, monkeypatch):
    """rotate() prunes and rewrites the handoff — the only routinely destructive
    operation in the system, and the one that already reached production state
    once from a test run (2026-07-31)."""
    a, b = pair
    for c, text in ((a, "alpha memory"), (b, "beta memory")):
        c.handoff_path.write_text(
            f"## Current state (as of 2026-08-01 09:00 CEST)\n\n{text}\n"
            f"## Current state (as of 2026-07-01 09:00 CEST)\n\nold {text}\n",
            encoding="utf-8")
    before_b = (b.handoff_path.read_text(encoding="utf-8"),
                b.handoff_path.stat().st_mtime_ns)

    monkeypatch.setenv(instance_config.CONFIG_ENV, str(a.source))
    monkeypatch.setattr(tr, "HANDOFF_PATH", None)
    monkeypatch.setattr(tr, "HANDOFF_KEEP_SNAPSHOTS", 1)
    tr.set_session(a.owner_chat_id, "sess-alpha")
    tr.rotate(a.owner_chat_id, invoker=lambda argv, timeout: __import__(
        "types").SimpleNamespace(returncode=0, stdout='{"session_id":"s","result":"r"}',
                                 stderr=""))

    assert "old alpha memory" not in a.handoff_path.read_text(encoding="utf-8")
    assert (b.handoff_path.read_text(encoding="utf-8"),
            b.handoff_path.stat().st_mtime_ns) == before_b


# --- 8. the installer refuses a name collision --------------------------------

def test_installer_refuses_two_configs_sharing_a_name(tmp_path):
    """Everything launchd-visible is namespaced by `name`, so a second instance
    reusing it would silently take over the first's labels, logs and schedule."""
    first = build_instance(tmp_path / "one", "samename", 111)
    second = build_instance(tmp_path / "two", "samename", 222)
    agents = tmp_path / "agents"
    agents.mkdir()
    env = {**os.environ, "ESTATE_LAUNCH_AGENTS": str(agents)}

    ok = subprocess.run([str(LIBEXEC / "install-instance.sh"), str(first.source), "--no-load"],
                        capture_output=True, text=True, env=env, timeout=120)
    assert ok.returncode == 0, ok.stderr

    clash = subprocess.run([str(LIBEXEC / "install-instance.sh"), str(second.source), "--no-load"],
                           capture_output=True, text=True, env=env, timeout=120)
    assert clash.returncode == 1
    assert "already belongs to a different instance config" in clash.stdout


def test_installer_renders_valid_plists_for_both_instances(pair, tmp_path):
    """Both instances install side by side, and every rendered plist is valid —
    checked with plutil, which is what caught the weekday-conditional bug that
    produced a syntactically broken StartCalendarInterval."""
    a, b = pair
    agents = tmp_path / "agents"
    agents.mkdir()
    env = {**os.environ, "ESTATE_LAUNCH_AGENTS": str(agents)}

    for c in (a, b):
        proc = subprocess.run(
            [str(LIBEXEC / "install-instance.sh"), str(c.source), "--no-load"],
            capture_output=True, text=True, env=env, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr

    # Derived, not spelled out. These filenames used to be four literals
    # carrying the author's reverse domain, so the test asserted a personal
    # default rather than the property it is about: that the installer names
    # every plist the way `instance_config` says, one per instance and one per
    # that instance's schedules.
    expected = sorted(f"{c.launchd_label(job)}.plist"
                      for c in (a, b) for job in (None, "morning-digest"))
    plists = sorted(p.name for p in agents.glob("*.plist"))
    assert plists == expected
    for p in agents.glob("*.plist"):
        lint = subprocess.run(["plutil", "-lint", str(p)], capture_output=True, text=True)
        assert lint.returncode == 0, f"{p.name}: {lint.stdout}{lint.stderr}"
        # plutil is LENIENT and launchd is too, so neither catches a `--` inside
        # an XML comment — which is illegal, and which the live reading-morning
        # plist carried for weeks unnoticed. Parse strictly as well, so any
        # tooling that uses a real XML parser keeps working.
        import plistlib
        with open(p, "rb") as fh:
            plistlib.load(fh)
        # A scheduled job must never carry KeepAlive: launchd would start it at
        # LOAD time and respawn it every ThrottleInterval, firing it off-window
        # and burning the day's marker so the real scheduled run no-ops.
        if "-morning-digest" in p.name:
            ka = subprocess.run(["plutil", "-extract", "KeepAlive", "raw", str(p)],
                                capture_output=True, text=True)
            assert ka.returncode != 0, f"{p.name} carries KeepAlive"


# --- 9. the turn environment ---------------------------------------------------

def test_turn_env_carries_this_instances_channel_state_dir(pair, monkeypatch):
    """TELEGRAM_STATE_DIR must reach the turn, and must be THIS instance's.

    A turn's auto-loading telegram channel plugin polls whatever token it finds
    in the channel state dir it is pointed at. Unset, it falls back to the
    machine default — which belongs to whichever instance was installed first
    and holds ITS token. The turn would then poll the other bot's token and
    409-war its live poller. Config-driven passthrough is what prevents it.
    """
    a, b = pair
    monkeypatch.setenv("TELEGRAM_STATE_DIR", str(a.channel_state_dir))
    monkeypatch.setenv(instance_config.CONFIG_ENV, str(a.source))

    env = tr._child_env(a)
    assert env.get("TELEGRAM_STATE_DIR") == str(a.channel_state_dir)
    assert env["TELEGRAM_STATE_DIR"] != str(b.channel_state_dir)


def test_turn_env_never_carries_the_bot_token_or_an_api_key(pair, monkeypatch):
    """Two hard exclusions: the bot token would arm the channel plugin inside
    the turn's own process, and an API key would move billing off the
    subscription onto metered credits (KTD1)."""
    a, _ = pair
    monkeypatch.setenv(instance_config.CONFIG_ENV, str(a.source))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "should-not-propagate")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-propagate")

    env = tr._child_env(a)
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
