# telegram-agent-estate

Run a personal Claude agent as a Telegram-driven daemon.

An owned long-poll receiver journals every inbound message to a durable receipt
WAL, and a flock-serialized runner answers it with one `claude -p --resume` per
turn. One plugin, many instances, no shared state between them.

```
Telegram ──▶ poller ──▶ receipt WAL ──▶ turn runner ──▶ claude -p --resume
             (owns the   (append-only   (flock'd drain,       │
              offset)     JSONL)         one group at a time) ▼
                                                      send_transaction.py
                                                       (Bot-API-direct)
```

## Why this shape

The obvious way to run a Telegram-driven agent is to keep a long-lived `claude`
session in `screen` and push messages into it. That works until it doesn't, and
the ways it fails are all silent:

| | long-lived session | this |
|---|---|---|
| edit `CLAUDE.md` | needs a restart, which costs the conversation | next turn reads it |
| crash mid-answer | whatever was in flight is gone | WAL replays it |
| OAuth goes stale overnight | bot silently stops answering | each turn reads the keychain itself |
| message during downtime | lost, or spooled and hand-drained | sits in the WAL, answered automatically |
| context | lives in a process | lives in a session id on disk |

The trade is that the conversation must survive *outside* the process, which is
what the session map and the nightly handoff rotation are for.

## Guarantees, and what backs them

- **At-least-once inbound.** The poller journals a whole `getUpdates` batch
  before advancing the offset, so a crash costs a re-delivery, never a message.
- **Exactly-once replies.** A `turn_started` record is written before invoking;
  drain-start reconciliation settles anything whose turn already delivered, so a
  crash between sending and marking does not answer twice.
- **Delivery truth is send-evidence, not exit status.** A turn whose tool was
  denied still exits 0 while narrating the refusal. A turn that owes a reply and
  produced no journaled outbound is treated as failed.
- **Poison entries never wedge the queue.** Two failures park an entry to a
  dead-letter file with an owner alert; everything behind it still runs.
- **Subscription-billed.** Turns authenticate through the keychain OAuth
  credential; `ANTHROPIC_API_KEY` is stripped from every turn's environment
  unconditionally.

## Layout

```
.claude-plugin/     plugin.json + marketplace.json
bin/                the executable stack (see bin/README.md — lands in U13)
templates/
  instance.yaml     the instance config schema, heavily commented
  permissions.json  allowlist template, reviewed and edited on install
  env.template      secrets file (token lives ONLY here)
  plists/           launchd templates for the supervisor and scheduled jobs
  CLAUDE-ops-section.md   paste into the instance's CLAUDE.md
skills/setup/       install / migrate / troubleshoot skill
tests/              the parity suite, doubling as per-instance self-test
```

## Status

**Scaffold (U12).** The architecture is proven in production on one instance
(first-instance, cut over 2026-07-28); `bin/` is transplanted and
parameterized in U13, after that instance's soak week. See `bin/README.md` for
why the code was deliberately not copied yet.
