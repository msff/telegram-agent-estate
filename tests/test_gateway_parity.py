"""Gateway parity suite (U11) — the phase-B gate, and the plugin's self-test.

WHAT THIS IS FOR. Every other test file proves one module keeps its contract.
This one proves the *stack* behaves at least as well as the screen-and-inject
stack it replaces, scenario by scenario, with nothing mocked between the WAL and
the reply. It is the gate that says the cutover may happen, and at U12 it ships
into the plugin as the self-test a fresh instance runs after install.

TWO MODES.

  fast (default) — the eleven scenarios run against an injected fake invoker.
      No claude, no network, no tokens, ~1s. This is what CI-less `pytest
      tests/` runs on every change, and it is what catches a regression in the
      WAL / coalescing / retry / dead-letter logic.

  real  (`--real`) — the same scenarios drive actual `claude -p` turns under
      the real permission allowlist, and the ones that only mean something
      end-to-end (session recall across a restart, recall across a rotation,
      CLAUDE.md freshness) unskip. ~12 turns, a few minutes, real tokens, and
      REAL TELEGRAM MESSAGES to the owner chat — every one prefixed `[PARITY]`
      so they are obviously drills. Run it deliberately:

          libexec/parity.sh <instance.yaml> --real

      which is also what supplies $ESTATE_PARITY_LIVE_CONFIG — the instance
      whose workdir, allowlist and MCP config the drill borrows. Driving pytest
      by hand means supplying both:

          ESTATE_PARITY_REAL=1 ESTATE_PARITY_LIVE_CONFIG=<instance.yaml> \
              venv/bin/python3 -m pytest tests/test_gateway_parity.py -q -s

Fast mode cannot prove the allowlist is complete — a missing rule only shows up
when a real turn tries the real command. That is precisely why real mode exists
and why KTD12 names this suite as the allowlist's gate.

ISOLATION, AND ITS ONE LIMIT. Both modes run against a tmp GATEWAY state dir
(WAL, drain lock, session map, dead-letter), handed to the turn's subprocess
through the parity instance's `env_passthrough` — the env sanitizer drops
everything it is not told to keep. So a drill never touches the live WAL or
session map.

The send journal is the exception, and it is deliberate rather than forgotten:
`msg_index` resolves against the AGENT state dir (`<workdir>/state`), and in
real mode the workdir IS the live instance's, because the turn has to think
with the real CLAUDE.md. A `--real` drill therefore appends its `[PARITY]`
outbounds to that instance's own msg-index — which is also why the assertions
can read them back. Nothing else of the instance's state is written.
"""
import json
import os
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import instance_config
import msg_index
import turn_runner as tr

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
LIBEXEC = PLUGIN_ROOT / "libexec"
REAL = os.environ.get("ESTATE_PARITY_REAL") == "1"
real_only = pytest.mark.skipif(
    not REAL, reason="needs real claude turns: libexec/parity.sh <config> --real")


# --- what the instance under test is, resolved rather than assumed -----------
#
# All three read the config the `gw` fixture installed, per call. None of them
# may become a module constant or a literal: in real mode they name a chat that
# receives real messages, a repo the turn really writes in, and a command the
# turn is really told to run — all three belong to whoever installed the
# instance, not to whoever wrote this file.

def owner():
    """The chat id under test."""
    return instance_config.load().owner_chat_id


def workdir():
    """The repo the turn thinks in.

    Tests 7 and 9 used a module-level `WORKDIR` that does not exist in this
    file and never did; they would have raised NameError the moment real mode
    started running them.
    """
    return instance_config.load().workdir


def send_command():
    """The command a turn must run to reply, derived from the instance under
    test.

    Not a literal. Six of these prompts named the reading instance's original
    send script until this unit — a file that does not ship here and that takes
    its text as `--text`, where `send.py` takes a bare positional. On the
    author's machine they would pass by accident; for anyone else the turn is
    told to run a nonexistent command, sends nothing, and the suite reports a
    missing allowlist rule — R5's gate reading as a permanent false negative.
    """
    c = instance_config.load()
    return (f"{shlex.quote(str(c.python))} "
            f"{shlex.quote(str(LIBEXEC / 'send.py'))}")


# --- harness ----------------------------------------------------------------

