#!/usr/bin/env python3
"""U6 — receipt WAL + flock-serialized turn runner (the gateway core).

This is the piece that replaces `screen -X stuff` injection with a resumable
headless `claude -p` gateway. Two invariants make it correct where the old
inject path was best-effort:

  1. At-least-once inbound. Every owner update is journaled to an append-only
     WAL (`inbound-wal.jsonl`, keyed by Telegram `update_id`) BEFORE it is acted
     on. A crash between journal and turn is recovered by a re-drain: the entry
     is still pending, and dedup is by `update_id`, so it is answered exactly
     once. (The poller writes the journal; see daemon/telegram_poll.py.)

  2. Strict per-session serialization. A single non-blocking flock guards the
     drain, so two turns never resume the same session concurrently — U5 proved
     concurrent `--resume` of one id silently drops a turn from the transcript.
     The drain loops one turn-group at a time, re-folding the WAL after each, so
     a message that arrives mid-turn is picked up on the same drain instead of
     racing it.

Turn execution goes through the `run_turn()` seam with two interchangeable
backends (KTD11): backend A is headless `claude -p --resume` (primary,
subscription-billed today); backend B is the interactive-session driver and
lands in U6f — but the seam, the `runner_backend` selector, and the billing
leak-detector that can flip A→B are all built here, so nothing downstream has to
be re-plumbed to gain the fallback.

Coalescing is source-aware: consecutive owner MESSAGES fold into one turn (the
user typed three lines, answer once); a SCHEDULER entry always runs as its own
turn so its exit status stays attributable to its job.

State lives OUTSIDE any synced folder (a high-frequency append-only log must not
fight the sync daemon, and a lock file on a synced volume is not a lock): the
instance config's `runtime.state_dir`, overridable via `ESTATE_GATEWAY_STATE_DIR`
(tests point it at a tmp dir).

INSTANCE-PARAMETERIZED (U13). Every path, timeout, prompt, and chat id below
comes from the instance config; nothing is derived from this file's own
location. That last part is the important one: `WORKDIR = Path(__file__).parent
.parent` was correct only while each instance had its own checkout of this file.
Under the plugin the same file serves every instance, so a path derived from it
resolves to the PLUGIN directory — shared by all instances — rather than to the
caller's workdir.

The WAL fold, coalescing, retry/dead-letter control flow, quota guard, and
backend selection are all pure or injectable, so the whole unit is tested
without the network, the CLI, or a live session.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

LIBEXEC = Path(__file__).resolve().parent
sys.path.insert(0, str(LIBEXEC))
PLUGIN_ROOT = LIBEXEC.parent

import instance_config  # noqa: E402
import msg_index  # noqa: E402  (U8 retry-safety: query the send journal)
from estate_client import append_jsonl, log, read_jsonl  # noqa: E402


def cfg():
    """This instance's config. Resolved per call, never cached in a module
    global — see instance_config's docstring on why an import-time constant is
    a live hazard here."""
    return instance_config.load()


def workdir() -> Path:
    """The repo the agent thinks in. NOT derived from this file's location:
    under the plugin that would be the shared plugin directory."""
    return cfg().workdir

# --- tunables ---------------------------------------------------------------
# Per-turn wall-clock budgets. The old 300s was sized off U5's ~6s warm turn and
# was WRONG BY AN ORDER OF MAGNITUDE for real work: on cutover night the evening
# triage timed out twice at exactly 300s and dead-lettered, because composing a
# digest means querying Reader, building cards, persisting a payload and sending
# it. The screen stack had no turn timeout at all — inject.sh simply polled for
# the completion marker for ~15 min — so this budget is a NEW constraint the
# cutover introduced, and it has to be at least as generous as what it replaced.
#
# Split by source, because the two have genuinely different shapes: a chat reply
# that has not finished in 10 minutes is stuck, while a scheduled digest taking
# 15 is just doing its job.
#
# The VALUES live in the instance config (`runtime.turn_timeout` /
# `job_timeout` / `max_retries` / `quota_defer_at`), which also carries the
# fallbacks. Deliberately not duplicated here: two sources of truth for a number
# whose wrongness is invisible until a live job dies at exactly the wrong second
# is not a tradeoff worth making. Each instance must size these against ITS
# heaviest real job — a second-instance coach pulling three MCP servers and writing a
# calendar event has a different floor than a reading digest, and copying a
# number measured on another instance is what produced the 300s timeout.


def per_turn_timeout():
    return cfg().turn_timeout


def job_turn_timeout():
    return cfg().job_timeout


def max_retries():
    return cfg().max_retries


def quota_defer_at():
    return cfg().quota_defer_at
LEAK_FLIP_DELTA = 0.05          # a single turn moving the pool >5% = billing leak

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

# --- permission posture (KTD12) ---------------------------------------------
# `bypassPermissions` is the WRONG default for an unattended daemon, for three
# concrete reasons rather than general caution:
#   1. It can hang forever. Circuit-breaker prompts (`rm -rf /`, `rm -rf ~`)
#      still prompt even under bypass; with no TTY the turn blocks indefinitely
#      WHILE HOLDING the per-session flock — a gateway-wide deadlock, not a
#      lost turn.
#   2. It is refused outright when running as root outside a container, so a
#      plugin that hardcodes it is not portable to other machines.
#   3. Background sessions refuse the flag until the acceptance dialog has been
#      accepted interactively once — a fresh machine cannot just run it.
# `dontAsk` + an explicit allowlist gives no prompts, no hang, and DETERMINISTIC
# denials: a missing rule fails the turn loudly instead of granting silent
# power. (`auto` was considered and rejected: it defers to a safety classifier
# whose verdicts vary by version/machine — wrong for reproducible unattended
# runs.) Both are overridable per instance; bypass remains an opt-in escape
# hatch, documented as unsafe for daemons.
DEFAULT_PERMISSION_MODE = "dontAsk"

# MCP diet (KTD2's "conditional GO" clause, finally acted on). Every turn is a
# fresh process, so it pays the full MCP handshake every time — and it was
# booting all SEVEN user-scope servers when this instance's brain uses exactly
# one. Measured 2026-07-29 on an identical no-tool prompt:
#     all user-scope servers  27s
#     --strict-mcp-config     16s
#     readwise only           16s   <- same as none; the server itself is free
# So the diet is worth ~11s on EVERY turn, and costs nothing in capability.
# --strict-mcp-config is what makes it stick: without it the file below is
# merged with the user-scope config rather than replacing it.
#
# The diet is PER INSTANCE and cannot be inherited: the servers a reading bot
# needs are not the ones a second-instance coach needs, and the measured saving depends
# on how many the machine has. U15 gates second-instance's cutover on re-running this
# measurement with second-instance's own MCP set.


def _permission_args(config=None):
    """The turn's permission posture as CLI args.

    Environment first, then the instance config: a test or a one-off drill can
    override without editing the instance's file, but the instance's own
    settings are what run unattended.
    """
    c = config or cfg()
    mode = os.environ.get("ESTATE_PERMISSION_MODE") or c.permission_mode
    if mode == "bypassPermissions":
        return ["--dangerously-skip-permissions"]
    args = ["--permission-mode", mode]
    settings = os.environ.get("ESTATE_PERMISSION_SETTINGS") or c.permission_settings
    if settings and Path(settings).exists():
        args += ["--settings", str(settings)]
    return args


def _mcp_args(config=None):
    """Restrict the turn to this instance's own MCP servers.

    Both flags or neither: `--mcp-config` alone MERGES with the user-scope
    config, so it would add servers rather than replace them.

    Absent the file we deliberately fall back to the machine's full config
    rather than to none — losing a server the brain needs is a worse failure
    than a slow turn.
    """
    c = config or cfg()
    path = os.environ.get("ESTATE_MCP_CONFIG") or c.mcp_config
    if path and Path(path).exists():
        return ["--strict-mcp-config", "--mcp-config", str(path)]
    return []

# Prompts are per-instance FILES, not literals. Two reasons, both learned here:
# the text is in the brain's own language (the reading instance's are Cyrillic,
# to match how it thinks and to avoid colliding with the ASCII tokens the tests
# assert on), and it NAMES THE HANDOFF FILE, which is itself configurable. A
# hardcoded prompt would tell a differently-configured instance to read a file
# that does not exist — and that turn would cheerfully "succeed" having restored
# nothing, which is the silent-degradation mode this whole area exists to catch.
# `{handoff}` is substituted with the path relative to the workdir.
DEFAULT_PRIME_PROMPT = PLUGIN_ROOT / "templates" / "prompts" / "prime.txt"
DEFAULT_FLUSH_PROMPT = PLUGIN_ROOT / "templates" / "prompts" / "flush.txt"


def _prompt_text(kind, config=None):
    c = config or cfg()
    override = {"prime": c.prime_prompt_file, "flush": c.flush_prompt_file}[kind]
    default = {"prime": DEFAULT_PRIME_PROMPT, "flush": DEFAULT_FLUSH_PROMPT}[kind]
    path = Path(override) if override else default
    target = handoff_path(c)
    try:
        target = target.relative_to(c.workdir)
    except ValueError:
        pass                      # handoff outside the workdir: leave it absolute
    return path.read_text(encoding="utf-8").rstrip("\n").format(handoff=target)


def prime_prefix(config=None):
    """Bootstrap priming (U7): when a chat has no session yet (fresh start or
    after a daily rotation), the first turn runs without --resume and carries
    this preamble, so the new session begins by reading the distilled
    conversational context out of the handoff file, not an empty transcript."""
    return _prompt_text("prime", config) + "\n\n"


def flush_prompt(config=None):
    """Rotation (U7): a silent flush turn on the current session before it is
    dropped — distil the day into the handoff file, do not push to Telegram."""
    return _prompt_text("flush", config)


# The file the flush turn must actually change. Resolved through the instance
# config per call, NEVER a module constant: on 2026-07-31 a WORKDIR-derived
# constant here ignored the test suite's state-dir override, and a plain
# `pytest tests/` pruned the LIVE handoff file. Reads were harmless while
# nothing wrote through it; the moment rotate() gained a prune step, the
# redirection gap became a production write from pytest.
# Tests and the parity drill still override this attribute directly.
HANDOFF_PATH = None


def handoff_path(config=None):
    if HANDOFF_PATH is not None:
        return Path(HANDOFF_PATH)
    return (config or cfg()).handoff_path

TurnResult = namedtuple(
    "TurnResult", ["ok", "reply", "new_head", "exit_code", "error"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts):
    """Epoch seconds for a `_now_iso` stamp, or None if it is unusable.

    Returns None rather than raising or defaulting: the only caller is the
    stuck-entry detector, and a row whose age cannot be established must not be
    reported as stuck on the strength of a guess."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


