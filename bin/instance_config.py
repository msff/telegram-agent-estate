#!/usr/bin/env python3
"""Instance configuration — the seam that makes one codebase serve many bots.

ONE plugin, MANY instances. Every string that differs between two bots on the
same mac is declared in an instance YAML and reaches the rest of `bin/` only
through this module. Nothing under `bin/` may hardcode a path, a chat id, a
label, or a process-match pattern.

WHY THIS IS THE DANGEROUS FILE. Before the plugin, two instances were two
separate checkouts, so their processes were distinguishable by path alone:
`pgrep -f first-instance/daemon/telegram_poll.py` could not possibly match
the second-instance bot. Under the plugin BOTH instances execute the same
`bin/poller.py`, so every sweep, liveness probe, and kill that matches on the
executable path now matches BOTH — a supervisor restarting its poller would
reap the other bot's. That is the R5/R18 failure class, and it is created by
consolidation itself: the plugin does not inherit the old isolation, it has to
manufacture it. `instance_tag()` and `poller_match()` below exist for exactly
that, and `install-instance.sh` refuses to install two instances sharing a name.

RESOLUTION IS THROUGH FUNCTIONS, NEVER MODULE CONSTANTS. A constant computed at
import time cannot follow an environment override, and the tests redirect state
via the environment. That is not a hypothetical: on 2026-07-31 a WORKDIR-derived
`HANDOFF_PATH` constant in the reading runner ignored the test suite's state-dir
override, and a plain `pytest tests/` pruned the LIVE production handoff file.
It was harmless only for as long as nothing wrote through that path. Read the
config through `load()`; do not cache it in a module global.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

# Points at the instance YAML. Set by the launchd plist and by supervisor.sh;
# tests and the parity drill set it to a temp file. There is deliberately NO
# fallback to a default location — an instance that cannot say who it is must
# fail loudly rather than silently adopt another instance's identity.
CONFIG_ENV = "ESTATE_INSTANCE_CONFIG"

REQUIRED = (
    "name", "label", "workdir", "python",
    "telegram.owner_chat_id", "telegram.token_file",
    "runtime.state_dir", "runtime.log_dir",
)


class ConfigError(RuntimeError):
    """Raised for a missing, malformed, or under-specified instance config."""


def _dig(data, dotted):
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _expand(value):
    """`~` and `$VAR` expansion for a path-shaped value, always absolute.

    Config files are written by hand and `~/.config/...` is the natural way to
    write a home-relative path. Left unexpanded it becomes a literal `./~`
    directory next to the working dir — a silent, wrong, and writable path.

    SYMLINKS ARE DELIBERATELY NOT FOLLOWED. This used `Path.resolve()` until the
    U14 cutover, where it took the live instance down: a venv's `bin/python3` is
    a symlink to the real interpreter, so resolving it yielded the SYSTEM python
    — which has none of the venv's packages. The poller crash-looped on
    `ModuleNotFoundError: No module named 'telegram'` while the drain, which
    imports nothing extra, kept working and made the stack look half-healthy.
    `abspath` normalises `..` lexically and leaves the symlink alone, which is
    what a venv shim needs to stay a venv shim.
    """
    return Path(os.path.abspath(os.path.expandvars(os.path.expanduser(str(value)))))


class InstanceConfig:
    """One instance's settings, plus the names derived from its `name`."""

    def __init__(self, data, source=None):
        self._data = data or {}
        self.source = Path(source) if source else None
        missing = [k for k in REQUIRED if _dig(self._data, k) in (None, "")]
        if missing:
            raise ConfigError(
                f"instance config {self.source or '<inline>'} is missing "
                f"required field(s): {', '.join(missing)}")
        if "/" in str(self.name) or str(self.name).strip() != str(self.name):
            raise ConfigError(
                f"instance name {self.name!r} must be a bare slug — it becomes "
                f"a launchd label, a lock filename, and a process-match pattern")

    # --- identity ------------------------------------------------------------

    @property
    def name(self):
        return self._data["name"]

    @property
    def label(self):
        return self._data["label"]

    @property
    def workdir(self):
        return _expand(self._data["workdir"])

    @property
    def python(self):
        return _expand(self._data["python"])

    def instance_tag(self):
        """The argv marker every long-lived process of this instance carries.

        Both instances run the same `bin/poller.py`, so the executable path can
        no longer tell them apart (see module docstring). Every spawn appends
        this, and every probe/sweep matches on it, so an instance can only ever
        see its own processes.
        """
        return f"--instance={self.name}"

    def poller_match(self):
        """pgrep -f pattern selecting THIS instance's poller and no other.

        Matches the tag rather than the script path, and includes `poller.py`
        so a stray shell whose argv merely mentions the instance cannot pass for
        a live poller. The reading instance shipped a probe that matched a bare
        string and counted the supervisor's own grep as a healthy poller; it was
        then over-corrected into matching a path that a python process no longer
        had. Both bugs were a probe describing something other than "this
        instance's poller process".
        """
        return f"poller\\.py.*{self.instance_tag()}"

    # --- transport -----------------------------------------------------------

    @property
    def owner_chat_id(self):
        return int(_dig(self._data, "telegram.owner_chat_id"))

    @property
    def token_file(self):
        return _expand(_dig(self._data, "telegram.token_file"))

    @property
    def channel_state_dir(self):
        d = _dig(self._data, "telegram.channel_state_dir")
        return _expand(d) if d else self.log_dir

    # --- runtime -------------------------------------------------------------

    @property
    def gateway_state_dir(self):
        """Non-synced machine state: receipt WAL, drain lock, session map.

        Deliberately NOT inside the workdir. A high-frequency append-only log in
        a Dropbox/iCloud folder fights the sync daemon, and a lock file on a
        synced volume is not a lock — the other machine never sees it held.
        """
        return _expand(_dig(self._data, "runtime.state_dir"))

    @property
    def agent_state_dir(self):
        """Synced state the AGENT itself reads and writes, e.g. the handoff.

        A genuinely different thing from `gateway_state_dir`, and the two are
        easy to confuse — the reading instance carries two same-shaped
        `state_dir()` helpers in different modules for exactly this reason.
        Rule of thumb: if the model reads it, it lives here; if only the
        plumbing reads it, it lives in the gateway dir.
        """
        return self.workdir / "state"

    @property
    def log_dir(self):
        return _expand(_dig(self._data, "runtime.log_dir"))

    @property
    def backend(self):
        return _dig(self._data, "runtime.backend") or "A"

    @property
    def turn_timeout(self):
        return int(_dig(self._data, "runtime.turn_timeout") or 900)

    @property
    def job_timeout(self):
        return int(_dig(self._data, "runtime.job_timeout") or 1800)

    @property
    def quota_defer_at(self):
        return float(_dig(self._data, "runtime.quota_defer_at") or 0.90)

    @property
    def max_retries(self):
        return int(_dig(self._data, "runtime.max_retries") or 1)

    # --- permissions ---------------------------------------------------------

    @property
    def permission_mode(self):
        return _dig(self._data, "permissions.mode") or "dontAsk"

    @property
    def permission_settings(self):
        p = _dig(self._data, "permissions.settings_file")
        return _expand(p) if p else None

    @property
    def mcp_config(self):
        p = _dig(self._data, "permissions.mcp_config")
        return _expand(p) if p else None

    @property
    def env_passthrough(self):
        return list(self._data.get("env_passthrough") or ())

    # --- prompts -------------------------------------------------------------

    @property
    def prime_prompt_file(self):
        """Override for the session-bootstrap prompt; None uses the plugin's."""
        p = _dig(self._data, "prompts.prime")
        return _expand(p) if p else None

    @property
    def flush_prompt_file(self):
        """Override for the rotation flush prompt; None uses the plugin's."""
        p = _dig(self._data, "prompts.flush")
        return _expand(p) if p else None

    @property
    def silent_directive_file(self):
        """Prefix for a scheduled job that must NOT push to Telegram."""
        p = _dig(self._data, "prompts.silent_directive")
        return _expand(p) if p else None

    @property
    def reply_directive_file(self):
        """Prefix telling a scheduled job where to reply. A scheduled prompt
        arrives with no Telegram metadata of its own, so without this the turn
        does the work and has nowhere to send it."""
        p = _dig(self._data, "prompts.reply_directive")
        return _expand(p) if p else None

    # --- housekeeping --------------------------------------------------------

    @property
    def housekeeping_hour(self):
        return int(_dig(self._data, "housekeeping.hour") or 3)

    @property
    def handoff_path(self):
        """Absolute path to the handoff file.

        Relative to workdir on purpose: the handoff is the agent's own memory
        and lives in the repo it thinks in, not in the non-synced state dir.
        """
        rel = _dig(self._data, "housekeeping.handoff_file") or "state/session-handoff.md"
        p = Path(os.path.expanduser(str(rel)))
        return p if p.is_absolute() else self.workdir / p

    @property
    def housekeeping_script(self):
        s = _dig(self._data, "housekeeping.script")
        if not s:
            return None
        p = Path(os.path.expanduser(str(s)))
        return p if p.is_absolute() else self.workdir / p

    @property
    def log_cap_bytes(self):
        return int(_dig(self._data, "housekeeping.log_cap_bytes") or 2_097_152)

    @property
    def handoff_snapshot_pattern(self):
        """Regex matching a retirable `## ` section header in THIS instance's
        handoff. Default is reading's `## Current state (as of ...)`.

        Instance-specific because the handoff is written by the agent in its own
        idiom, and the two existing instances disagree completely: reading
        prepends `## Current state (as of 2026-08-02 ...)`, second-instance writes
        `## Чт 23.07 — утренний бриф отправлен ...`. A prune hardcoded to one
        shape silently no-ops on the other — which is what happened to second-instance,
        whose handoff reached 2366 lines (past the 2000-line single-Read
        ceiling, so recovery was reading a truncated head) while every prune
        reported a clean `noop`.
        """
        return (_dig(self._data, "housekeeping.handoff_snapshot_pattern")
                or r"^##\s+(?:Current|Earlier)\s+state\b")

    @property
    def handoff_order(self):
        """How to rank snapshots when deciding which to retire.

        `date` (default) reads an `as of YYYY-MM-DD` out of the header, so a
        hand-edit that moves a block cannot retire the newest state. `file`
        trusts newest-first file order, which is what a handoff whose headers
        carry no parseable year has to fall back on.
        """
        v = (_dig(self._data, "housekeeping.handoff_order") or "date").lower()
        if v not in ("date", "file"):
            raise ConfigError(
                f"housekeeping.handoff_order must be 'date' or 'file', got {v!r}")
        return v

    @property
    def msg_index_retention_days(self):
        """How long the send journal keeps records.

        This is delivery EVIDENCE, not chat history: it is how a retry decides
        whether a message already went out. Too short and a retry re-sends;
        the floor is however long a dead-lettered entry can sit before someone
        looks at it.
        """
        return int(_dig(self._data, "housekeeping.msg_index_retention_days") or 60)

    @property
    def schedules(self):
        return list(self._data.get("schedules") or ())

    # --- derived names -------------------------------------------------------

    @property
    def launchd_prefix(self):
        return self._data.get("launchd_prefix") or "com.example"

    def launchd_label(self, job=None):
        base = f"{self.launchd_prefix}.{self.name}"
        return f"{base}-{job}" if job else base

    def state_path(self, *parts):
        return self.gateway_state_dir.joinpath(*parts)

    def log_path(self, filename):
        return self.log_dir / filename

    def as_dict(self):
        return dict(self._data)


