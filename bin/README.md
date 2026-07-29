# `bin/` — intentionally empty until U13

The executable stack (supervisor, poller, turn runner, guard, housekeeping) is
**not** vendored here yet. It currently lives in the first-instance repo at
`daemon/` and is still the live code for that instance.

## Why it was not copied during U12

Copying it now would fork code that is demonstrably still settling. The reading
cutover (2026-07-28) changed the shape four times in its first hours of real
load:

- per-turn timeouts were wrong by an order of magnitude and had to be split by
  source;
- the poller-liveness probe was wrong twice, in opposite directions;
- bot initialization needed a retry that the poll loop's handler never saw;
- the startup orphan sweep had to be deleted outright.

None of those were reachable from tests. A copy taken before the soak week would
have to absorb every one of those fixes twice, by hand, with the two copies
silently diverging in between — which is exactly the failure mode a shared
plugin exists to prevent.

## What lands here in U13

Transplanted from `first-instance/daemon/`, parameterized by the instance
config (`templates/instance.yaml`) so that nothing instance-specific remains:

| file | from | must stop hardcoding |
|---|---|---|
| `supervisor.sh` | `daemon/daemon.sh` | workdir, log dir, poller path, owner chat, label prefix |
| `poller.py` | `daemon/telegram_poll.py` | workdir, config path, token file, inbox dir |
| `turn_runner.py` | `daemon/turn_runner.py` | state dir, timeouts, permissions path, handoff path, prompts |
| `guard.py` | part of `scripts/send_transaction.py` | marker/lease/payload roots |
| `housekeeping.sh` | the 03:00 block of `daemon.sh` | which extra script to run, log caps |
| `install-instance.sh` | *new* | renders `templates/plists/*.tmpl`, copies (never symlinks) |
| `parity.sh` | wraps `tests/test_gateway_parity.py` | workdir, instance config |

The gating rule for U13: **every string in the table's right-hand column must
come from the instance config, and two mock instances must run side by side
without their sweeps, locks, or probes crossing.**