# --- gateway (non-synced) state root ----------------------------------------

def gateway_state_dir() -> Path:
    """WAL, drain lock, session map — non-synced, and per instance.

    The env override still wins so tests can redirect it, but the fallback is
    now the instance's configured dir rather than a hardcoded one. A shared
    fallback would put two instances' drain locks at the same path, which reads
    as "the other bot is mid-turn" and makes each one skip its own work.
    """
    override = os.environ.get("ESTATE_GATEWAY_STATE_DIR")
    d = Path(override) if override else cfg().gateway_state_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def wal_path() -> Path:
    return gateway_state_dir() / "inbound-wal.jsonl"


def lock_path() -> Path:
    return gateway_state_dir() / "turn.lock"


def session_map_path() -> Path:
    return gateway_state_dir() / "session-map.json"


def backend_flag_path() -> Path:
    return gateway_state_dir() / "runner-backend"


def dead_letter_path() -> Path:
    return gateway_state_dir() / "dead-letter.jsonl"


def turns_held_path() -> Path:
    return gateway_state_dir() / "turns-held"


def turns_held():
    """Is the supervisor holding turns? Returns the reason string, or None.

    The CLI-version tripwire (U11) sets this when `claude --version` changes:
    U5's verified `-p --resume` semantics have no documented stability contract
    and the CLI self-updates silently, so a version bump pauses turn execution
    until the parity suite re-passes. Inbound is NOT paused — messages keep
    landing in the WAL and are answered once the hold lifts, which is the whole
    point of having a journal."""
    try:
        return turns_held_path().read_text(encoding="utf-8").strip() or "held"
    except OSError:
        return None


# --- WAL: append-only, typed records ----------------------------------------
# Record types on the one WAL:
#   {"type":"inbound","update_id":N,"source":"message"|"scheduler",...,"block":..}
#   {"type":"processed","update_ids":[...],"session":...}
#   {"type":"dead_letter","update_id":N,"reason":...}
# pending = inbound whose update_id is in neither the processed nor the
# dead-letter set, in WAL order.

def journal_inbound(update_id, source, chat_id, block,
                    job=None, message_id=None, meta=None) -> None:
    """Append one inbound record. Called by the poller BEFORE delivery (the
    journal-before-ack invariant) and by the scheduler wrapper (U9) for a
    synthetic entry. Idempotent-friendly: re-journaling the same update_id is
    harmless — the fold dedups on update_id and keeps the first.

    `meta` (optional dict) carries per-entry rendering/routing flags — U9 uses
    `silent` so the prompt template renders the no-reply directive."""
    append_jsonl(wal_path(), {
        "type": "inbound",
        "ts": _now_iso(),
        "update_id": update_id,
        "source": source,
        "chat_id": chat_id,
        "block": block,
        "job": job,
        "message_id": message_id,
        "meta": meta or {},
    })


def _scheduler_counter_path() -> Path:
    return gateway_state_dir() / "scheduler-seq"


def next_scheduler_update_id() -> int:
    """Allocate a NEGATIVE synthetic update_id for a scheduler entry.

    Telegram update_ids are positive, so the negative half is a disjoint id
    space that can never collide with a real update. This matters for the U8r
    watermark fix: a `max_processed_update_id` watermark folds `update_id <= N`
    as done, which would swallow scheduler entries if they shared the positive
    space. Allocation is a flock'd counter file so two jobs firing in the same
    second cannot collide (a collision would make the fold dedup one job away)."""
    p = _scheduler_counter_path()
    fd = os.open(str(p), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.read(fd, 64).decode("utf-8").strip()
        n = int(raw) if raw else 0
        n += 1
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(n).encode("utf-8"))
        return -n
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def mark_processed(update_ids, session=None) -> None:
    append_jsonl(wal_path(), {
        "type": "processed",
        "ts": _now_iso(),
        "update_ids": list(update_ids),
        "session": session,
    })


def journal_turn_started(turn_id, update_ids, job=None) -> None:
    """Record that a turn is ABOUT to be invoked for these entries.

    This is the crash-recovery anchor. Without it, a turn that sent its reply
    and then died before its processed-mark landed is re-invoked on restart with
    a freshly-minted turn_id, so the send journal cannot be correlated and the
    user is answered twice. With it, drain-start reconciliation can ask "did the
    turn we started for this entry already produce an outbound?" and settle the
    entry instead of replaying it."""
    append_jsonl(wal_path(), {
        "type": "turn_started",
        "ts": _now_iso(),
        "turn_id": turn_id,
        "update_ids": list(update_ids),
        "job": job,
    })


def dead_letter(update_id, reason) -> None:
    """Park a poison entry so the drain stops retrying it, and keep a durable
    copy in a dedicated file for the owner to inspect."""
    rec = {"type": "dead_letter", "ts": _now_iso(),
           "update_id": update_id, "reason": str(reason)[:500]}
    append_jsonl(wal_path(), rec)
    append_jsonl(dead_letter_path(), rec)


def _fold_rows(rows):
    """Fold WAL rows into (ordered inbound records, processed set, dead set).
    Duplicate inbound update_ids keep the first occurrence.

    The watermark makes dedup self-sufficient across compaction: compaction
    discards individual `processed` records, so without it a Telegram
    re-delivery of an already-answered update (the poll offset is confirmed on
    the NEXT getUpdates, so a crash can replay one) would fold as pending and be
    answered twice. Only POSITIVE ids are watermarked — Telegram's space —
    which is exactly why scheduler entries were given negative ids (U9): a
    watermark can never swallow a scheduled job."""
    inbound, seen, processed, dead = [], set(), set(), set()
    watermark = None
    for row in rows:
        t = row.get("type")
        if t == "inbound":
            uid = row.get("update_id")
            if uid in seen:
                continue
            seen.add(uid)
            inbound.append(row)
        elif t == "processed":
            processed.update(row.get("update_ids") or [])
        elif t == "dead_letter":
            dead.add(row.get("update_id"))
        elif t == "watermark":
            n = row.get("max_processed_update_id")
            if isinstance(n, int):
                watermark = n if watermark is None else max(watermark, n)
    if watermark is not None:
        for r in inbound:
            uid = r.get("update_id")
            if isinstance(uid, int) and uid > 0 and uid <= watermark:
                processed.add(uid)
    return inbound, processed, dead


def _fold_wal():
    return _fold_rows(read_jsonl(wal_path()))


def pending_entries():
    """Inbound records not yet processed or dead-lettered, in WAL order."""
    inbound, processed, dead = _fold_wal()
    done = processed | dead
    return [r for r in inbound if r.get("update_id") not in done]


# An owner message this old with no turn against it is not latency, it is a
# stuck gateway. The drain runs on a ~5 min supervisor tick and the slowest
# real job measured is under 8 min, so 30 min is well clear of both.
UNANSWERED_MAX_AGE_S = 1800


def unanswered_entries(max_age_s=UNANSWERED_MAX_AGE_S, now=None):
    """Inbound records the WAL shows no attempt against, older than max_age_s.

    THE GAP THIS CLOSES: every existing health probe answers "is the pipeline
    running", none answers "is it doing anything". On 2026-08-03 a poisoned
    watermark made `_fold_rows` classify real messages as already-processed, so
    `pending_entries()` was empty, the drain reported `turns: 0` forever, and
    the poller, backlog probe and scheduled jobs all stayed green while the bot
    ignored its owner for five hours. A pending-age check could not have caught
    it — the entries were not pending.

    What the failure DOES leave is an unambiguous on-disk signature: an
    `inbound` row with neither a `turn_started` nor a `processed` record naming
    its update_id. Every healthy path writes one or the other within a tick.
    This deliberately reads the raw rows rather than the fold, because the fold
    is the thing under suspicion — a detector built on it would inherit the bug
    it exists to find."""
    rows = read_jsonl(wal_path())
    attempted, done = set(), set()
    for row in rows:
        t = row.get("type")
        if t == "turn_started":
            attempted.update(row.get("update_ids") or [])
        elif t == "processed":
            done.update(row.get("update_ids") or [])
        elif t == "dead_letter":
            done.add(row.get("update_id"))
    cutoff = (now or time.time()) - max_age_s
    stuck = []
    for row in rows:
        if row.get("type") != "inbound":
            continue
        uid = row.get("update_id")
        if uid in attempted or uid in done:
            continue
        ts = _parse_iso(row.get("ts"))
        if ts is not None and ts <= cutoff:
            stuck.append(row)
    return stuck