def write_parity_instance(tmp_path, chat_id, live=None):
    """A throwaway instance rooted in tmp.

    In REAL mode it inherits the LIVE instance's workdir, interpreter,
    permissions and MCP config — the turn has to run against the real CLAUDE.md
    and the real allowlist, since proving the allowlist complete is the entire
    reason real mode exists — but its STATE roots stay in tmp so a drill never
    touches the live WAL or session map.
    """
    import sys, yaml
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    workdir = str(live.workdir) if live else str(tmp_path)
    data = {
        "name": "parity", "label": "Parity Drill",
        "workdir": workdir,
        # The instance's own interpreter, not the one running pytest: it is what
        # `send_command()` puts in front of send.py, and the allowlist rule the
        # drill is checking names that exact path.
        "python": str(live.python) if live else sys.executable,
        "telegram": {"owner_chat_id": chat_id,
                     "token_file": str(live.token_file) if live else str(tmp_path / "env")},
        "runtime": {"state_dir": str(tmp_path), "log_dir": str(tmp_path / "logs")},
        "env_passthrough": ["TELEGRAM_STATE_DIR", "ESTATE_GATEWAY_STATE_DIR"],
    }
    if live is not None and live.permission_settings:
        data["permissions"] = {"mode": live.permission_mode,
                               "settings_file": str(live.permission_settings)}
        if live.mcp_config:
            data["permissions"]["mcp_config"] = str(live.mcp_config)
    p = tmp_path / "parity-instance.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


@pytest.fixture
def gw(tmp_path, monkeypatch, owner_chat_id, live_config):
    """A private gateway + state root for one scenario.

    `live_config` is None in fast mode and the instance under drill in real
    mode; conftest refuses a real run that cannot name one, so this fixture
    never has to guess.
    """
    monkeypatch.setenv(instance_config.CONFIG_ENV,
                       str(write_parity_instance(tmp_path, owner_chat_id,
                                                 live_config)))
    # The gateway root is ALSO exported so a real turn's child process resolves
    # the same tmp state the assertions read; without it the turn's `reply`
    # journals into the live index and the send-evidence check looks in the
    # wrong place.
    monkeypatch.setenv("ESTATE_GATEWAY_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tr, "HANDOFF_PATH", None)
    return tmp_path


def block(text, mid):
    return (f'<channel source="telegram" chat_id="{owner()}" message_id="{mid}">'
            f"\n{text}\n</channel>")


def fake_invoker(session_id="parity-sess", reply="ok", record=None, sends=True,
                 fail_first=0):
    """Fast-mode stand-in for `claude -p`: succeeds, and mirrors a real turn by
    stamping the send journal with ESTATE_TURN_ID (which is what the runner
    reads as delivery truth). `fail_first` makes the first N calls fail."""
    state = {"n": 0}

    def _inv(argv, timeout):
        state["n"] += 1
        if record is not None:
            record.append(argv)
        if state["n"] <= fail_first:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        if sends:
            tid = os.environ.get("ESTATE_TURN_ID")
            if tid:
                msg_index.append_raw(owner(), 9000 + state["n"], reply,
                                     meta={"turn_id": tid})
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"session_id": session_id, "result": reply}),
            stderr="")
    return _inv


def invoker(**kw):
    """The invoker under test: None in real mode (turn_runner uses the real
    `claude -p` path), the fake otherwise."""
    return None if REAL else fake_invoker(**kw)


def _raw_rows():
    from estate_client import read_jsonl
    return [r for r in read_jsonl(msg_index.index_path())
            if r.get("type") == "raw"]


def replies_since(mark):
    """Outbound texts journaled after `mark` (a msg-index row count)."""
    return [r.get("text", "") for r in _raw_rows()[mark:]]


def index_mark():
    return len(_raw_rows())


def ask(text, mid, alert=None, **kw):
    """Journal one owner message and drain it — the whole inbound path."""
    tr.journal_inbound(mid, "message", owner(), block(text, mid))
    return tr.drain(invoker=invoker(**kw), alert=alert or _silent,
                    probe=_no_probe)


def _silent(*_a, **_k):
    return None


def _no_probe():
    return None


def nonce():
    return uuid.uuid4().hex[:8].upper()


# --- 1. ad-hoc echo parity ---------------------------------------------------

