---
name: setup
description: Install, configure, repair or remove a telegram-agent-estate instance — a Claude agent driven by Telegram through a durable receipt WAL. Use when the user asks to set up a new bot instance, add a scheduled job, migrate an existing screen-based bot onto the estate, fix a silent bot, upgrade the runtime, or uninstall an instance.
---

# Setting up a telegram-agent-estate instance

An *instance* is one bot: one Telegram token, one workdir the agent thinks in,
one set of schedules. Multiple instances share this plugin and must never touch
each other's processes or state.

**You are driving this, not narrating it.** Run the commands, read the output,
and ask the user only what cannot be derived — `inspect-project.py` exists so
that the questions are few and specific. Where a step says *ask*, ask; where it
says *never*, the reason is in the line beneath it.

`<plugin>` below is this plugin's directory. `<name>` is the instance slug.

## Which of the five things is being asked

| the ask | start at |
|---|---|
| "set up a bot for this project" | **Install**, step 1 |
| "move my existing bot onto this" | **Install**, then **Migrating** |
| "the bot has gone quiet" | **Repair** |
| "update the plugin" | **Upgrade** |
| "get rid of it" | **Uninstall** |

## Install

### 1. Confirm the shape, and stop if it is wrong

Ask, and do not proceed past a "no":

- **Is there a dedicated bot token?** One token, one poller, forever. Two
  pollers on a token 409-war each other permanently, and the loser is silent
  rather than broken. If the token is already used by n8n, another machine, or
  another instance — get a new bot from BotFather first.
- **Is there a workdir?** A directory the agent thinks in, ideally with a
  `CLAUDE.md` already. Its behaviour lives there, not here; this plugin decides
  *when* it thinks and *how* it speaks, never *what about*.
- **Would they hand that directory to a capable, occasionally wrong assistant
  with their shell?** That is the actual question the permission model asks. If
  the answer is no, the workdir is wrong, not the settings.

### 2. Preflight the machine

Run these and read the answers; do not assume any of them.

```sh
sw_vers -productVersion                 # macOS — launchd is the scheduler
python3 -VV                             # 3.11+; /usr/bin/python3 is 3.9 or a stub
claude --version && claude auth status   # logged in; turns bill to that subscription
```

Three failures worth naming, because each looks like something else later:

- **Python below 3.11**, or a Command Line Tools stub that exits 127. Homebrew
  python is a prerequisite, not an optimisation.
- **`claude` not logged in.** Every turn would exit 0 having done nothing. Never
  set `ANTHROPIC_API_KEY` to work around it — that moves billing off the
  subscription and the runner drops it anyway.
- **A workdir inside Dropbox/iCloud/Drive.** Fine for the workdir itself; fatal
  for the venv and the state dir. `inspect-project.py` flags it in step 3 and
  steps 6 and 9 keep both out.

### 3. Inspect the project

```sh
<plugin>/libexec/inspect-project.py <workdir>
```

Stdlib-only and read-only: it runs before the venv exists and prints no
credential value, only variable names. Read the whole report. What matters:

- `instance.slug` / `instance.label` — the derived name. Confirm it; everything
  namespaced comes from it (launchd labels, lock names, state paths, process
  patterns), and two instances sharing a prefix sweep each other's processes.
- `warnings` — synced storage, most often. Carry it into steps 6 and 9.
- `claude_md.estate_markers` — non-empty means this project already has an
  instance. You are repairing or upgrading, not installing.
- `allowlist_candidates`, `mcp_servers` — the raw material for step 10. They are
  listed under `opt_in_required` for a reason: they are proposals, not findings.

### 4. Say what this is, before you write anything

Report what you derived and what you still need. Then, **before the first byte
lands on disk**, tell the user plainly:

> The permission allowlist is **not a sandbox.** Your agent legitimately runs
> inline python, so allowing that is equivalent to allowing arbitrary code. What
> the allowlist buys is: no permission prompts (a headless turn cannot answer
> one), no hung turns, portability to another machine, and a deny-list for a few
> genuinely destructive verbs. The real security boundary is that the transport
> only accepts messages from your chat id, and the send path refuses to send
> anywhere else.
>
> I am about to create: a venv, a config directory holding your bot token, a
> state directory, launchd jobs that run unattended on a timer, and two marked
> blocks inside your project's `CLAUDE.md`. Everything outside those two blocks
> is left byte-identical.