def wal_size():
    """Byte length of the WAL, or -1 if it does not exist yet.

    This is the drain's lost-wakeup guard (U11). The race it closes: a poller
    append that lands AFTER the drain's final `pending_entries()` fold but
    BEFORE it releases the flock strands that entry — the poller's trigger gets
    "busy" and exits, and the lock holder never re-folds. Nothing is lost, but
    the reply waits for the next unrelated trigger, which on a quiet evening can
    be a long time.

    Size works as the change signal because `append_jsonl` writes each record
    with ONE `os.write` on an O_APPEND fd under flock: the file grows atomically
    per record, so "grew since I folded" is exactly "new work arrived since I
    folded". No extra counter file and no poller-side cooperation needed, which
    also means a hand-run `enqueue` from the CLI is caught by the same guard.

    Compaction shrinks the file, hence `!=` rather than `>` at the call site —
    but compaction only ever runs while holding the drain lock, so in practice it
    cannot interleave with a live drain anyway."""
    try:
        return wal_path().stat().st_size
    except FileNotFoundError:
        return -1


# --- coalescing (source-aware) ----------------------------------------------

def group_into_turns(entries):
    """Split pending entries into turn-groups. Consecutive `message` entries
    coalesce into one group; every `scheduler` entry is its own group. Order is
    preserved, so a scheduler entry between two messages breaks the run."""
    groups, run = [], []
    for e in entries:
        if e.get("source") == "scheduler":
            if run:
                groups.append(run)
                run = []
            groups.append([e])
        else:
            run.append(e)
    if run:
        groups.append(run)
    return groups


# Same reasoning as the prime/flush prompts: instance-supplied files, because
# these are addressed to the brain in its own language. Defaults ship with the
# plugin. `{chat_id}` is substituted in the reply directive.
DEFAULT_SILENT_DIRECTIVE = PLUGIN_ROOT / "templates" / "prompts" / "silent-directive.txt"
DEFAULT_REPLY_DIRECTIVE = PLUGIN_ROOT / "templates" / "prompts" / "reply-directive.txt"


def _directive(kind, config=None):
    c = config or cfg()
    override = {"silent": c.silent_directive_file,
                "reply": c.reply_directive_file}[kind]
    default = {"silent": DEFAULT_SILENT_DIRECTIVE,
               "reply": DEFAULT_REPLY_DIRECTIVE}[kind]
    path = Path(override) if override else default
    return path.read_text(encoding="utf-8").strip() + " "


def render_entry(entry):
    """Render one WAL entry into its prompt text.

    Message entries are already escaped `<channel>` blocks from the transport
    edge (telegram_poll.block_from_message_dict) and pass through verbatim; we
    never unescape, so a spoofed `</channel>` stays inert end-to-end.

    Scheduler entries (U9) carry the bare job prompt plus a `silent` metadata
    flag, and the directive is rendered HERE rather than baked into the stored
    text — that is what replaces inject.sh's prompt-prefix hack. Silent jobs
    (the 03:00 handoff flush) must not push a Telegram notification; every other
    job is told where to reply, because a scheduled prompt arrives with no
    Telegram metadata of its own."""
    if entry.get("source") != "scheduler":
        return entry["block"]
    meta = entry.get("meta") or {}
    directive = (_directive("silent") if meta.get("silent")
                 else _directive("reply").format(chat_id=entry.get("chat_id")))
    return directive + entry["block"]


def coalesce_blocks(group):
    """The prompt for one turn: the group's entries rendered and joined in
    order. A scheduler entry is always alone in its group (source-aware
    coalescing), so its directive never leaks onto a user message."""
    return "\n".join(render_entry(e) for e in group)


# --- session map + bootstrap (U7) -------------------------------------------
# The session id is stable per chat (U5), so the map is written only at
# bootstrap and rotation — never per turn. A missing OR corrupt map reads as
# empty, which sends the next turn down the bootstrap path (prime + fresh id).

def read_session_map():
    p = session_map_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        log(f"turn_runner: session map corrupt ({exc}) — bootstrapping fresh")
        return {}


def _write_session_map(m):
    tmp = session_map_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, session_map_path())


def get_session(chat_id):
    return read_session_map().get(str(chat_id))


def set_session(chat_id, session_id):
    if not session_id:
        return
    m = read_session_map()
    m[str(chat_id)] = session_id
    _write_session_map(m)


def drop_session(chat_id):
    m = read_session_map()
    if str(chat_id) in m:
        del m[str(chat_id)]
        _write_session_map(m)


# --- backend selection + billing leak detector (KTD11) ----------------------

def current_backend():
    """`A` (headless -p, default) or `B` (interactive session, U6f). Read from a
    flag file each turn so the supervisor or the leak detector can flip it live."""
    p = backend_flag_path()
    if p.exists():
        v = p.read_text(encoding="utf-8").strip().upper()
        if v in ("A", "B"):
            return v
    return "A"


def set_backend(backend, reason=""):
    backend = backend.upper()
    backend_flag_path().write_text(backend + "\n", encoding="utf-8")
    log(f"turn_runner: runner_backend -> {backend} ({reason})")


def _maybe_flip_backend(before, after, alert):
    """Billing-policy tripwire (runtime half): if a single turn moved the metered
    pool by more than LEAK_FLIP_DELTA, headless `-p` is billing API-rate. No
    reliable reading (None) means no signal, so we do not act on noise.

    ALERT-ONLY until U6f: backend B (the interactive-session driver) is not
    implemented yet, so we must NOT auto-`set_backend("B")` — selecting an
    unimplemented backend would make every subsequent turn fail. We alert the
    owner to intervene (investigate the billing policy; manually flip once B
    exists). When U6f lands backend B, restore the `set_backend("B")` flip
    here."""
    if before is None or after is None:
        return
    if after - before >= LEAK_FLIP_DELTA and current_backend() == "A":
        alert("⚠️ headless -p usage jumped +{:.3f} of the 5H pool in one turn — "
              "possible API-rate metering. The subscription-safe interactive "
              "backend (B) is not implemented yet (U6f); investigate the "
              "Agent-SDK billing policy: "
              "support.claude.com/en/articles/15036540".format(after - before))


# --- quota-headroom guard ---------------------------------------------------

def default_usage_probe():
    """Best-effort 5H-window utilization in [0,1], or None if unknown.

    The oauth/usage endpoint is rate-limited (U5), so instead of a fragile live
    call this reads an optional locally-cached reading. A sampler (or the
    statusline) may write `~/.claude/usage-cache.json` with a `five_hour_utilization`
    float and an epoch `ts`; a stale or missing file yields None, and the guard
    fail-opens (a bot that refuses to answer because it cannot read a probe is
    worse than one that might touch overflow credits). The control flow around
    this is fully tested via injection; the probe source is a documented seam."""
    try:
        cache = Path.home() / ".claude" / "usage-cache.json"
        data = json.loads(cache.read_text(encoding="utf-8"))
        util = float(data["five_hour_utilization"])
        return max(0.0, min(1.0, util))
    except Exception:
        return None


def quota_ok(probe=None):
    """(proceed, utilization). proceed is False only when we have a reading AND
    it is at/above the defer threshold."""
    probe = probe or default_usage_probe
    util = probe()
    if util is None:
        return True, None
    return util < quota_defer_at(), util


QUOTA_NOTICE_COOLDOWN = 1800     # seconds between "deferred" notices to the owner


def _quota_notice_stamp() -> Path:
    return gateway_state_dir() / "quota-notice-at"


def quota_notice_due(now=None, cooldown=QUOTA_NOTICE_COOLDOWN) -> bool:
    """Rate-limit the quota-deferral notice, and stamp it when it fires.

    "One notice per drain" was sufficient while the drain only ran on a real
    trigger. It stops being sufficient at the U11 cutover, where the supervisor
    also runs a safety-net drain every 30s: a quota-blocked backlog would then
    apologize to the owner 120 times an hour. The stamp lives in the gateway
    state dir (non-synced), so a lost stamp costs at most one extra notice."""
    now = time.time() if now is None else now
    p = _quota_notice_stamp()
    try:
        last = float(p.read_text(encoding="utf-8").strip())
    except Exception:
        last = 0.0
    if now - last < cooldown:
        return False
    try:
        p.write_text(str(now), encoding="utf-8")
    except OSError:
        pass                      # a stamp we cannot write just means more notices
    return True


# --- owner alert / deferral notice ------------------------------------------