def test_1_adhoc_message_gets_exactly_one_reply(gw):
    """The baseline the screen stack already met: a message in, one reply out,
    the entry settled. Everything else is a variation on this."""
    mark = index_mark()
    tag = nonce()
    s = ask(f'[PARITY] Drill. Send exactly the line PARITY-ECHO-{tag} by '
            f'running: {send_command()} "PARITY-ECHO-{tag}"', 1,
            reply=f"PARITY-ECHO-{tag}")
    assert s["turns"] == 1 and s["dead_letter"] == 0
    assert tr.pending_entries() == []
    out = replies_since(mark)
    assert len(out) == 1, f"expected exactly one outbound, got {out}"
    if REAL:
        assert tag in out[0], out[0]


# --- 2. scheduled fire + marker + duplicate no-op ----------------------------

def test_2_scheduled_job_fires_once_and_the_duplicate_no_ops(gw, monkeypatch):
    """launchd wakes twice (sleep catch-up); the second fire must be a no-op.
    The guard's exit codes are the contract the plists read: 0 enqueued,
    3 marker already exists."""
    marker_written = {"v": False}

    def guard(job, not_before=None, **kw):
        return 3 if marker_written["v"] else 0

    def lease(job):
        pass

    rc1 = tr.enqueue_job("parity-job", "[SCHEDULED] parity drill", chat_id=owner(),
                         guard_fn=guard, lease_writer=lease)
    assert rc1 == 0
    s = tr.drain(invoker=invoker(), alert=_silent, probe=_no_probe)
    assert s["turns"] == 1
    marker_written["v"] = True                    # the send primitive wrote it

    rc2 = tr.enqueue_job("parity-job", "[SCHEDULED] parity drill", chat_id=owner(),
                         guard_fn=guard, lease_writer=lease)
    assert rc2 == 3, "a duplicate fire must not enqueue a second turn"
    assert tr.pending_entries() == []
    s2 = tr.drain(invoker=invoker(), alert=_silent, probe=_no_probe)
    assert s2["turns"] == 0


# --- 3. crash-replay dedup ---------------------------------------------------

def test_3_a_crash_after_the_send_does_not_answer_twice(gw):
    """The failure the screen stack could not survive: the turn sent, then the
    process died before the entry was settled. Reconciliation must recognise the
    delivered turn from its journal anchor instead of replaying it."""
    tr.journal_inbound(3, "message", owner(), block("[PARITY] crash drill", 3))
    turn_id = uuid.uuid4().hex
    tr.journal_turn_started(turn_id, [3])
    msg_index.append_raw(owner(), 4242, "already answered",
                         meta={"turn_id": turn_id})       # the send got out
    # ...and then the process died here, before mark_processed.

    calls = []
    s = tr.drain(invoker=fake_invoker(record=calls), alert=_silent,
                 probe=_no_probe)
    assert s["reconciled"] == 1
    assert calls == [], "the entry was replayed — the user is answered twice"
    assert tr.pending_entries() == []


# --- 4. rapid messages coalesce ---------------------------------------------

def test_4_rapid_messages_coalesce_into_one_turn(gw):
    """Three messages typed in a burst are one thought, not three turns."""
    mark = index_mark()
    for i, part in enumerate(["[PARITY] раз", "два", "три"], start=10):
        tr.journal_inbound(i, "message", owner(), block(part, i))
    calls = []
    s = tr.drain(invoker=invoker(record=calls), alert=_silent, probe=_no_probe)
    assert s["turns"] == 1, "a burst must not fan out into one turn per message"
    if not REAL:
        prompt = calls[0][2]
        for part in ("раз", "два", "три"):
            assert part in prompt
    assert len(replies_since(mark)) == 1


# --- 5. scheduled vs ad-hoc serialization ------------------------------------

def test_5_a_job_never_coalesces_with_a_user_message(gw):
    """A scheduled brief and a chat message must stay separate turns — a job's
    outcome has to remain attributable to the job."""
    tr.journal_inbound(20, "message", owner(), block("[PARITY] чат", 20))
    tr.enqueue_job("parity-serial", "[SCHEDULED] parity brief", chat_id=owner(),
                   guard_fn=lambda *a, **k: 0, lease_writer=lambda j: None)
    tr.journal_inbound(21, "message", owner(), block("[PARITY] ещё чат", 21))
    groups = tr.group_into_turns(tr.pending_entries())
    assert [len(g) for g in groups] == [1, 1, 1]
    assert groups[1][0]["source"] == "scheduler"
    calls = []
    s = tr.drain(invoker=invoker(record=calls), alert=_silent, probe=_no_probe)
    assert s["turns"] == 3
    if not REAL:
        assert "чат" not in calls[1][2], "the job prompt leaked user text"


