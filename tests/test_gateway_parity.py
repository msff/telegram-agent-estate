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

  real  (ESTATE_PARITY_REAL=1) — the same scenarios drive actual `claude -p`
      turns under the real permission allowlist, and the ones that only mean
      something end-to-end (session recall across a restart, recall across a
      rotation, CLAUDE.md freshness) unskip. ~12 turns, a few minutes, real
      tokens, and REAL TELEGRAM MESSAGES to the owner chat — every one prefixed
      `[PARITY]` so they are obviously drills. Run it deliberately:

          ESTATE_PARITY_REAL=1 venv/bin/python3 -m pytest tests/test_gateway_parity.py -q -s

Fast mode cannot prove the allowlist is complete — a missing rule only shows up
when a real turn tries the real command. That is precisely why real mode exists
and why KTD12 names this suite as the allowlist's gate.

ISOLATION. Both modes run against a tmp gateway state dir, so a parity run never
touches the live WAL or session map. In real mode the tmp dirs are handed to the
turn's subprocess through ESTATE_ENV_PASSTHROUGH — the env sanitizer (U8r)
drops everything it is not told to keep, so without that passthrough a real turn
would journal its send into the LIVE msg-index and the send-evidence check would
read the wrong file. That is the one piece of wiring real mode needs.
"""
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import instance_config
import msg_index
import turn_runner as tr

OWNER = 10000000001
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
BIN = PLUGIN_ROOT / "bin"
REAL = os.environ.get("ESTATE_PARITY_REAL") == "1"
real_only = pytest.mark.skipif(
    not REAL, reason="needs real claude turns: ESTATE_PARITY_REAL=1")


# --- harness ----------------------------------------------------------------

def write_parity_instance(tmp_path, live=None):
    """A throwaway instance rooted in tmp.

    In REAL mode it inherits the LIVE instance's workdir, permissions and MCP
    config — the turn has to run against the real CLAUDE.md and the real
    allowlist, since proving the allowlist complete is the entire reason real
    mode exists — but its STATE roots stay in tmp so a drill never touches the
    live WAL, session map, or handoff.
    """
    import sys, yaml
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    workdir = str(live.workdir) if live else str(tmp_path)
    data = {
        "name": "parity", "label": "Parity Drill",
        "workdir": workdir, "python": sys.executable,
        "telegram": {"owner_chat_id": OWNER,
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
def gw(tmp_path, monkeypatch):
    """A private gateway + state root for one scenario."""
    live = None
    if REAL:
        # Real mode needs the live instance's brain and allowlist; see above.
        live = instance_config.load(os.environ["ESTATE_PARITY_LIVE_CONFIG"]) \
            if os.environ.get("ESTATE_PARITY_LIVE_CONFIG") else None
    monkeypatch.setenv(instance_config.CONFIG_ENV,
                       str(write_parity_instance(tmp_path, live)))
    # The gateway root is ALSO exported so a real turn's child process resolves
    # the same tmp state the assertions read; without it the turn's `reply`
    # journals into the live index and the send-evidence check looks in the
    # wrong place.
    monkeypatch.setenv("ESTATE_GATEWAY_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tr, "HANDOFF_PATH", None)
    return tmp_path


def block(text, mid):
    return (f'<channel source="telegram" chat_id="{OWNER}" message_id="{mid}">'
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
                msg_index.append_raw(OWNER, 9000 + state["n"], reply,
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
    tr.journal_inbound(mid, "message", OWNER, block(text, mid))
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
    s = ask(f"[PARITY] Ответь ровно строкой: PARITY-ECHO-{tag}. "
            f"Отправь её через send_transaction.py reply.", 1,
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

    rc1 = tr.enqueue_job("parity-job", "[SCHEDULED] parity drill", chat_id=OWNER,
                         guard_fn=guard, lease_writer=lease)
    assert rc1 == 0
    s = tr.drain(invoker=invoker(), alert=_silent, probe=_no_probe)
    assert s["turns"] == 1
    marker_written["v"] = True                    # the send primitive wrote it

    rc2 = tr.enqueue_job("parity-job", "[SCHEDULED] parity drill", chat_id=OWNER,
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
    tr.journal_inbound(3, "message", OWNER, block("[PARITY] crash drill", 3))
    turn_id = uuid.uuid4().hex
    tr.journal_turn_started(turn_id, [3])
    msg_index.append_raw(OWNER, 4242, "already answered",
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
        tr.journal_inbound(i, "message", OWNER, block(part, i))
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
    tr.journal_inbound(20, "message", OWNER, block("[PARITY] чат", 20))
    tr.enqueue_job("parity-serial", "[SCHEDULED] parity brief", chat_id=OWNER,
                   guard_fn=lambda *a, **k: 0, lease_writer=lambda j: None)
    tr.journal_inbound(21, "message", OWNER, block("[PARITY] ещё чат", 21))
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
    ask(f"[PARITY] Запомни число {fact}. Ответь ровно 'ЗАПОМНИЛ {fact}' "
        f"через send_transaction.py reply.", 30)
    session_after_first = tr.get_session(OWNER)
    assert session_after_first, "no session captured — nothing to resume"

    # "Restart everything": drop every in-process trace. The session map on disk
    # is all that survives, which is exactly the claim under test.
    import importlib
    importlib.reload(tr)

    mark = index_mark()
    ask(f"[PARITY] Какое число я просил запомнить? Ответь одним словом "
        f"через send_transaction.py reply.", 31)
    out = replies_since(mark)
    assert out and fact in out[0], (
        f"context lost across restart: expected {fact}, got {out}")


# --- 7. rotation recall ------------------------------------------------------

@real_only
def test_7_context_survives_the_daily_rotation(gw):
    """Rotation bounds transcript growth by dropping the session — so the thread
    has to survive in the handoff file the flush turn writes, not in the
    transcript. This is the one scenario that proves the flush is real."""
    scratch = WORKDIR / "state" / "parity-scratch.md"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text("# parity scratch\n", encoding="utf-8")
    fact = nonce()
    monkey = pytest.MonkeyPatch()
    # Point the flush and the bootstrap prime at a scratch file so a drill never
    # writes noise into the real session-handoff.md. `state/**` is already in the
    # allowlist, so this needs no permission change.
    monkey.setattr(tr, "FLUSH_PROMPT",
                   "Запиши в файл state/parity-scratch.md все числа, которые "
                   "я просил запомнить в этом разговоре. Ничего не отправляй "
                   "в Telegram.")
    monkey.setattr(tr, "PRIME_PREFIX",
                   "Сначала прочитай state/parity-scratch.md, затем ответь.\n\n")
    monkey.setattr(tr, "HANDOFF_PATH", scratch)   # what rotate() checks changed
    try:
        ask(f"[PARITY] Запомни число {fact}.", 40)
        r = tr.rotate(OWNER)
        assert r["status"] == "ok" and r["flushed"] is True
        assert tr.get_session(OWNER) is None, "rotation did not drop the session"
        assert fact in scratch.read_text(encoding="utf-8"), (
            "the flush turn did not persist the fact — post-rotation recall "
            "would be lost")
        mark = index_mark()
        ask("[PARITY] Какое число я просил запомнить? Ответь одним словом "
            "через send_transaction.py reply.", 41)
        out = replies_since(mark)
        assert out and fact in out[0], f"recall lost across rotation: {out}"
    finally:
        monkey.undo()
        scratch.unlink(missing_ok=True)


# --- 8. failure -> retry -> alert -------------------------------------------

def test_8_a_failing_turn_retries_once_then_alerts(gw):
    """A turn that cannot complete must not retry forever, must not vanish
    silently, and must tell the owner."""
    tr.journal_inbound(50, "message", OWNER, block("[PARITY] fail drill", 50))
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
    tr.journal_inbound(51, "message", OWNER, block("[PARITY] silent drill", 51))
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
    md = WORKDIR / "CLAUDE.md"
    original = md.read_text(encoding="utf-8")
    md.write_text(original + f"\n<!-- parity probe: {marker} -->\n",
                  encoding="utf-8")
    try:
        mark = index_mark()
        ask("[PARITY] Найди в CLAUDE.md комментарий вида 'parity probe: XXXX' "
            "и отправь через send_transaction.py reply только его код.", 60)
        out = replies_since(mark)
        assert out and marker in out[0], (
            f"the turn did not see the fresh CLAUDE.md: {out}")
    finally:
        md.write_text(original, encoding="utf-8")


# --- 10. instance isolation --------------------------------------------------

def test_10_the_gateway_never_reaches_across_instances(gw, tmp_path):
    """Two stacks live on this mac (reading + second-instance). Nothing here may see or
    sweep the other instance's state.

    The three couplings that could cross: the state root (env-scoped), the
    process probe (the poller's cmdline is unique to this repo), and the outbound
    allowlist (owner chat only)."""
    # 1. state root: every gateway path resolves under the env-scoped dir.
    for p in (tr.wal_path(), tr.lock_path(), tr.session_map_path(),
              tr.dead_letter_path()):
        assert str(p).startswith(str(tmp_path)), p
    # 2. process identity. Under the plugin BOTH instances run the same
    #    bin/poller.py, so a path-keyed probe matches both and one supervisor's
    #    restart reaps the other's poller. Every probe must key on the instance
    #    tag instead.
    sup = (BIN / "supervisor.sh").read_text(encoding="utf-8")
    assert 'pgrep -f "poller.py.*$TAG"' in sup, "poller probe is not tag-scoped"
    assert '$TAG.*drain' in sup, "drain probe is not tag-scoped"
    assert "bun server.ts" not in sup
    # 3. outbound: the send path refuses any chat but the owner's.
    import send as send_mod
    assert send_mod.allowed_chat_ids() == {OWNER}
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
    monkey.setenv("TELEGRAM_BOT_TOKEN", "second-instance-token-must-not-leak")
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
            msg_index.append_raw(OWNER, 7000 + state["n"], "ok",
                                 meta={"turn_id": tid})
        return SimpleNamespace(returncode=0, stdout=json.dumps(
            {"session_id": "parity-sess", "result": "ok"}), stderr="")

    alerts = []
    tr.journal_inbound(70, "message", OWNER, block("[PARITY] poison", 70))
    s = tr.drain(invoker=poison_then_fine, alert=alerts.append, probe=_no_probe)
    assert s["dead_letter"] == 1
    assert alerts, "the owner was never told a message was parked"

    tr.journal_inbound(71, "message", OWNER, block("[PARITY] healthy", 71))
    s2 = tr.drain(invoker=poison_then_fine, alert=alerts.append, probe=_no_probe)
    assert s2["turns"] == 1 and s2["dead_letter"] == 0
    assert any("healthy" in p for p in seen), "the healthy message never ran"
    assert tr.pending_entries() == []
    dead = [json.loads(l) for l in
            tr.dead_letter_path().read_text(encoding="utf-8").splitlines() if l]
    assert [d["update_id"] for d in dead] == [70], "only the poison was parked"


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
    sup = _code(BIN / "supervisor.sh")
    assert "turn_runner.py" in sup, "supervisor never drains the WAL"
    assert "housekeeping.sh" in sup, "supervisor never runs the daily window"
    assert "screen -L -dmS" not in sup, "still spawning a screen session"
    assert "inject.sh" not in sup, "still injecting into a screen session"
    hk = _code(BIN / "housekeeping.sh")
    assert "rotate" in hk, "the daily window no longer rotates"
    job = _code(BIN / "run-job.sh")
    assert "run-job" in job, "the job wrapper no longer drives the runner"
    assert "inject.sh" not in job, "the job wrapper still injects"


def test_cli_version_tripwire_records_the_running_version():
    """CLI churn is a live risk (U5's --resume semantics have no stability
    contract and the CLI self-updates). The supervisor must notice a version
    change and hold turns until the suite re-passes."""
    sup = _code(BIN / "supervisor.sh")
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
    a turn that exits 0 and sends nothing."""
    mark = index_mark()
    tag = nonce()
    s = ask(f"[PARITY] Отправь через send_transaction.py reply ровно строку "
            f"PARITY-PERM-{tag}", 80)
    assert s["dead_letter"] == 0, (
        "the turn could not send — the allowlist is missing a rule "
        "(check daemon/permissions.json against the command it tried)")
    out = replies_since(mark)
    assert out and tag in out[0], out