def _default_alert(text):
    """Direct Bot API notice to the owner (not a Claude turn). Reuses the
    sanctioned send primitive, which journals and is owner-allowlisted."""
    try:
        sys.path.insert(0, str(LIBEXEC))
        import send
        send.reply(text)
    except Exception as exc:  # never let an alert failure break the drain
        log(f"turn_runner: owner alert failed: {exc}")


# --- turn execution seam ----------------------------------------------------

# Environment handed to a turn. Inheriting the whole shell env hands every turn
# whatever credentials happen to be exported — a real probe (2026-07-28) found
# live Gemini, Firecrawl, GitHub PAT, Notion, Exa, Parallel, Fireflies, Mochi
# and Intervals keys readable from inside a turn. The turn needs almost none of
# them, so pass an explicit minimal set plus a per-instance passthrough for the
# credentials that instance genuinely needs (an instance whose MCP servers need
# a key lists it in ESTATE_ENV_PASSTHROUGH, comma-separated).
# HOME is required for keychain OAuth (KTD1) and for --resume's project scoping;
# PATH for the node/claude launcher; the rest keep normal shell behaviour.
ENV_BASE_KEYS = ("HOME", "PATH", "USER", "SHELL", "LOGNAME", "LANG", "LC_ALL",
                 "TMPDIR", "TERM", "TZ", "SSH_AUTH_SOCK", "XDG_CONFIG_HOME")


def _instance_config_source(config=None):
    """Absolute path of the config this process is running as, or None.

    Prefers the loaded config's own `source` over the environment variable, so
    a caller that passed an explicit config (the parity suite, a scratch drill)
    hands the turn the same instance it is itself using rather than whatever
    the ambient variable happens to name."""
    src = getattr(config or cfg(), "source", None)
    if src:
        return Path(os.path.abspath(os.path.expanduser(str(src))))
    src = os.environ.get(instance_config.CONFIG_ENV)
    return Path(os.path.abspath(os.path.expanduser(src))) if src else None


def _child_env(config=None):
    """The sanitized environment for a turn subprocess.

    Passthrough keys come from the INSTANCE CONFIG first, then the environment.
    The config half is load-bearing rather than tidy: TELEGRAM_STATE_DIR is what
    points a turn's auto-loading telegram channel plugin at THIS instance's
    (deliberately blank) channel dir. Without it the plugin falls back to the
    default channel state dir, which on a multi-instance mac belongs to whichever
    instance was installed first and holds ITS token — so the turn would start
    polling the other bot's token and 409-war its live poller.
    """
    env = {k: v for k, v in os.environ.items() if k in ENV_BASE_KEYS}
    keys = list((config or cfg()).env_passthrough)
    keys += [k.strip() for k in os.environ.get("ESTATE_ENV_PASSTHROUGH", "").split(",")]
    for key in keys:
        if key and key in os.environ:
            env[key] = os.environ[key]
    # The retry-safety anchor must reach the turn (and the send primitive it
    # shells out to) — verified end-to-end 2026-07-28.
    tid = os.environ.get("ESTATE_TURN_ID")
    if tid:
        env["ESTATE_TURN_ID"] = tid
    # So must the instance's own identity. Everything the turn is told to run —
    # send.py above all — resolves its instance through instance_config.load(),
    # which has NO default by design: an instance that cannot identify itself
    # would inherit another instance's state directory and token. Without this
    # line that load raises ConfigError, and a turn physically cannot reply.
    #
    # Measured 2026-08-03: the child env was HOME, PATH, USER, SHELL, LOGNAME,
    # TMPDIR, TERM, SSH_AUTH_SOCK and nothing else, and `load()` under exactly
    # that environment fails. Live turns nevertheless sent, by a mechanism that
    # is not in this repo, the dotfiles, the plists, the --settings file, or the
    # shell snapshots — so the send path worked on this machine for a reason
    # nobody chose and no other machine can be assumed to reproduce. Passing it
    # explicitly is what makes the behaviour a property of the code.
    #
    # This is a path, not a secret, and it does not widen what the AGENT can
    # read: `Read(~/.config/**)` stays denied by the allowlist, so the model
    # cannot open the file. Only the estate's own helpers, which are ordinary
    # subprocesses rather than the Read tool, resolve through it.
    src = _instance_config_source(config)
    if src:
        env[instance_config.CONFIG_ENV] = str(src)
    # Never let an API key reach the turn: billing must stay on the keychain
    # OAuth subscription (KTD1).
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def _default_invoker(argv, timeout):
    """Run `claude -p ...` and return a CompletedProcess. Prompt is passed as
    argv (never stdin — the launchd env fix), cwd is the INSTANCE workdir so
    --resume stays project-scoped, and the child gets a sanitized environment
    carrying ESTATE_TURN_ID (set by _backend_a) but not the machine's
    unrelated credentials.

    stdin is EXPLICITLY /dev/null. Without it the CLI waits ~3s for piped stdin
    that is never coming ("no stdin data received in 3s, proceeding without
    it") — pure latency on every single turn, and a hang risk if the inherited
    stdin happens to be a pipe nobody closes."""
    return subprocess.run(argv, cwd=str(workdir()), capture_output=True,
                          text=True, timeout=timeout, env=_child_env(),
                          stdin=subprocess.DEVNULL)


def _backend_a(prompt, head, invoker, timeout, turn_id=None):
    """Headless `claude -p --resume`. Returns TurnResult. Parses --output-format
    json for the session id (stable per chat, U5) and the reply text. Exposes
    the turn id to the child via ESTATE_TURN_ID (restored after) so an outbound
    sent mid-turn is journaled with it — the retry-safety anchor (U8)."""
    argv = [CLAUDE_BIN, "-p", prompt, "--output-format", "json"]
    argv += _permission_args()
    argv += _mcp_args()
    if head:
        argv += ["--resume", head]
    old = os.environ.get("ESTATE_TURN_ID")
    if turn_id is not None:
        os.environ["ESTATE_TURN_ID"] = turn_id
    try:
        proc = invoker(argv, timeout)
    except subprocess.TimeoutExpired:
        return TurnResult(False, None, None, None, "timeout")
    except Exception as exc:
        return TurnResult(False, None, None, None, f"invoke error: {exc}")
    finally:
        if turn_id is not None:
            if old is None:
                os.environ.pop("ESTATE_TURN_ID", None)
            else:
                os.environ["ESTATE_TURN_ID"] = old
    if proc.returncode != 0:
        return TurnResult(False, None, None, proc.returncode,
                          (proc.stderr or "")[:300])
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return TurnResult(False, None, None, proc.returncode,
                          "non-JSON output from claude -p")
    new_head = data.get("session_id") or head
    return TurnResult(True, data.get("result"), new_head, 0, None)


def run_turn(prompt, head, *, backend=None, invoker=None,
             timeout=None, turn_id=None):
    """Execute one turn through the selected backend. Backend A is live; backend
    B (interactive session) lands in U6f — the seam and selector are here now."""
    # Resolved here rather than as a default argument: a default is evaluated at
    # import time, which would freeze one instance's timeout into the module for
    # every instance sharing the process.
    timeout = per_turn_timeout() if timeout is None else timeout
    backend = (backend or current_backend()).upper()
    invoker = invoker or _default_invoker
    if backend == "A":
        return _backend_a(prompt, head, invoker, timeout, turn_id)
    if backend == "B":
        raise NotImplementedError(
            "runner_backend=B (interactive-session driver) is implemented in U6f")
    raise ValueError(f"unknown runner_backend {backend!r}")


def _write_job_marker(group):
    """Stamp today's completion marker for a scheduler entry that just succeeded.

    The marker is a scheduled job's completion truth: `guard()` reads it to
    refuse a duplicate run, and reconciliation reads it to prove a job finished
    (the send journal cannot, since a job may legitimately send nothing).

    Written HERE — at the point completion is actually known — rather than in
    `run_job`, which returns as soon as the entry is enqueued and therefore
    cannot distinguish "done" from "another drain will do it later". That
    distinction is not academic: when a conversation is already draining,
    run_job returns `busy` and the in-flight drain does the work, so a marker
    written by run_job would be either a lie or absent depending on timing.

    Reading's digest jobs also write this marker from inside their R20 send
    transaction; re-writing the same path here is idempotent. second-instance's jobs
    reply through the plain send primitive and had no marker at all until this
    existed — observed live on 2026-08-02, when the 20:00 post-ride check
    delivered its brief and left no completion evidence behind.

    CALLED ONLY WHEN THE TURN PRODUCED SEND EVIDENCE. That condition is the
    whole point and is easy to drop by accident: `_owes_reply` deliberately
    exempts scheduled jobs from the send-evidence rule *because* the marker is
    their completion proof. Writing the marker on a bare exit-0 would make it
    exactly as trustworthy as the exit code — which KTD12 established is not
    trustworthy at all — and reconciliation would then confirm jobs that
    delivered nothing. A scheduled job that exits 0 silently gets no marker, so
    its guard lets the next fire retry it.
    """
    for entry in group:
        job = entry.get("job")
        if entry.get("source") != "scheduler" or not job:
            continue
        try:
            import guard as guard_mod
            path = guard_mod.marker_path(job, datetime.now().strftime("%Y-%m-%d"))
            if not path.exists():
                path.write_text(_now_iso() + "\n", encoding="utf-8")
                log(f"turn_runner: marked '{job}' complete ({path.name})")
        except Exception as exc:  # never let bookkeeping fail a delivered turn
            log(f"turn_runner: could not write marker for '{job}': {exc}")