# --- 6. restart recall (the headline improvement) ----------------------------

@real_only
def test_6_context_survives_a_full_restart(gw):
    """THE reason for the gateway. Under the screen stack, a claude restart —
    nightly, or a crash — wiped the conversation. Here every turn is already its
    own process, so "restart everything" is the normal case: the session id in
    the map is what carries the thread.

    The probe fact is a random nonce that exists in no file, so a correct answer
    can only have come from the resumed session."""
    fact = nonce()
    ask(f'[PARITY] Drill. Remember the code {fact}. Confirm by running: '
        f'{send_command()} "REMEMBERED {fact}"', 30)
    session_after_first = tr.get_session(owner())
    assert session_after_first, "no session captured — nothing to resume"

    # "Restart everything": drop every in-process trace. The session map on disk
    # is all that survives, which is exactly the claim under test.
    import importlib
    importlib.reload(tr)

    mark = index_mark()
    ask(f'[PARITY] Which code did I ask you to remember? Answer with that one '
        f'word by running: {send_command()} "<the code>"', 31)
    out = replies_since(mark)
    assert out and fact in out[0], (
        f"context lost across restart: expected {fact}, got {out}")


# --- 7. rotation recall ------------------------------------------------------

@real_only
def test_7_context_survives_the_daily_rotation(gw):
    """Rotation bounds transcript growth by dropping the session — so the thread
    has to survive in the handoff file the flush turn writes, not in the
    transcript. This is the one scenario that proves the flush is real."""
    rel = "state/parity-scratch.md"
    scratch = workdir() / rel
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text("# parity scratch\n", encoding="utf-8")
    fact = nonce()
    monkey = pytest.MonkeyPatch()
    # Point the flush and the bootstrap prime at a scratch file so a drill never
    # writes noise into the real handoff. `state/**` is already in the
    # allowlist, so this needs no permission change.
    #
    # These patch FUNCTIONS, not constants. `FLUSH_PROMPT` and `PRIME_PREFIX`
    # became `flush_prompt()` / `prime_prefix()` when the prompts moved into
    # per-instance files; patching the old names bound a dead attribute and the
    # runner went on calling the real prompt — invisible for as long as real
    # mode never ran, and an AttributeError under `raising=True` the moment it
    # did.
    monkey.setattr(tr, "flush_prompt", lambda config=None: (
        f"(System task — do NOT send anything to Telegram.) Write every code I "
        f"asked you to remember in this conversation into {rel}."))
    monkey.setattr(tr, "prime_prefix", lambda config=None: (
        f"(System task) A new session is starting. First read {rel}, then "
        f"answer the message(s) below.\n\n"))
    monkey.setattr(tr, "HANDOFF_PATH", scratch)   # what rotate() checks changed
    try:
        ask(f"[PARITY] Drill. Remember the code {fact}.", 40)
        r = tr.rotate(owner())
        assert r["status"] == "ok" and r["flushed"] is True
        assert tr.get_session(owner()) is None, "rotation did not drop the session"
        assert fact in scratch.read_text(encoding="utf-8"), (
            "the flush turn did not persist the fact — post-rotation recall "
            "would be lost")
        mark = index_mark()
        ask(f'[PARITY] Which code did I ask you to remember? Answer with that '
            f'one word by running: {send_command()} "<the code>"', 41)
        out = replies_since(mark)
        assert out and fact in out[0], f"recall lost across rotation: {out}"
    finally:
        monkey.undo()
        scratch.unlink(missing_ok=True)


# --- 8. failure -> retry -> alert -------------------------------------------

def test_8_a_failing_turn_retries_once_then_alerts(gw):
    """A turn that cannot complete must not retry forever, must not vanish
    silently, and must tell the owner."""
    tr.journal_inbound(50, "message", owner(), block("[PARITY] fail drill", 50))
    calls, alerts = [], []

    def always_fails(argv, timeout):
        calls.append(argv)
        return SimpleNamespace(returncode=1, stdout="", stderr="parity: forced")

    s = tr.drain(invoker=always_fails, alert=alerts.append, probe=_no_probe)
    assert len(calls) == tr.max_retries() + 1
    assert s["dead_letter"] == 1
    assert alerts and "dead-letter" in alerts[0]
    assert tr.pending_entries() == [], "a poison entry must stop blocking"