Get an explicit yes. This is the only disclosure that is load-bearing — a user
who learns at step 13 that a timer now runs as them was told too late.

### 5. Provision the runtime

```sh
telegram-agent-estate provision            # or: --source <url|path> --ref <tag>
telegram-agent-estate provision --status
```

launchd will execute this pinned checkout, not the plugin cache. A plugin
update installs into a *new* cache directory and leaves the old one, so a plist
pointing at the cache would keep running a version nobody chose, forever,
without ever failing. The stamp records the resolved **commit SHA**, never the
tag name — a tag can move server-side with no signal here, and what sits behind
it is code that runs as the user, unattended.

Offline is fine: with no git or an unreachable source it copies the tree and
records `"mechanism": "copy"`. Say so if that happens — **a copied runtime has
no upgrade path** until it is re-provisioned with git.

### 6. Build the venv, outside any synced folder

```sh
python3 -m venv ~/.local/share/<name>/venv
~/.local/share/<name>/venv/bin/python3 -m pip install -r <plugin>/requirements.txt
```

`~/.local/share/`, never the workdir: a synced venv corrupts, and a lock file on
a synced volume is not a lock. Four packages, and `pytest` is one of them at
runtime — when the `claude` CLI self-updates, the supervisor holds every turn
and runs the parity suite to decide whether to release them. Without `pytest` in
the venv that command fails, parity never passes, and the bot goes quiet with
the cause a dozen log lines from the symptom.

### 7. Capture the token without ever seeing it

**Do not ask the user to paste the token into this chat.** It would be written
to the transcript on disk, and a transcript is not a secret store. Hand them
this to run in their own terminal:

```zsh
umask 077
mkdir -p ~/.config/<name>
read -rs "tok?Paste the bot token (input is hidden): "
print -r -- "TELEGRAM_BOT_TOKEN=$tok" > ~/.config/<name>/env
unset tok
chmod 600 ~/.config/<name>/env
```

Then verify **shape and mode, never content**:

```sh
stat -f '%A %N' ~/.config/<name>/env      # expect 600
grep -c '^TELEGRAM_BOT_TOKEN=.' ~/.config/<name>/env   # expect 1
```

`grep -c` prints a count. Never `cat` this file, never echo the variable, never
run `curl -v` against the Bot API — the URL carries the token and `-v` prints
the URL.

**This file is the only place the token may live.** Not the plist's
`EnvironmentVariables`, not the repo, not a turn's environment. The official
telegram channel plugin auto-loads inside every `claude` session, including the
headless turns this stack spawns, and it starts polling any
`TELEGRAM_BOT_TOKEN` it finds — so a leaked token turns each turn into a second
poller that 409-wars this instance's own receiver.

### 8. Learn the owner chat id, with a nonce

Ask the user to send the bot one message containing a word you pick now — say
`estate-7413` — and nothing else. Then, **once**:

```zsh
set -a; source ~/.config/<name>/env; set +a
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" | \
/usr/bin/python3 -c 'import json,sys
for u in json.load(sys.stdin).get("result", []):
    m = u.get("message") or {}
    c = m.get("chat") or {}
    if c.get("type") == "private":
        print(u["update_id"], c.get("id"), (m.get("text") or "")[:40])'
```

Take the chat id of the message whose text is your nonce **and** whose
`chat.type` is `private`. Both halves matter: a group the bot was added to also
produces updates, and a group chat id in `owner_chat_id` means every member can
drive the agent.

Then drain what you just read, so the poller does not answer the nonce as its
first turn:

```zsh
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates?offset=<last_update_id + 1>" >/dev/null
```

**Only ever do this before the poller exists.** `getUpdates` against a live
poller is the 409 war described above, with this instance as one of the
combatants. Repair does not do this step.

### 9. Write the instance config

Copy `<plugin>/templates/instance.yaml` to `~/.config/<name>/instance.yaml` and
fill it in. Read its comments as you go — every field is there because
something broke without it. Not into the workdir: the config sits beside the
token, in a directory that is not synced and not in a repo.