def _job_marker_exists(job, when_iso=None):
    """Did a scheduled job already complete? The marker is its completion truth
    (the send primitive writes it), so it is the reconciliation evidence for a
    scheduler entry — the send journal is not, because a job may legitimately
    send nothing to chat."""
    if not job:
        return False
    try:
        import guard as guard_mod
        day = (when_iso or "")[:10] or datetime.now().strftime("%Y-%m-%d")
        return guard_mod.marker_path(job, day).exists()
    except Exception:
        return False


def reconcile_pending(output_check=None):
    """Settle entries whose turn already did its work but whose processed-mark
    never landed (crash/kill between the send and the WAL append).

    Evidence differs by source: a message turn proves itself with an outbound
    stamped with its turn_id; a scheduled job proves itself with its marker.
    Runs once at drain start, under the drain lock. Returns the recovered ids."""
    output_check = output_check or _default_output_check
    rows = read_jsonl(wal_path())
    inbound, processed, dead = _fold_rows(rows)
    done = processed | dead
    starts = {}
    for r in rows:
        if r.get("type") == "turn_started":
            for uid in r.get("update_ids") or []:
                starts.setdefault(uid, []).append(r.get("turn_id"))
    recovered = []
    for e in inbound:
        uid = e.get("update_id")
        if uid in done:
            continue
        if e.get("source") == "scheduler":
            if _job_marker_exists(e.get("job"), e.get("ts")):
                recovered.append(uid)
            continue
        if any(output_check(tid) for tid in starts.get(uid, [])):
            recovered.append(uid)
    if recovered:
        mark_processed(recovered, session=None)
        log(f"turn_runner: reconciled {recovered} — their turn had already "
            f"delivered before the crash; not replaying")
    return recovered


def _owes_reply(group):
    """Does this turn-group owe the owner an outbound message?

    A user message always does — a turn that answers nothing is indistinguishable
    from a lost message. A scheduler entry marked `silent` (the 03:00 handoff
    flush) owes nothing by design, and a non-silent scheduled job proves
    completion through its marker rather than a chat reply, so neither is held
    to the send-evidence rule here."""
    return all(e.get("source") != "scheduler" for e in group)


def _default_output_check(turn_id):
    """Retry-safety (U8): did this turn already produce an outbound? A reply
    stamps turn_id into the msg-index journal (send.reply reads
    ESTATE_TURN_ID). If a raw record with this turn_id exists, the turn sent
    before it died — re-invoking would double-send, so treat it as processed."""
    if not turn_id:
        return False
    try:
        rows = read_jsonl(msg_index.index_path())
    except Exception as exc:  # noqa: BLE001
        # A corrupt journal line must not crash the whole drain (the session
        # map and quota probe fail safe the same way). Fail OPEN — "no evidence
        # found" — so the turn is retried rather than silently accepted; the
        # retry is bounded and ends in a dead-letter + alert.
        log(f"turn_runner: send-journal unreadable ({exc}) — assuming no output")
        return False
    for row in rows:
        if row.get("type") == "raw" and row.get("turn_id") == turn_id:
            return True
    return False


# --- the drain --------------------------------------------------------------