def test_8b_a_turn_that_exits_zero_without_sending_is_not_accepted(gw):
    """Exit status is not delivery truth (KTD12 correction): a denied tool still
    exits 0 while the model narrates the refusal. Such a turn must retry."""
    tr.journal_inbound(51, "message", owner(), block("[PARITY] silent drill", 51))
    calls = []
    s = tr.drain(invoker=fake_invoker(record=calls, sends=False),
                 alert=_silent, probe=_no_probe)
    assert len(calls) == tr.max_retries() + 1
    assert s["dead_letter"] == 1


# --- 9. CLAUDE.md freshness --------------------------------------------------

@real_only
def test_9_a_claude_md_edit_is_visible_to_the_very_next_turn(gw):
    """Documents whether the screen stack's worst ops gotcha is dead. There, the
    session read CLAUDE.md once at start, so an edit needed a restart (and a
    lost conversation) to take effect. Every `-p` turn is a fresh process, so
    the edit should land immediately — this asserts it rather than assuming."""
    marker = nonce()
    md = workdir() / "CLAUDE.md"
    original = md.read_text(encoding="utf-8")
    md.write_text(original + f"\n<!-- parity probe: {marker} -->\n",
                  encoding="utf-8")
    try:
        mark = index_mark()
        ask(f'[PARITY] Find the comment of the form "parity probe: XXXX" in '
            f'CLAUDE.md and send ONLY its code by running: '
            f'{send_command()} "<the code>"', 60)
        out = replies_since(mark)
        assert out and marker in out[0], (
            f"the turn did not see the fresh CLAUDE.md: {out}")
    finally:
        md.write_text(original, encoding="utf-8")


# --- 10. instance isolation --------------------------------------------------

def test_10_the_gateway_never_reaches_across_instances(gw, tmp_path):
    """A mac can carry several instances of this plugin at once. Nothing here
    may see or sweep another instance's state.

    The three couplings that could cross: the state root (env-scoped), the
    process probe (the poller's cmdline is unique to this repo), and the outbound
    allowlist (owner chat only)."""
    # 1. state root: every gateway path resolves under the env-scoped dir.
    for p in (tr.wal_path(), tr.lock_path(), tr.session_map_path(),
              tr.dead_letter_path()):
        assert str(p).startswith(str(tmp_path)), p
    # 2. process identity. Under the plugin BOTH instances run the same
    #    libexec/poller.py, so a path-keyed probe matches both and one supervisor's
    #    restart reaps the other's poller. Every probe must key on the instance
    #    tag instead.
    sup = (LIBEXEC / "supervisor.sh").read_text(encoding="utf-8")
    assert 'pgrep -f "poller.py.*$TAG"' in sup, "poller probe is not tag-scoped"
    assert '$TAG.*drain' in sup, "drain probe is not tag-scoped"
    assert "bun server.ts" not in sup
    # 3. outbound: the send path refuses any chat but the owner's.
    import send as send_mod
    assert send_mod.allowed_chat_ids() == {owner()}
    # 4. the auto-loading telegram channel plugin: it loads inside every turn's
    #    claude, and its DEFAULT state dir belongs to whichever instance was
    #    installed first — whose .env holds THAT instance's token. The
    #    supervisor must point it at this instance's blank .env, and the runner
    #    must pass that pointer through the env sanitizer, or a turn could end
    #    up polling the other bot.
    assert 'TELEGRAM_STATE_DIR="$CHANNEL_STATE_DIR"' in sup
    # ...and the runner really does forward it (config-driven passthrough).
    mp = pytest.MonkeyPatch()
    mp.setenv("TELEGRAM_STATE_DIR", "/tmp/parity-channel-dir")
    try:
        assert tr._child_env().get("TELEGRAM_STATE_DIR") == "/tmp/parity-channel-dir"
    finally:
        mp.undo()
    # and the sanitizer really does drop everything else
    monkey = pytest.MonkeyPatch()
    monkey.setenv("TELEGRAM_BOT_TOKEN", "other-instance-token-must-not-leak")
    monkey.setenv("ESTATE_ENV_PASSTHROUGH", "TELEGRAM_STATE_DIR")
    monkey.setenv("TELEGRAM_STATE_DIR", "/blank/dir")
    try:
        env = tr._child_env()
    finally:
        monkey.undo()
    assert env.get("TELEGRAM_STATE_DIR") == "/blank/dir"
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env


