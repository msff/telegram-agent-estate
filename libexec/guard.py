#!/usr/bin/env python3
"""Scheduling safety: markers, leases, payload persistence, and the pre-run guard.

Extracted from the combined send/schedule module the first instance grew, which
mixed three separable things: this scheduling-safety layer (generic), the
outbound send primitive (generic — now `send.py`), and that instance's own
digest transaction, with its API-side tag swaps and deep links
(instance-specific, stays put).

The three state roots — markers, leases, payloads — come from the instance
config. Under separate checkouts they were distinct because `state_dir()`
resolved against each repo; under the plugin they would collide, and a collision
here is not a crash. It is instance A reading instance B's marker for the same
job name ("morning-digest" is not an unusual name) and concluding its OWN work
was already done. The bot then goes quiet for a day with every process healthy
and every log clean.
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import instance_config
import msg_index
from estate_client import agent_state_dir, log, read_jsonl

LEASE_STALE_MINUTES = 20

# guard() exit codes. Also the process exit status when run as a CLI, which is
# how a shell scheduler consumes it.
GUARD_OK = 0            # no marker today AND no fresh lease -> run
GUARD_MARKER = 3        # already sent today
GUARD_LEASE_FRESH = 4   # send in progress -> don't double-run
GUARD_TOO_EARLY = 5     # before the job's validity window (wake catch-up)


class SendError(Exception):
    """Telegram send failed: HTTP error or ok=false in the Bot API reply."""


# --- state paths -------------------------------------------------------------

def lease_path(job, cfg=None):
    d = agent_state_dir(cfg) / "leases"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{job}.lease"


def marker_path(job, date, cfg=None):
    d = agent_state_dir(cfg) / "markers"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{job}-{date}"


def payloads_dir(cfg=None):
    d = agent_state_dir(cfg) / "payloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write_json(path, obj):
    """Temp file in the same dir + os.replace — readers never see a torn file."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --- payload persistence -----------------------------------------------------

def persist_payload(payload, cfg=None):
    """Write payloads/<payload_id>.json atomically. The SESSION calls this
    BEFORE any send (R20: persist precedes the first chunk) — resume must read
    composed content from disk, never recompose. Returns the path."""
    path = payloads_dir(cfg) / f"{payload['payload_id']}.json"
    _atomic_write_json(path, payload)
    log(f"guard: persisted payload {payload['payload_id']} "
        f"({len(payload.get('chunks') or [])} chunks) -> {path}")
    return path


def resolve_payload_path(payload_id=None, job=None, cfg=None):
    """payload_id -> its file; job -> today's newest payload file for the job."""
    d = payloads_dir(cfg)
    if payload_id:
        path = d / f"{payload_id}.json"
        return path if path.exists() else None
    today = datetime.now().strftime("%Y-%m-%d")
    candidates = sorted(d.glob(f"{globmod.escape(job)}-{today}-*.json"),
                        key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


# --- send journal (the msg index doubles as the R20 send journal) ------------

def sent_chunks(payload_id, path=None, cfg=None):
    """{chunk_idx: message_id} already sent for this payload, from raw journal
    records — the resume checkpoint AND the re-annotation source."""
    return {row["chunk_idx"]: row.get("message_id")
            for row in read_jsonl(msg_index.index_path(path, cfg))
            if row.get("type") == "raw"
            and row.get("payload_id") == payload_id
            and row.get("chunk_idx") is not None}


def sent_chunk_indices(payload_id, path=None, cfg=None):
    """Chunk indices already sent for this payload (compat wrapper)."""
    return set(sent_chunks(payload_id, path=path, cfg=cfg))


# --- the guard ---------------------------------------------------------------

def guard(job, lease_stale_minutes=LEASE_STALE_MINUTES, not_before=None, cfg=None):
    """Pre-run check: 0 = run (no marker today AND no fresh lease), 3 = marker
    exists (already sent), 4 = lease fresh (send in progress — don't double-run
    mid-compose), 5 = before `not_before` (HH:MM).

    A STALE lease does not block: that means a session died mid-compose, and the
    retry should re-run.

    `not_before` guards launchd wake catch-up: a StartCalendarInterval missed
    while the mac slept fires ONCE on wake — a 00:30 catch-up of the 20:30
    evening job would otherwise run immediately and write the NEW day's marker,
    making the real 20:30 fire that evening a no-op. A full day's rhythm
    silently skipped, with nothing in any log to say why.
    """
    if not_before:
        if datetime.now().strftime("%H:%M") < not_before:
            log(f"guard: before {not_before} — '{job}' wake catch-up "
                f"suppressed; the scheduled fire will run it")
            return GUARD_TOO_EARLY
    today = datetime.now().strftime("%Y-%m-%d")
    marker = marker_path(job, today, cfg)
    if marker.exists():
        log(f"guard: marker {marker.name} exists — '{job}' already sent today")
        return GUARD_MARKER
    lease = lease_path(job, cfg)
    if lease.exists():
        age = time.time() - lease.stat().st_mtime
        if age < lease_stale_minutes * 60:
            log(f"guard: lease for '{job}' is fresh ({age:.0f}s old) — "
                f"send in progress")
            return GUARD_LEASE_FRESH
        log(f"guard: lease for '{job}' is stale ({age:.0f}s old) — "
            f"run OK (resume path)")
    return GUARD_OK


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_guard = sub.add_parser("guard", help="pre-run check; exit code is the verdict")
    p_guard.add_argument("job")
    p_guard.add_argument("--not-before", default=None, metavar="HH:MM")
    p_guard.add_argument("--lease-stale-minutes", type=int,
                         default=LEASE_STALE_MINUTES)

    p_mark = sub.add_parser("mark", help="write today's completion marker")
    p_mark.add_argument("job")

    args = parser.parse_args(argv)
    if args.command == "guard":
        return guard(args.job, args.lease_stale_minutes, args.not_before)
    if args.command == "mark":
        path = marker_path(args.job, datetime.now().strftime("%Y-%m-%d"))
        path.write_text(f"{datetime.now().isoformat()}\n", encoding="utf-8")
        log(f"guard: marked {path.name}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
