# `libexec/` — the shared executable stack

Transplanted from the first instance's own `daemon/` directory and parameterized
by the instance config. **Nothing here may hardcode an instance-specific
string.**

**Why `libexec/` and not `bin/`** (KTD5): Claude Code adds an enabled plugin's
`bin/` to the Bash tool's PATH for every session, so shipping these files
under those names would put `send.py`, `guard.py`, `parity.sh` and
`supervisor.sh` into every installing user's shell. `bin/` holds exactly one
file — the `telegram-agent-estate` dispatcher — and everything real lives here,
reached either through that dispatcher or, for launchd, by absolute path.

| file | from | notes |
|---|---|---|
| `instance_config.py` | *new* | the seam; everything else reads paths through it |
| `estate-bootstrap.zsh` | *new* | sourced, not run: sed-reads `python:` so no entry point needs an ambient interpreter |
| `estate-runtime.zsh` | *new* | sourced, not run: the one copy of "which `libexec/` is this?" — stamp, cache, or self |
| `provision-runtime.sh` | *new* | clones the pinned checkout launchd executes; `upgrade` verifies HEAD before restarting |
| `estate_client.py` | `scripts/<api>_client.py` (generic half) | logging, state roots, JSONL journals, conflict guard |
| `msg_index.py` | `scripts/msg_index.py` | message→document index + send journal |
| `turn_runner.py` | `daemon/turn_runner.py` | receipt WAL, coalescing, retry, rotation, handoff prune |
| `poller.py` | `daemon/telegram_poll.py` | owned long-poll receiver |
| `guard.py` | `scripts/send_transaction.py` (scheduling half) | markers, leases, payloads, the guard |
| `send.py` | `scripts/send_transaction.py` (send half) | one message, journaled, owner-only |
| `supervisor.sh` | `daemon/daemon.sh` (loop) | poller health, drain backstop, CLI tripwire |
| `housekeeping.sh` | `daemon/daemon.sh` (03:00 block) | rotation, log caps, inbox sweep |
| `run-job.sh` | `daemon/inject.sh --job` | one scheduled job; guard exit codes preserved |
| `install-instance.sh` | *new* | renders plists, copies (never symlinks) |
| `inspect-project.py` | *new* | onboarding step 3: what a target project already declares — **stdlib only**, it runs before the venv exists, and it prints no credential value |
| `parity.sh` | *new* | wraps `tests/test_gateway_parity.py` per instance |

What did NOT come over, and should not: that instance's third-party API client,
its digest transaction (chunked resume, tag swaps, per-item deep links), and
anything else that is a concern of the *agent* rather than of the *gateway*.

## The rule this directory exists to enforce

Before the plugin, two instances were two checkouts, so isolation was free —
every path and every `pgrep` pattern differed by construction. **Consolidation
destroys that and hands back the obligation to manufacture it.** Both instances
now execute these exact files.

So:

- **Never match a process on a path under `libexec/`.** Always match the
  `--instance=<name>` tag (`InstanceConfig.poller_match()`). A path-keyed probe
  matches every instance, which turns "restart my poller" into "kill my
  neighbour's".
- **Never resolve a path from `__file__`.** That yields the shared plugin
  directory, not the caller's workdir.
- **Never cache the config in a module global.** An import-time constant cannot
  follow an environment override — that is exactly how a plain `pytest tests/`
  once pruned a live handoff file.
- **Never default to a shared location.** Prefer a hard error: an instance that
  cannot identify itself would otherwise inherit another's state dir and token.

`tests/test_two_instances.py` is the gate. It builds two complete instances side
by side and asserts nothing crosses — markers, leases, locks, WALs, session
maps, journals, handoffs, outbound allowlists, launchd labels, and process
probes. Add to it whenever you add shared state.

## Running one instance by hand

```sh
export ESTATE_INSTANCE_CONFIG=~/.config/<name>/instance.yaml

libexec/supervisor.sh   "$ESTATE_INSTANCE_CONFIG"      # the daemon (launchd does this)
libexec/housekeeping.sh "$ESTATE_INSTANCE_CONFIG"      # force the daily window now
libexec/run-job.sh      "$ESTATE_INSTANCE_CONFIG" morning-digest
libexec/parity.sh       "$ESTATE_INSTANCE_CONFIG" [--real]
libexec/install-instance.sh "$ESTATE_INSTANCE_CONFIG" [--dry-run] [--no-load]

libexec/turn_runner.py --instance=<name> drain
libexec/turn_runner.py --instance=<name> prune-handoff --dry-run
libexec/guard.py guard <job>        # exit: 0 run · 3 marker · 4 lease · 5 too early
```

The two with a user-facing name are also reachable through the dispatcher, which
is what a user has on their PATH and what the docs point at:

```sh
telegram-agent-estate install "$ESTATE_INSTANCE_CONFIG" [--dry-run] [--no-load]
telegram-agent-estate parity  "$ESTATE_INSTANCE_CONFIG" [--real]
```

`--instance` is cross-checked against the loaded config; a mismatch exits 2
rather than running under a confused identity.