# --- 11. poison entry is parked, the next message still answered -------------

def test_11_a_poison_entry_does_not_block_the_queue(gw):
    """The property the spool never had: one undeliverable message must not
    wedge everything behind it.

    Staged as it actually happens — the poison arrives, fails its turn, and the
    NEXT message arrives afterwards. (Had both been in one burst they would have
    coalesced into a single turn and dead-lettered together, which is the right
    behaviour: a turn-level failure is rarely about one specific message.)"""
    state = {"n": 0}
    seen = []

    def poison_then_fine(argv, timeout):
        state["n"] += 1
        seen.append(argv[2])
        if "poison" in argv[2]:
            return SimpleNamespace(returncode=1, stdout="", stderr="poisoned")
        tid = os.environ.get("ESTATE_TURN_ID")
        if tid:
            msg_index.append_raw(owner(), 7000 + state["n"], "ok",
                                 meta={"turn_id": tid})
        return SimpleNamespace(returncode=0, stdout=json.dumps(
            {"session_id": "parity-sess", "result": "ok"}), stderr="")

    alerts = []
    tr.journal_inbound(70, "message", owner(), block("[PARITY] poison", 70))
    s = tr.drain(invoker=poison_then_fine, alert=alerts.append, probe=_no_probe)
    assert s["dead_letter"] == 1
    assert alerts, "the owner was never told a message was parked"

    tr.journal_inbound(71, "message", owner(), block("[PARITY] healthy", 71))
    s2 = tr.drain(invoker=poison_then_fine, alert=alerts.append, probe=_no_probe)
    assert s2["turns"] == 1 and s2["dead_letter"] == 0
    assert any("healthy" in p for p in seen), "the healthy message never ran"
    assert tr.pending_entries() == []
    dead = [json.loads(l) for l in
            tr.dead_letter_path().read_text(encoding="utf-8").splitlines() if l]
    assert [d["update_id"] for d in dead] == [70], "only the poison was parked"


# --- the suite's own switches ------------------------------------------------
#
# Real mode is the gate that proves a permission allowlist complete, and for its
# whole life it could not start: `parity.sh` passed `--real`, nothing declared
# the option, and pytest rejected the run with `unrecognized arguments` before
# collecting a test. A gate that never runs reports nothing and looks like
# nothing is wrong, so the switches themselves are now under test.

def test_fast_mode_never_reaches_the_network(gw, monkeypatch):
    """Fast mode's entire claim is that it costs nothing — no claude, no
    tokens, no Telegram. Both are one wrong line away: `invoker()` returning
    None in fast mode would spawn a real turn, and a scenario calling `reply`
    instead of journaling directly would hit the Bot API."""
    if REAL:
        pytest.skip("this asserts the fast-mode contract")
    import send as send_mod
    monkeypatch.setattr(send_mod, "bot_api_sender", lambda *a, **k: pytest.fail(
        "fast mode reached the Telegram API"))
    monkeypatch.setattr(tr, "_default_invoker", lambda *a, **k: pytest.fail(
        "fast mode spawned a real `claude -p` turn"))
    mark = index_mark()
    s = ask("[PARITY] fast-mode drill", 90)
    assert s["turns"] == 1 and s["dead_letter"] == 0
    assert len(replies_since(mark)) == 1


def _nested_pytest(tmp_path, *args, live=None):
    """Run this file in a child pytest, collection only, with both parity
    variables under our control. Collection is enough: everything under test
    here happens in `pytest_configure`, before a test runs."""
    import sys
    env = {k: v for k, v in os.environ.items()
           if k not in ("ESTATE_PARITY_REAL", "ESTATE_PARITY_LIVE_CONFIG")}
    if live is not None:
        env["ESTATE_PARITY_LIVE_CONFIG"] = str(live)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--rootdir", str(PLUGIN_ROOT),
         str(Path(__file__).resolve()), "--collect-only", "-q", *args],
        capture_output=True, text=True, cwd=str(tmp_path), env=env, timeout=300)


def test_the_real_flag_is_a_declared_option(tmp_path, owner_chat_id):
    """`--real` must PARSE. Until this unit nothing declared it, so every
    `parity.sh <config> --real` died on argument parsing — the allowlist gate
    never once reached a verdict."""
    live = write_parity_instance(tmp_path, owner_chat_id)
    proc = _nested_pytest(tmp_path, "--real", live=live)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "unrecognized arguments" not in (proc.stdout + proc.stderr)


