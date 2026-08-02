#!/usr/bin/env python3
"""Shared plumbing for every instance: logging, state roots, JSONL journals,
sync-conflict detection, and the interactive-activity flag.

Transplanted from `first-instance/scripts/reader_client.py`, which was two
modules wearing one name: this generic half, and a Readwise Reader API client
bolted onto it. Only this half is instance-independent, so only this half moved.
The Readwise client stays in the reading repo, where it belongs — a plugin that
serves a second-instance coach has no business carrying a bookmarking API.

What changed in the move, all of it the same change: nothing resolves against a
module-level `REPO_ROOT` any more. Paths come from the instance config, because
under the plugin the file's own location no longer identifies the instance.

The JSONL primitives are copied VERBATIM. They look over-built and they are not:
each guard below is a bug that reached production once already.
"""
from __future__ import annotations

import fcntl
import glob
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import instance_config

CONFLICT_PATTERNS = ("*conflicted copy*", "* (Case Conflict)*")


def _cfg(cfg=None):
    return cfg if cfg is not None else instance_config.load()


def log(msg, err=False):
    """Timestamped line to the caller's stdout (the supervisor redirects it).

    `flush=True` is load-bearing, not decoration: output is redirected to a log
    file, so Python block-buffers it and lines sit unwritten for minutes. A log
    that appears late is worse than no log — it once made a healthy poller look
    dead during an incident.
    """
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}",
          file=sys.stderr if err else sys.stdout, flush=True)


# --- state roots -------------------------------------------------------------

def agent_state_dir(cfg=None):
    """Synced state the agent itself reads/writes (handoff, notes)."""
    d = _cfg(cfg).agent_state_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def gateway_state_dir(cfg=None):
    """Non-synced machine state (WAL, locks, session map)."""
    d = _cfg(cfg).gateway_state_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_secrets(path=None, cfg=None, keys=None):
    """KEY=VALUE lines from a chmod-600 env file; real env vars win.

    `path` defaults to the instance's `telegram.token_file`. Callers name the
    `keys` they want promoted from the real environment — the old version
    hardcoded a reading-specific list, which silently dropped any other
    instance's credentials.
    """
    src = Path(path) if path else _cfg(cfg).token_file
    secrets = {}
    if src.exists():
        for line in src.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            secrets[key.strip()] = value.strip().strip('"').strip("'")
    if keys:
        secrets.update({k: v for k, v in os.environ.items() if k in keys})
    return secrets


# --- sync-conflict guard ------------------------------------------------------

def find_conflicts(paths):
    """Dropbox/iCloud sync collisions: conflicted-copy siblings of state files."""
    hits = []
    for p in paths:
        p = Path(p)
        for pattern in CONFLICT_PATTERNS:
            hits.extend(glob.glob(str(p.parent / f"{p.stem}{pattern}")))
            hits.extend(glob.glob(str(p.parent / f"{p.stem} {pattern.lstrip('*')}")))
    return sorted(set(hits))


def check_no_conflicts(paths, job_name="job", cfg=None):
    """Called by scheduled jobs at startup. On hit: flag it, log loudly, exit 2.

    Fail INTO triage rather than proceeding: a conflicted copy means two writers
    disagreed about a state file, and guessing which one is current is how a
    day of state gets silently overwritten.
    """
    hits = find_conflicts(paths)
    if not hits:
        return
    flag = agent_state_dir(cfg) / "conflict-detected"
    with open(flag, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat()} {job_name}: {'; '.join(hits)}\n")
    log(f"CONFLICT: sync conflicted-copy state files detected by {job_name}: {hits}")
    log(f"Wrote {flag}; resolve manually before re-running.")
    sys.exit(2)


# --- interactive-activity flag -----------------------------------------------

def interactive_flag_path(cfg=None):
    return agent_state_dir(cfg) / "interactive-activity"


def refresh_interactive_flag(cfg=None):
    interactive_flag_path(cfg).touch()


def interactive_flag_fresh(ttl_seconds, cfg=None):
    """Is someone using the repo interactively right now?

    `ttl_seconds` is now a REQUIRED argument. It used to default to a lookup
    into the reading instance's `config.yaml` (`triage.interactive_flag_ttl_
    seconds`) — a config shape no other instance has, so the default would have
    raised KeyError on the second instance's first batch job.
    """
    flag = interactive_flag_path(cfg)
    if not flag.exists():
        return False
    return (time.time() - flag.stat().st_mtime) < ttl_seconds


def wait_while_interactive(ttl_seconds, poll=30, max_wait=1800, cfg=None):
    """Batch writers call this before request bursts; yields to live use."""
    waited = 0
    while interactive_flag_fresh(ttl_seconds, cfg) and waited < max_wait:
        log("interactive-activity flag fresh — batch job yielding")
        time.sleep(poll)
        waited += poll


# --- JSONL ledgers/journals ---------------------------------------------------

def append_jsonl(path, obj):
    """Append one JSON line under flock with O_APPEND (single atomic write).

    After acquiring the lock, re-verify the fd still points at the file the path
    names: a compactor (temp + os.replace) can swap the inode while we were
    blocked on flock, and an append to the orphaned old inode would be silently
    lost. On mismatch, reopen and retry.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    for _ in range(5):
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                same = os.fstat(fd).st_ino == os.stat(path).st_ino
            except FileNotFoundError:
                same = False
            if same:
                os.write(fd, line.encode("utf-8"))
                return
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    raise OSError(f"append_jsonl: file at {path} kept being replaced under us")


def read_jsonl(path):
    """Read all rows; tolerate exactly one trailing partial line (crash mid-write).

    Only the LAST line may be partial — that is the one a crash can truncate. A
    bad line anywhere else means real corruption and must not be swallowed.
    """
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                log(f"read_jsonl: tolerating partial trailing line in {path}")
                continue
            raise
    return rows