What you already know: `name`, `label`, `workdir`, `python` (the step-6 venv,
**absolute, on one line** — a folded YAML scalar is unreadable to the shell
bootstrap), `telegram.owner_chat_id`, `telegram.token_file`,
`telegram.channel_state_dir`, `runtime.state_dir`, `runtime.log_dir`.

What to ask:

- **`launchd_prefix`** — their own reverse domain or handle. Left as
  `com.example`, every install on every machine lands under the same reserved
  domain and the installer's clobber check can no longer tell two people's jobs
  apart.
- **Schedules.** Delete the ones they do not want rather than leaving them
  pointing at prompts nobody wrote; a `prompt_file` that resolves to nothing
  exits the job with status 2.
- **`runtime.turn_timeout` / `job_timeout`.** Size these against the heaviest
  real job, not a benchmark. The first instance shipped with 300s derived from
  a spike that measured 6s warm turns; its first live scheduled digest timed out
  twice and dead-lettered, because the real composition work took 7m26s.
- **Language.** If the agent thinks in something other than English, point the
  four `prompts:` entries at translations —
  `templates/prompts/examples/ru/` is a set to copy and adapt. A prime or flush
  prompt in the wrong language degrades the very turn that restores context, and
  the failure is invisible: the session looks alive, answers normally, and has
  quietly forgotten everything.

### 10. Build the allowlist, one item at a time, default off

Copy `<plugin>/templates/permissions.json` to the `permissions.settings_file`
path, then work through `allowlist_candidates` from step 3 **one by one**.

**Nothing is added unless the user says so for that item.** A candidate is
evidence that a command exists in their `CLAUDE.md` or `scripts/`, not evidence
that a timer-driven agent should be allowed to run it unattended. Present each
with its origin (`claude_md:<line>` or `scripts/<file>`) and take silence as no.

Two things that will otherwise cost a debugging session each:

- Substitute `{python}` with the interpreter **exactly as written in the
  config**, tilde and all. The tilde form matched a real invocation; the same
  rule spelled with the expanded `/Users/...` path did not.
- A command containing shell-variable expansion (`$PY`) is denied regardless of
  the allowlist, because the matcher cannot know what it expands to. That is why
  the ops block tells the agent to write the interpreter path literally.

MCP servers are separate: they are not gated by this file. Membership comes from
`permissions.mcp_config`, so **adding a server there is what grants it**. Offer
the `mcp_servers` list the same way — per item, default off — and write the ones
they pick into their own MCP config. Leaving `mcp_config` unset gives the turn
the machine's full set, which is slower but never silently missing a server; a
diet is worth real latency, but a server left out is invisible to the turn and
`dontAsk` gates MCP tools too, so a scheduled job exits 0 having done nothing.

### 11. Write the two CLAUDE.md blocks

```sh
<plugin>/libexec/claude-md-block.py set <workdir>/CLAUDE.md \
    ops=<filled-in ops file> brief=<filled-in brief file>
```

Fill the placeholders **before** calling it: the ops block needs
`[ABSOLUTE_PYTHON_PATH]`, `[ESTATE_LIBEXEC]` and `[HANDOFF_FILE]`; the brief
needs the project description, the bot's job, the fact sources, the language and
the voice — most of which step 3 already gave you in `project_description`.

Do not hand-edit the `CLAUDE.md` with the Edit tool instead. This script is the
only writer of a file the user already owned: it splices on lines with their
endings preserved, so everything outside the markers is byte-identical
afterwards, it repairs rather than duplicates on a re-run, and it refuses a
malformed marker pair rather than guessing which repair was meant. Those are
assertions in its test suite; an agent following prose is a hope.

It reports what it did as JSON. If it exits 2, read the `error.message` — every
refusal names the line and what to do about it — and fix the file rather than
retrying.

The brief is the user's to rewrite, and worth saying so: it is the fastest lever
they have on how the bot behaves, and a plugin upgrade refreshes the ops block
without touching it.

### 12. Install the launchd jobs

```sh
telegram-agent-estate install ~/.config/<name>/instance.yaml --dry-run
telegram-agent-estate install ~/.config/<name>/instance.yaml
```

Dry-run first and read what it would write. Plists are installed **by copy,
never symlink** — `launchctl load` of a symlink whose target is in Dropbox fails
EIO on macOS 15, quietly enough to look like "the job just didn't fire". Re-copy
after any config edit; the installer is idempotent and does exactly that.