def test_real_mode_without_a_live_config_says_which_value_is_missing(tmp_path):
    """Real mode borrows a live instance's workdir, allowlist and MCP config;
    with none named it used to fall back to a tmp instance and drive turns
    against an empty repo. Fail, and name the variable."""
    proc = _nested_pytest(tmp_path, "--real")
    assert proc.returncode != 0
    assert "ESTATE_PARITY_LIVE_CONFIG" in (proc.stdout + proc.stderr)
    for wrong in ("NameError", "AttributeError", "Traceback"):
        assert wrong not in (proc.stdout + proc.stderr), proc.stdout + proc.stderr


def test_parity_sh_hands_real_mode_everything_it_needs():
    """The script and the suite have to agree on all three switches, and the
    disagreements were silent: the flag was undeclared, the live config was
    never exported, and the plugin's conftest — the file that declares the flag
    — is not even loaded when pytest infers its rootdir from a single test file
    under `tests/`."""
    sh = _code(LIBEXEC / "parity.sh")
    assert "--real" in sh, "the script no longer passes the flag"
    assert '--rootdir' in sh, (
        "without an explicit rootdir pytest never loads the conftest that "
        "declares --real")
    assert 'ESTATE_PARITY_LIVE_CONFIG="$CONFIG"' in sh, (
        "real mode is not told which instance it is drilling")
    assert "unset ESTATE_PARITY_REAL" in sh, (
        "a variable exported by an earlier drill would turn a plain fast run "
        "into one that spends tokens and sends real messages")


# --- cutover wiring ----------------------------------------------------------

def _code(path):
    """Shell source with comment lines stripped — these assertions are about
    what the supervisor DOES, and the header comment legitimately names the
    machinery the cutover removed."""
    return "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                     if not l.lstrip().startswith("#"))

def test_supervisor_and_wrappers_are_wired_to_the_gateway():
    """Guards the cutover itself: the shell layer must drive the runner, not the
    screen. These are the four edits that make the gateway live, and a silent
    revert of any of them is the kind of thing nothing else would catch."""
    sup = _code(LIBEXEC / "supervisor.sh")
    assert "turn_runner.py" in sup, "supervisor never drains the WAL"
    assert "housekeeping.sh" in sup, "supervisor never runs the daily window"
    assert "screen -L -dmS" not in sup, "still spawning a screen session"
    assert "inject.sh" not in sup, "still injecting into a screen session"
    hk = _code(LIBEXEC / "housekeeping.sh")
    assert "rotate" in hk, "the daily window no longer rotates"
    job = _code(LIBEXEC / "run-job.sh")
    assert "run-job" in job, "the job wrapper no longer drives the runner"
    assert "inject.sh" not in job, "the job wrapper still injects"


def test_cli_version_tripwire_records_the_running_version():
    """CLI churn is a live risk (U5's --resume semantics have no stability
    contract and the CLI self-updates). The supervisor must notice a version
    change and hold turns until the suite re-passes."""
    sup = _code(LIBEXEC / "supervisor.sh")
    assert "--version" in sup
    assert "cli-version" in sup
    assert "turns-held" in sup, "nothing sets the hold flag"
    # ...and the runner must actually honour it, or the hold is decorative.
    import inspect
    assert "turns_held()" in inspect.getsource(tr.drain)


@real_only
def test_the_permission_allowlist_covers_a_real_send(gw):
    """KTD12's open question, answered only by a real turn: is the allowlist
    complete enough for the brain to actually reply? A missing rule shows up as
    a turn that exits 0 and sends nothing.

    The command below is the instance's own send entrypoint, so a failure here
    means the allowlist really is missing a rule — as opposed to the suite
    having named a command that exists nowhere, which is what it did until this
    unit and which no allowlist could ever have satisfied."""
    mark = index_mark()
    tag = nonce()
    s = ask(f'[PARITY] Send exactly the line PARITY-PERM-{tag} by running: '
            f'{send_command()} "PARITY-PERM-{tag}"', 80)
    assert s["dead_letter"] == 0, (
        f"the turn could not send — the allowlist is missing a rule for "
        f"`{send_command()} …` (check the instance's permission settings file "
        f"against the command it tried)")
    out = replies_since(mark)
    assert out and tag in out[0], out
