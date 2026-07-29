---
name: setup
description: Install, configure, or troubleshoot a telegram-agent-estate instance — a Claude agent driven by Telegram through a durable receipt WAL. Use when the user asks to set up a new bot instance, add a scheduled job, migrate an existing screen-based bot onto the estate, fix a silent bot, or extend an instance's permission allowlist.
---

# Setting up a telegram-agent-estate instance

An *instance* is one bot: one Telegram token, one workdir the agent thinks in,
one set of schedules. Multiple instances share this plugin and must never touch
each other's processes or state.

## Before anything: is this actually the right shape?

Ask, and stop if the answer is no:

- **Is there a dedicated bot token?** One token, one poller, forever. Two
  pollers on a token 409-war each other permanently. If the token is already
  used by n8n, another machine, or another instance — get a new bot.
- **Does the workdir have a `CLAUDE.md`?** The agent's behaviour lives there,
  not here. This plugin only decides *when* it thinks and *how* it speaks.
- **Is there a venv outside synced folders?** Dropbox/iCloud/Drive corrupt
  venvs and cannot host lock files.

## Install

1. **Create the instance config.** Copy `templates/instance.yaml` to
   `<workdir>/agent-instance.yaml` and fill it in. Read the comments — every
   field exists because something broke without it. Get `name` right first:
   launchd labels, lock names, state paths, and process-match patterns all
   derive from it, and two instances sharing a prefix will kill each other's
   processes.

2. **Create the secrets file.** Copy `templates/env.template` to the path named
   by `telegram.token_file`, add the token, `chmod 600`. Do not put the token
   anywhere else — see the warning in the template; it is not generic caution.

3. **Create the permission allowlist.** Copy `templates/permissions.json` to the
   path named by `permissions.settings_file`, then **edit it for this
   instance**. This is the step people skip and regret.

   Enumerate what this instance's brain is actually told to run:
   - every script command in its `CLAUDE.md`, written with the **literal**
     absolute interpreter path (a command containing `$VAR` is denied outright);
   - every MCP tool it uses (`mcp__whoop__*`, `mcp__Intervals_icu__*`, …);
   - the files it writes (`Edit`/`Write` on its state dir — allow **both**
     verbs, since the model picks `Edit` for a file that already exists).

   Be honest with the user about what this buys: it is **not a sandbox** — a
   brain allowed to run inline python can run anything. It buys no prompts, no
   headless hang, portability, and a deny-list for genuinely destructive verbs.
   The real security boundary is the owner-gated transport plus the
   owner-allowlisted send path.

4. **Add the ops section to the instance's CLAUDE.md.** Paste
   `templates/CLAUDE-ops-section.md`, substituting the interpreter path and
   handoff file. Without it the brain does not know that emitting text is not
   the same as replying.

5. **Render and install the launchd jobs.**
   ```
   <plugin>/bin/install-instance.sh <workdir>/agent-instance.yaml
   ```
   Plists install **by copy, never symlink** — `launchctl load` of a symlink
   whose target is in Dropbox fails EIO on macOS 15. Re-copy after any edit.

6. **Run the parity suite.** Fast mode first, then the real one:
   ```
   <plugin>/bin/parity.sh <workdir>/agent-instance.yaml
   <plugin>/bin/parity.sh <workdir>/agent-instance.yaml --real
   ```
   `--real` drives actual turns and **sends `[PARITY]` messages to the owner
   chat** — warn the user before running it. It is the only thing that proves
   the allowlist complete: a missing rule shows up as a turn that exits 0 and
   sends nothing, which fast mode cannot see.

## Migrating an existing screen-based bot

Cut over **atomically** — never run both transports on one token, even briefly.

1. Verify the new instance's parity suite green while the old stack still runs.
2. Check the WAL for a backlog before flipping; a shadow-journaling period can
   leave entries that would all be answered at once.
3. Stop the old daemon and its screen session, then start the instance.
4. Confirm: exactly one poller, no screen session, a real message round-trips.
5. Keep the old inject path unwired but present until a clean soak week.

**Re-derive the turn timeouts against this instance's heaviest real job.**
Timeouts carried over from another instance are the most likely thing to break
on the first live scheduled run.

## Troubleshooting

Work down the pipeline; the WAL tells you exactly how far a message got.

| Symptom | Look at |
|---|---|
| Nothing in the WAL | poller alive? `poller.log` for 401 (bad token) / 409 (foreign poller) |
| Journaled, never answered | `turns-held` (CLI-version tripwire), `drain.log` for a quota defer |
| Answered but silent | send journal — a turn that "replied" without sending is a denied tool |
| Parked | `dead-letter.jsonl` has the reason; the owner was alerted |

A **409** means something else polls this token: an orphaned poller from a
killed session, a claude whose env carries the token, or the bot deployed
elsewhere. The instance's own flock prevents its own duplicates.

Telegram drops updates older than ~24h server-side. A mac closed over a weekend
loses them before any poller sees them — not a bug, and no journal can help.