def load(path=None):
    """Load an instance config. Explicit path wins, else $ESTATE_INSTANCE_CONFIG.

    Called per use rather than cached in a module global — see the module
    docstring on why a constant resolved at import time is a live hazard.
    """
    src = path or os.environ.get(CONFIG_ENV)
    if not src:
        raise ConfigError(
            f"no instance config: pass a path or set ${CONFIG_ENV}. "
            f"There is no default — an instance that cannot identify itself "
            f"would inherit another instance's state directory and token.")
    src = Path(os.path.expanduser(str(src)))
    try:
        data = yaml.safe_load(src.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read instance config {src}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"instance config {src} is not valid YAML: {exc}") from exc
    return InstanceConfig(data, source=src)


if __name__ == "__main__":  # `python3 bin/instance_config.py [path]` — sanity check
    import sys
    cfg = load(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"name         {cfg.name}")
    print(f"label        {cfg.label}")
    print(f"workdir      {cfg.workdir}")
    print(f"gw_state     {cfg.gateway_state_dir}")
    print(f"agent_state  {cfg.agent_state_dir}")
    print(f"log_dir      {cfg.log_dir}")
    print(f"handoff      {cfg.handoff_path}")
    print(f"owner        {cfg.owner_chat_id}")
    print(f"tag          {cfg.instance_tag()}")
    print(f"poller_match {cfg.poller_match()}")
    print(f"launchd      {cfg.launchd_label()} / {cfg.launchd_label('morning-digest')}")