def _acquire_lock():
    """Non-blocking flock. Returns the held fd, or None if a drain is already
    running (the trigger that lost the race just exits — a running drain re-folds
    the WAL after every turn, so it will pick up whatever this trigger saw)."""
    fd = open(lock_path(), "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return None
    return fd


def _process_group(group, *, invoker, alert, probe, output_check):
    """Run one turn-group to a terminal state (processed or dead-lettered).
    Returns "processed" | "dead_letter"."""
    uids = [e["update_id"] for e in group]
    chat_id = group[0].get("chat_id")
    # One turn id for this group, stable across retries — it is the anchor the
    # retry-safety check reads back from the send journal (U8).
    turn_id = uuid.uuid4().hex
    attempt = 0
    while True:
        before = probe() if probe else None
        session = get_session(chat_id)
        # Bootstrap: no session yet (fresh start or post-rotation) -> run a fresh
        # turn (no --resume) primed to read the handoff file. The id is stable
        # per chat (U5), so once captured the map is not rewritten per turn.
        prompt = coalesce_blocks(group)
        if session is None:
            prompt = prime_prefix() + prompt
        # Anchor the turn BEFORE invoking: if we die after the send but before
        # the processed-mark, reconciliation uses this record to find the
        # turn_id and prove the reply already went out (no double-send).
        if attempt == 0:
            journal_turn_started(turn_id, uids, job=group[0].get("job"))
        # Scheduled work gets the longer budget (see per_turn_timeout()).
        timeout = (job_turn_timeout() if group[0].get("source") == "scheduler"
                   else per_turn_timeout())
        try:
            result = run_turn(prompt, session, invoker=invoker, turn_id=turn_id,
                              timeout=timeout)
        except Exception as exc:  # e.g. a stray backend flag -> NotImplementedError
            # Never let a backend error crash the drain: treat it as a turn
            # failure so it retries/dead-letters + alerts instead of wedging the
            # whole gateway (an uncaught raise would strand every message).
            result = TurnResult(False, None, None, None, f"backend error: {exc}")
        if result.ok:
            if session is None:
                set_session(chat_id, result.new_head)
            after = probe() if probe else None
            _maybe_flip_backend(before, after, alert)
            # Exit status is NOT delivery truth (verified 2026-07-28): when a
            # tool is denied under dontAsk the turn still exits 0 and the model
            # merely narrates the denial. Marking such a turn processed would
            # lose the message silently — the user gets no reply and nothing
            # retries. So a turn that OWES a reply must show evidence of one:
            # an outbound stamped with this turn_id in the send journal (U8).
            # Silent jobs owe nothing; scheduled jobs prove completion with the
            # marker the send primitive writes, which is checked by their guard.
            sent = output_check(turn_id)
            if _owes_reply(group) and not sent:
                result = TurnResult(False, None, result.new_head, 0,
                                    "turn exited 0 but produced no outbound "
                                    "(tool denied, or the turn chose not to reply)")
                log(f"turn_runner: {uids} exited 0 with no send — treating as "
                    f"a failure so it retries instead of vanishing")
            else:
                mark_processed(uids, session=result.new_head)
                # Marker only on real send evidence — see _write_job_marker.
                if sent:
                    _write_job_marker(group)
                log(f"turn_runner: turn ok, processed {uids} "
                    f"(session {result.new_head})")
                return "processed"
        attempt += 1
        log(f"turn_runner: turn failed ({result.error}) attempt {attempt}/"
            f"{max_retries() + 1} for {uids}")
        if attempt > max_retries():
            for uid in uids:
                dead_letter(uid, result.error or "turn failed")
            alert(f"⚠️ a message could not be processed after {max_retries() + 1} "
                  f"attempts ({result.error}). Parked update_id(s) {uids} to the "
                  f"dead-letter file; following messages still answered.")
            return "dead_letter"
        # Retry-safety (plan U6 step 3): if the failed turn already produced an
        # outbound (it sent, then died before we saw success), do NOT re-invoke —
        # that would double-send. The concrete msg-index correlation is wired in
        # U8 (the send primitive stamps a turn id); here it is an injectable
        # predicate defaulting to "no output detected".
        if output_check and output_check(turn_id):
            log(f"turn_runner: {uids} already produced output — not re-invoking")
            mark_processed(uids, session=get_session(chat_id))
            return "processed"


def drain(*, invoker=None, alert=None, probe=None, output_check=None):
    """Drain the WAL under the per-session lock. One turn-group per iteration,
    re-folding after each so mid-turn arrivals are caught on this drain.

    Quota guard: at >=90% of the 5H window, scheduled turns are deferred (left
    pending — they self-heal when the window recovers) and ad-hoc messages get a
    short direct notice instead of a Claude turn, so automation never silently
    spills into paid overflow credits."""
    alert = alert or _default_alert
    output_check = output_check or _default_output_check
    # Resolve the probe ONCE and thread the same callable into both quota_ok and
    # _process_group. Without this, the CLI `drain()` path (probe=None) leaves
    # _process_group's before/after as None and the billing-leak detector never
    # fires in production — it only worked in tests that injected a probe.
    probe = probe or default_usage_probe
    held = turns_held()
    if held:
        # Everything stays pending — the WAL is exactly what makes a hold safe.
        log(f"turn_runner: turns held ({held}) — leaving "
            f"{len(pending_entries())} entr(ies) pending")
        return {"status": "held", "reason": held}
    lock_fd = _acquire_lock()
    if lock_fd is None:
        return {"status": "busy"}
    try:
        summary = {"status": "ok", "turns": 0, "dead_letter": 0,
                   "deferred": 0, "notified": 0, "reconciled": 0}

        # Crash recovery first: settle anything whose turn already delivered
        # before a crash, so it is never replayed into a second reply.
        summary["reconciled"] = len(reconcile_pending(output_check))

        proceed, util = quota_ok(probe)
        if not proceed:
            # One pass, no re-fold loop: under a quota block we do not want to
            # spin. Messages get a notice + are marked processed; scheduler
            # entries are left pending to self-heal.
            # Everything is LEFT PENDING under a quota block — messages too.
            # Marking a message processed after a best-effort notice loses it
            # outright when that notice fails to send (_default_alert swallows
            # send errors), which breaks at-least-once for the one thing the
            # WAL exists to protect. Pending entries self-heal: the next drain
            # after the window recovers answers them for real.
            groups = group_into_turns(pending_entries())
            summary["deferred"] = len(groups)
            if (any(g[0].get("source") != "scheduler" for g in groups)
                    and quota_notice_due()):
                # One notice per drain AND at most one per cooldown window — a
                # quota-blocked backlog must not turn into a burst of apologies,
                # and post-cutover the supervisor drains every 30s regardless of
                # whether anything arrived.
                alert("⏳ Отложено: 5-часовое окно лимита почти исчерпано, "
                      "держу сообщение до его сброса, чтобы не уходить в "
                      "платные кредиты. Отвечу, как только окно освободится.")
                summary["notified"] = 1
            log(f"turn_runner: quota {util:.0%} — {len(groups)} group(s) left "
                f"pending; they self-heal when the window recovers")
            summary["status"] = "quota_deferred"
            return summary

        while True:
            # Snapshot the WAL length BEFORE folding, so an append that lands
            # between the fold and the loop's exit check is detected rather than
            # stranded (see wal_size()). Re-snapshotted every iteration, so the
            # drain's own processed/turn_started appends never trip it.
            size_before = wal_size()
            entries = pending_entries()
            if not entries:
                if wal_size() != size_before:
                    # Work arrived while we were folding, and any trigger it
                    # fired bounced off our lock. Fold once more rather than
                    # leaving it for the next unrelated wakeup.
                    continue
                break
            group = group_into_turns(entries)[0]
            outcome = _process_group(
                group, invoker=invoker, alert=alert, probe=probe,
                output_check=output_check)
            summary["turns"] += 1
            if outcome == "dead_letter":
                summary["dead_letter"] += 1
        return summary
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


# --- scheduled jobs through the gateway (U9) --------------------------------

def enqueue_job(job, prompt, chat_id=None, not_before=None, silent=False,
                guard_fn=None, lease_writer=None):
    """Guard-then-enqueue: the gateway replacement for `inject.sh --job`.

    Guard semantics are preserved EXACTLY (they are the idempotency contract the
    launchd plists depend on): 0 = proceed, 3 = marker exists (already sent
    today), 4 = a fresh lease means a send is in flight, 5 = before the job's
    validity window (launchd wake catch-up past midnight must not burn the new
    day's marker). A stale lease does not block — that is the resume path.

    On a passing guard we write the lease and append a synthetic scheduler entry
    to the WAL. The entry gets a dedicated turn (U6 source-aware coalescing), so
    its exit status stays attributable to its job. The `-p` exit status replaces
    inject.sh's 30x30s marker poll, but the MARKER remains the completion truth
    — the send primitive writes it, so "the turn exited 0" never counts as
    delivered on its own.

    Returns the guard exit code (0 when enqueued)."""
    import guard as guard_mod  # lazy: pulls requests/msg_index
    guard_fn = guard_fn or guard_mod.guard
    rc = guard_fn(job, not_before=not_before)
    if rc != guard_mod.GUARD_OK:
        log(f"turn_runner: job '{job}' not enqueued (guard rc={rc})")
        return rc
    # Lease BEFORE journaling so a second guard during the turn sees it and
    # no-ops (mirrors inject.sh's ordering).
    if lease_writer is None:
        def lease_writer(j):
            guard_mod.lease_path(j).write_text(
                datetime.now().isoformat(timespec="seconds") + "\n")
    lease_writer(job)
    if chat_id is None:
        chat_id = cfg().owner_chat_id
    uid = next_scheduler_update_id()
    journal_inbound(uid, "scheduler", chat_id, prompt, job=job,
                    meta={"silent": bool(silent)})
    log(f"turn_runner: enqueued job '{job}' as update_id={uid} "
        f"(silent={bool(silent)})")
    return 0


def run_job(job, prompt, chat_id=None, not_before=None, silent=False, **kw):
    """enqueue_job + drain — the entry point every launchd-fired scheduled job
    calls (morning.sh / evening.sh / weekly.sh, since the U11 cutover).

    Draining synchronously is deliberate: the launchd job's own lifetime should
    span the work, so a failure is attributable to that fire. If another drain
    already holds the lock this one returns "busy" — harmless, because the
    running drain re-folds and will pick the job's entry up in the same pass."""
    rc = enqueue_job(job, prompt, chat_id=chat_id, not_before=not_before,
                     silent=silent, **kw)
    if rc != 0:
        return rc
    summary = drain()
    log(f"turn_runner: run_job '{job}' drain {summary}")
    return 0


# --- rotation + compaction (U7) ---------------------------------------------

def _handoff_stamp():
    """(mtime_ns, size) of the handoff file, or None if it is missing. Both,
    because a same-second rewrite of identical length is exactly what a
    "distilled the day into it" edit is not."""
    try:
        st = handoff_path().stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _flag_flush_failure():
    """Surface a silent flush failure the way this repo already surfaces the
    03:00 housekeeping one: a flag file the brain reads and mentions in the next
    brief. Deliberately NOT a Telegram alert — rotation runs at 03:00 and a push
    notification then buys nothing over a mention six hours later."""
    try:
        flag = cfg().agent_state_dir / "rotation-flush-failed"
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(
            f"{_now_iso()} rotation flush exited 0 but did not modify "
            f"{handoff_path().name} — that night's context was not persisted. "
            f"Check daemon.log and daemon/permissions.json (a denied Edit still "
            f"exits 0).\n", encoding="utf-8")
    except OSError as exc:  # never let flagging break the rotation
        log(f"turn_runner: could not write the flush-failure flag: {exc}")


# --- handoff pruning ---------------------------------------------------------
#
# The handoff file is the whole design's load-bearing assumption: "context lives
# on disk, not in a process". Nothing was bounding it. Each flush PREPENDS a
# fresh `## Current state (as of ...)` block and never retires the previous one,
# so by 2026-07-31 the file held 60 stacked snapshots — 202 KB of superseded
# "current" state going back six weeks, under a 278 KB total.
#
# Three costs, ascending: every flush turn re-reads it; it is a large slice of
# per-turn latency; and past a certain size it stops being readable in one call,
# at which point recovery silently degrades and nothing reports it.
#
# Pruning is MECHANICAL rather than prompt-driven on purpose. The brain already
# knew: a 2026-07-23 flush wrote "handoff is 2057+ lines / ~193KB, pre-Jul-1
# blocks safe to prune (left to housekeeping)" — and then it grew another 85 KB,
# because "housekeeping" had no such job. An instruction a model already follows
# in prose and cannot act on is not a control.
#
# Nothing is deleted. Dropped content is appended to state/handoff-archive/
# first, and the live file is only rewritten once that archive is on disk.
#
# THE FILE OSCILLATES; MEASURE THE PEAK, NOT THE TROUGH. The 03:00 prune drops it
# to the retained blocks, then two flushes a day grow it back ~20 KB before the
# next one. The first live run went 84,089 → 63,945 B and was back to 81,204 by
# 09:06 — so a size check that runs only just after pruning samples the one
# moment the file is smallest by construction, and can fire only if the retained
# blocks ALONE bust the budget. That is a real question, and `over_budget` still
# asks it. It is just not the question that predicts silent degradation.
#
# That one is a LINE count. A single Read returns 2000 lines by default, so the
# 2790-line file recovery was reading in July lost its last 790 lines with no
# error — the exact failure this whole section exists to prevent, measured in the
# wrong unit. HANDOFF_PEAK_LINES watches the pre-prune high-water mark against
# that ceiling with room to act before truncation starts.

HANDOFF_KEEP_SNAPSHOTS = 4        # "## Current state" blocks; ~2 days at 2/day
HANDOFF_LOG_TAIL_BYTES = 25_000   # per append-log section ("## Log", "## Service log")
HANDOFF_SOFT_MAX = 80_000         # retained blocks alone bust budget (trough check)
HANDOFF_PEAK_LINES = 1_400        # daily high-water mark vs the 2000-line Read limit

_SNAPSHOT_RE = re.compile(r"^##\s+(?:Current|Earlier)\s+state\b", re.I)
_LOG_SECTION_RE = re.compile(r"^##\s+(?:Service\s+log|Log)\s*$", re.I)
_AS_OF_RE = re.compile(r"as of\s+(\d{4}-\d{2}-\d{2})")


def _split_sections(text):
    """(preamble_lines, [section_lines, ...]) split on `## ` headers. Keeps line
    endings so a rebuild is byte-exact for anything untouched."""
    preamble, sections, cur = [], [], None
    for ln in text.splitlines(keepends=True):
        if ln.startswith("## "):
            cur = [ln]
            sections.append(cur)
        elif cur is None:
            preamble.append(ln)
        else:
            cur.append(ln)
    return preamble, sections


def _tail_by_bytes(body, budget):
    """Last whole lines of `body` fitting in `budget` bytes, oldest-first order
    preserved. Returns (kept, dropped)."""
    kept, total = [], 0
    for ln in reversed(body):
        n = len(ln.encode("utf-8"))
        if total + n > budget:
            break
        kept.append(ln)
        total += n
    kept.reverse()
    return kept, body[:len(body) - len(kept)]


def _annotate_budget(summary, *, peak_lines, kept_lines):
    """Attach both budget verdicts to a prune summary.

    Applied to EVERY return path, `noop` included. It used to live at the tail of
    the success path, after `if not archived: return {...noop...}` — so the single
    most alarming state the file can be in, huge but with nothing prunable (four
    enormous snapshots, log tails already under budget), returned `noop` and was
    the one case rotate() logged nothing at all for. A budget check that skips the
    unprunable file checks only the files that were about to shrink anyway.
    """
    summary["before_lines"] = peak_lines
    summary["after_lines"] = kept_lines
    if summary.get("after", 0) > HANDOFF_SOFT_MAX:
        summary["over_budget"] = True
    if peak_lines > HANDOFF_PEAK_LINES:
        summary["peak_over_lines"] = peak_lines
    return summary


def prune_handoff(path=None, *, keep_snapshots=None, log_tail_bytes=None,
                  archive_dir=None, dry_run=False, config=None,
                  snapshot_pattern=None, order=None):
    """Retire superseded state snapshots and over-long log tails to an archive
    file, leaving the live handoff small enough to read in one call.

    WHAT COUNTS AS A SNAPSHOT IS PER INSTANCE. The agent writes its handoff in
    its own idiom and the two instances disagree entirely — reading prepends
    `## Current state (as of 2026-08-02 ...)`, second-instance writes
    `## Чт 23.07 — утренний бриф ...`. A hardcoded pattern no-ops silently on
    the other shape, which is exactly what happened: second-instance's handoff reached
    2366 lines, past the 2000-line single-Read ceiling, so every recovery was
    reading a truncated head while each nightly prune reported a clean `noop`.

    Ordering matters at the call site: prune BEFORE the flush turn, so the turn
    reads a compact file — and capture the flush's `before` stamp AFTER, or the
    prune's own write is indistinguishable from the flush's and rotate() reports
    flushed=True for a turn that wrote nothing.
    """
    try:
        c = config or cfg()
    except instance_config.ConfigError:
        c = None                       # explicit-path callers need no instance
    path = Path(path) if path else handoff_path(c)
    keep = HANDOFF_KEEP_SNAPSHOTS if keep_snapshots is None else keep_snapshots
    tail_budget = HANDOFF_LOG_TAIL_BYTES if log_tail_bytes is None else log_tail_bytes
    snap_re = re.compile(snapshot_pattern or (
        c.handoff_snapshot_pattern if c else r"^##\s+(?:Current|Earlier)\s+state\b"))
    order = order or (c.handoff_order if c else "date")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"status": "missing", "error": str(exc)}

    before = len(text.encode("utf-8"))
    before_lines = len(text.splitlines())
    preamble, sections = _split_sections(text)

    # Rank the retirable sections. `date` reads the header's own `as of` date
    # rather than trusting file order, so a hand-edit that moves a block cannot
    # silently retire the newest state; undated headers are never dropped.
    # `file` trusts newest-first file order, which is the only option for a
    # handoff whose headers carry no parseable year (second-instance's `## Чт 23.07 —`).
    ranked = []
    for i, sec in enumerate(sections):
        if not snap_re.match(sec[0]):
            continue
        if order == "file":
            ranked.append((-i, -i, i))          # earlier in file == newer
        else:
            m = _AS_OF_RE.search(sec[0])
            if m:
                ranked.append((m.group(1), -i, i))
    ranked.sort(reverse=True)
    doomed = {i for _, _, i in ranked[keep:]}

    kept_lines, archived = list(preamble), []
    for i, sec in enumerate(sections):
        if i in doomed:
            archived.extend(sec)
            continue
        if _LOG_SECTION_RE.match(sec[0]):
            head, body = sec[:1], sec[1:]
            body_kept, body_dropped = _tail_by_bytes(body, tail_budget)
            if body_dropped:
                archived.extend(head)
                archived.extend(body_dropped)
            kept_lines.extend(head + body_kept)
            continue
        kept_lines.extend(sec)

    if not archived:
        return _annotate_budget(
            {"status": "noop", "before": before, "after": before,
             "snapshots_dropped": 0},
            peak_lines=before_lines, kept_lines=before_lines)

    new_text = "".join(kept_lines)
    after = len(new_text.encode("utf-8"))
    summary = _annotate_budget(
        {"status": "pruned", "before": before, "after": after,
         "snapshots_dropped": len(doomed), "archived_bytes": before - after},
        peak_lines=before_lines, kept_lines=len(new_text.splitlines()))
    if dry_run:
        summary["status"] = "dry-run"
        return summary

    adir = Path(archive_dir) if archive_dir else path.parent / "handoff-archive"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m")
    dest = adir / f"{path.stem}-archive-{stamp}.md"
    try:
        # Archive first, durably. If this half fails the live file is untouched,
        # which is the only acceptable direction to fail in.
        adir.mkdir(parents=True, exist_ok=True)
        with open(dest, "a", encoding="utf-8") as fh:
            fh.write(f"\n<!-- pruned {_now_iso()} from {path.name} -->\n")
            fh.writelines(archived)
            fh.flush()
            os.fsync(fh.fileno())

        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new_text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        return _annotate_budget(
            {"status": "error", "error": str(exc), "before": before,
             "after": before, "snapshots_dropped": 0},
            peak_lines=before_lines, kept_lines=before_lines)

    summary["archive"] = str(dest)
    return summary