If it refuses with a clobber error, two instances are wearing one name. Rename
one. Do not work around it.

### 13. Prove it, before you say it works

```sh
telegram-agent-estate parity ~/.config/<name>/instance.yaml
telegram-agent-estate parity ~/.config/<name>/instance.yaml --real
```

Fast mode first. Then warn the user that `--real` **drives actual turns and
sends `[PARITY]` messages to their chat**, and get a yes before running it.

`--real` is the only thing that proves the allowlist complete. A missing rule
does not fail a turn: the turn exits 0 and the model narrates the denial, which
fast mode cannot see and which is indistinguishable from success everywhere
except the absence of a message.

Then have them send one real message and confirm a reply.

### 14. Hand it over

Tell them, in their own paths:

- **Where the logs are** — `runtime.log_dir`: `daemon.log`, `poller.log`,
  `drain.log`.
- **What a failure looks like** — a turn that owes a reply and sends nothing is
  retried once, then parked in `dead-letter.jsonl` with an alert to their chat.
- **How to change the agent's behaviour** — edit the `CLAUDE.md`; it is re-read
  every turn, and there is no restart to wait for.
- **How to change the schedule** — edit the config, re-run `install`.
- **That the brief block is theirs**, and that a plugin upgrade will refresh the
  ops block beside it without touching it.

## Migrating a bot that already exists

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

## Repair

A silent bot. Work down the pipeline; the WAL tells you exactly how far a
message got.

**Do not call `getUpdates` while diagnosing.** The poller is live by definition
here, and a second reader is the 409 you may be about to diagnose — self-
inflicted, and it looks exactly like the real thing.

| Symptom | Look at |
|---|---|
| Nothing in the WAL | poller alive? `poller.log` for 401 (bad token) / 409 (foreign poller) |
| Journaled, never answered | `turns-held` (CLI-version tripwire), `drain.log` for a quota defer |
| Answered but silent | send journal — a turn that "replied" without sending is a denied tool |
| Parked | `dead-letter.jsonl` has the reason; the owner was alerted |

A **409** means something else polls this token: an orphaned poller from a
killed session, a `claude` whose environment carries the token, or the bot
deployed on another machine. The instance's own flock prevents its own
duplicates, so it is never that.

Re-running `install` is safe and is the fix for a plist that drifted. Re-running
`claude-md-block.py set` is safe and is the fix for a mangled ops block. Neither
needs an uninstall first.

Telegram drops updates older than ~24h server-side. A mac closed over a weekend
loses them before any poller sees them — not a bug, and no journal can help.

## Upgrade

```sh
telegram-agent-estate upgrade --ref <tag>
```

It verifies the checked-out HEAD is the SHA that was asked for **before**
restarting anything, and refuses (exit 3) if the tag has moved since the stamp
was written. Then refresh the ops block, which may have changed with it:

```sh
<plugin>/libexec/claude-md-block.py set <workdir>/CLAUDE.md ops=<new ops file>
```

Do not pass `brief=` on an upgrade. That block is the user's prose by then, and
the two markers are separate precisely so this cannot overwrite it.

## Uninstall

```sh
telegram-agent-estate uninstall ~/.config/<name>/instance.yaml
```

It prints a plan and exits 3, changing nothing. Read the plan back to the user,
then re-run with `--yes`.

It removes what the plugin installed: the launchd jobs, this instance's
processes, and the two `CLAUDE.md` blocks. It keeps the workdir, the venv and
the handoff file under every flag — the handoff is the agent's own memory, and
an uninstall is not the moment to decide a year of context is worthless.

Two things are opt-in, and worth asking about separately rather than bundling:

- `--purge-state` — the receipt WAL and the send journal. The WAL may hold
  messages nobody answered, and the journal is how a retry knows a message
  already went out.
- `--purge-secrets` — the token file and the allowlist. Say the part the flag
  cannot do: **deleting the file does not revoke the token.** If the bot is
  finished with, they revoke it in BotFather, or it stays live in their shell
  history and backups.

If the venv is already gone, it still works — degraded to a text read of the
config, enough to unload the jobs and clean the `CLAUDE.md`, and it refuses the
purges rather than guessing at a directory to delete.