def rotate(chat_id, *, invoker=None):
    """Daily rotation for one chat: fire a silent handoff-flush turn on the
    current session, then drop the id so the next turn re-bootstraps fresh. This
    bounds transcript growth to one day and doubles as the recovery path for a
    lost/corrupt session. Runs under the same lock as the drain so it never
    races a turn, and compacts the WAL while the lock is held.

    Called by daemon.sh's 03:00 housekeeping window (since the U11 cutover),
    where it replaced the old inject'd handoff flush plus a claude restart.

    FLUSH TRUTH IS THE FILE, NOT THE EXIT CODE. Same lesson as delivery truth
    (KTD12): a turn whose Edit was denied still exits 0 while the model narrates
    the refusal. A conversational turn is caught by its missing send-evidence,
    but the flush is deliberately silent, so the only evidence it can leave is
    the handoff file itself. Found by the U11 rotation drill, where rotate()
    reported flushed=True after a turn that wrote nothing — in production that
    would silently drop a day of context every night with no signal at all."""
    lock_fd = _acquire_lock()
    if lock_fd is None:
        log("turn_runner: rotate skipped — a drain holds the lock")
        return {"status": "busy"}
    try:
        pruned = prune_handoff()
        if pruned["status"] == "pruned":
            log(f"turn_runner: pruned handoff {pruned['before']}→{pruned['after']} B "
                f"({pruned['before_lines']}→{pruned['after_lines']} lines), "
                f"{pruned['snapshots_dropped']} superseded snapshots → {pruned['archive']}")
        elif pruned["status"] == "error":
            log(f"turn_runner: handoff prune failed ({pruned['error']}) — file left intact")

        # Outside the status branches on purpose: the file being too big is worth
        # saying whether or not this run had anything to archive, and `noop` is
        # the status the worst case wears.
        if pruned.get("over_budget"):
            log(f"turn_runner: handoff still {pruned['after']} B after pruning "
                f"(soft max {HANDOFF_SOFT_MAX}) — the retained blocks are the bloat; "
                f"lower HANDOFF_KEEP_SNAPSHOTS or tighten the flush prompt")
        if pruned.get("peak_over_lines"):
            log(f"turn_runner: handoff peaked at {pruned['peak_over_lines']} lines "
                f"before pruning (ceiling {HANDOFF_PEAK_LINES}, single-Read limit "
                f"2000) — a session recovering near the peak reads a truncated head "
                f"and reports nothing; grow the prune, not the ceiling")

        sid = get_session(chat_id)
        flushed = False
        if sid:
            # AFTER the prune, deliberately: prune_handoff() rewrites the same
            # file, so a stamp taken before it would make every flush look
            # successful — the exact false positive the U11 drill was added for.
            before = _handoff_stamp()
            try:
                result = run_turn(flush_prompt(), sid, invoker=invoker)
            except Exception as exc:
                result = TurnResult(False, None, None, None, f"backend error: {exc}")
            flushed = bool(result.ok) and _handoff_stamp() != before
            if not result.ok:
                log(f"turn_runner: rotation flush failed ({result.error}) — "
                    f"dropping anyway; the handoff keeps its last good state")
            elif not flushed:
                log(f"turn_runner: rotation flush exited 0 but left "
                    f"{handoff_path().name} untouched — a denied tool, or the turn "
                    f"judged there was nothing to add. Flagging for triage.")
                _flag_flush_failure()
        drop_session(chat_id)
        comp = compact_wal()
        log(f"turn_runner: rotated chat {chat_id} (flushed={flushed}), "
            f"compacted WAL {comp}, handoff {pruned['status']}")
        return {"status": "ok", "flushed": flushed, "compaction": comp,
                "handoff": pruned}
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def compact_wal():
    """Rewrite the WAL keeping only still-pending inbound records; drop processed
    inbound and every terminal record. Held under an exclusive flock on the WAL
    so a concurrent poller append serializes behind it: append_jsonl re-checks
    the inode after acquiring the lock and retries on the new file, so nothing is
    lost across the os.replace.

    Safe because a processed update_id is never re-delivered — the Telegram poll
    offset has advanced past it — so discarding its journal record cannot
    resurrect it as pending."""
    p = wal_path()
    if not p.exists():
        return {"kept": 0, "dropped": 0}
    # Retry loop mirrors append_jsonl: after taking the lock, verify the fd
    # still points at the file the path names. A concurrent compaction (the
    # `compact` CLI racing the nightly rotate) replaces the inode, and holding a
    # lock on the orphaned one protects nothing — an append landing on the new
    # inode would be silently dropped by our os.replace.
    for _ in range(5):
        fd = os.open(str(p), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                same = os.fstat(fd).st_ino == os.stat(p).st_ino
            except FileNotFoundError:
                same = False
            if not same:
                continue                      # someone replaced it; re-open
            rows = read_jsonl(p)
            inbound, processed, dead = _fold_rows(rows)
            done = processed | dead
            keep = [r for r in inbound if r.get("update_id") not in done]
            keep_uids = {r.get("update_id") for r in keep}
            # Retain the turn_started records of still-pending entries: they are
            # the crash-recovery anchor, and dropping them would make a
            # post-compaction crash unrecoverable (double-send).
            keep_starts = [r for r in rows if r.get("type") == "turn_started"
                           and any(u in keep_uids
                                   for u in (r.get("update_ids") or []))]
            # Collapse every discarded `processed` record into one watermark so
            # dedup survives compaction (Telegram can re-deliver an update whose
            # offset was not yet confirmed).
            #
            # ONLY IDS TELEGRAM ACTUALLY DELIVERED MAY RAISE THE WATERMARK.
            # "positive" is not a sufficient test, and trusting it took both
            # instances down on 2026-08-03: scheduler ids are negative *today*
            # (see next_scheduler_update_id) but an older scheme allocated them
            # as 999000000+seq, and those legacy `processed` rows were still in
            # both WALs. The first compaction after the scheme change folded
            # them in, parking the watermark ~620M above the bot's real update_id
            # range — after which _fold_rows marked every genuine message as
            # already-done. Silent, total, and invisible to every health probe:
            # poller alive, backlog zero, scheduled jobs unaffected (their
            # negative ids are exempt from the fold), drains all reporting
            # turns: 0. Restricting the source set to ids that arrived as real
            # inbound messages closes the whole class, including the next
            # synthetic-id scheme nobody has invented yet.
            real_ids = {r.get("update_id") for r in rows
                        if r.get("type") == "inbound"
                        and r.get("source") == "message"
                        and isinstance(r.get("update_id"), int)
                        and r.get("update_id") > 0}
            pos_done = [u for u in done if u in real_ids]
            # `prior` needs no such filter: a watermark can only ever have been
            # raised by this rule, and when pos_done is empty hi degenerates to
            # prior, which is the correct no-op.
            prior = [r.get("max_processed_update_id") for r in rows
                     if r.get("type") == "watermark"
                     and isinstance(r.get("max_processed_update_id"), int)]
            hi = max(pos_done + prior) if (pos_done or prior) else None
            out = list(keep_starts) + keep
            if hi is not None:
                out.insert(0, {"type": "watermark", "ts": _now_iso(),
                               "max_processed_update_id": hi})
            tmp = p.with_suffix(".jsonl.compact")
            with open(tmp, "w", encoding="utf-8") as f:
                for r in out:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
            return {"kept": len(keep), "dropped": len(rows) - len(out),
                    "watermark": hi}
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    raise OSError("compact_wal: the WAL kept being replaced under us")


# --- CLI --------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="U6/U7 turn runner (see docstring)")
    # Same purpose as the poller's: both instances run THIS file, so a
    # supervisor checking `pgrep -f "turn_runner.py drain"` for its own drain
    # would see its neighbour's and skip — quietly, every tick, for as long as
    # the other instance stays busy. The tag makes the probe instance-specific.
    parser.add_argument("--instance", default=None, metavar="NAME",
                        help="instance name; must match the loaded config")
    parser.add_argument("--config", default=None, metavar="PATH",
                        help=f"instance YAML (default: ${instance_config.CONFIG_ENV})")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("drain", help="drain the inbound WAL under the session lock")
    p_rot = sub.add_parser(
        "rotate", help="daily rotation: flush handoff + drop session + compact")
    p_rot.add_argument("--chat-id", type=int, required=True)
    sub.add_parser("compact", help="compact the inbound WAL (drop processed records)")
    p_stuck = sub.add_parser(
        "stuck-check",
        help="report inbound entries the WAL shows no attempt against")
    p_stuck.add_argument("--max-age", type=int, default=UNANSWERED_MAX_AGE_S,
                         metavar="SECONDS",
                         help=f"age before an unattempted entry counts as stuck "
                              f"(default {UNANSWERED_MAX_AGE_S})")
    p_prune = sub.add_parser(
        "prune-handoff", help="archive superseded handoff snapshots + log tails")
    p_prune.add_argument("--dry-run", action="store_true",
                         help="report what would be archived, write nothing")
    p_prune.add_argument("--keep", type=int, default=None, metavar="N",
                         help=f"state snapshots to retain (default {HANDOFF_KEEP_SNAPSHOTS})")
    p_enq = sub.add_parser("enqueue", help="journal a synthetic inbound entry")
    p_enq.add_argument("--update-id", type=int, required=True)
    p_enq.add_argument("--source", default="scheduler")
    p_enq.add_argument("--chat-id", type=int, required=True)
    p_enq.add_argument("--job", default=None)
    p_enq.add_argument("--block", required=True)

    # U9: the gateway replacement for `inject.sh --job` (guard exit codes are
    # the contract the plists read: 0 ok, 3 marker, 4 fresh lease, 5 too early).
    for name, helptext in (("enqueue-job", "guard then enqueue a scheduled job"),
                           ("run-job", "guard, enqueue, then drain (post-cutover)")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--job", required=True)
        p.add_argument("--prompt", required=True)
        p.add_argument("--chat-id", type=int, default=None)
        p.add_argument("--not-before", default=None, metavar="HH:MM")
        p.add_argument("--silent", action="store_true")

    args = parser.parse_args(argv)
    if args.config:
        os.environ[instance_config.CONFIG_ENV] = str(args.config)
    try:
        c = cfg()
    except instance_config.ConfigError as exc:
        log(f"turn_runner: {exc}", err=True)
        return 2
    if args.instance and args.instance != c.name:
        log(f"turn_runner: --instance={args.instance} does not match the loaded "
            f"config ({c.name} from {c.source}) — refusing to run under a "
            f"confused identity", err=True)
        return 2

    if args.command == "drain":
        summary = drain()
        log(f"turn_runner: drain {summary}")
        return 0
    if args.command == "rotate":
        summary = rotate(args.chat_id)
        log(f"turn_runner: rotate {summary}")
        return 0
    if args.command == "compact":
        # Take the drain lock like rotate() does, so a manual compaction cannot
        # race the nightly one (or a live drain) over the WAL inode.
        lock_fd = _acquire_lock()
        if lock_fd is None:
            log("turn_runner: compact skipped — a drain/rotate holds the lock")
            return 0
        try:
            summary = compact_wal()
            log(f"turn_runner: compact {summary}")
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        return 0
    if args.command == "stuck-check":
        # Deliberately lock-free and read-only: this must stay diagnosable while
        # a drain holds the lock, since "the drain is wedged" is one of the
        # things it exists to report. Exit 0 clean / 1 stuck, so a shell caller
        # can branch without parsing.
        stuck = unanswered_entries(args.max_age)
        if not stuck:
            print("stuck-check: clean")
            return 0
        oldest = stuck[0]
        print(f"stuck-check: {len(stuck)} entry(s) with no turn attempted; "
              f"oldest update_id={oldest.get('update_id')} ts={oldest.get('ts')}")
        return 1
    if args.command == "prune-handoff":
        # Under the drain lock: a live turn may be mid-Edit on this same file.
        lock_fd = _acquire_lock()
        if lock_fd is None:
            log("turn_runner: prune-handoff skipped — a drain/rotate holds the lock")
            return 0
        try:
            summary = prune_handoff(keep_snapshots=args.keep, dry_run=args.dry_run)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        log(f"turn_runner: prune-handoff {summary}")
        return 0 if summary["status"] != "error" else 1
    if args.command == "enqueue":
        journal_inbound(args.update_id, args.source, args.chat_id, args.block,
                        job=args.job)
        return 0
    if args.command in ("enqueue-job", "run-job"):
        fn = enqueue_job if args.command == "enqueue-job" else run_job
        return fn(args.job, args.prompt, chat_id=args.chat_id,
                  not_before=args.not_before, silent=args.silent)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
