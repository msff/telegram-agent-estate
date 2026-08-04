# telegram-agent-estate

Run a personal Claude agent as a Telegram-driven daemon.

An owned long-poll receiver journals every inbound message to a durable receipt
WAL, and a flock-serialized runner answers it with one `claude -p --resume` per
turn. One plugin, many instances, no shared state between them.

```
Telegram ──▶ poller ──▶ receipt WAL ──▶ turn runner ──▶ claude -p --resume
             (owns the   (append-only   (flock'd drain,       │
              offset)     JSONL)         one group at a time) ▼
                                                    libexec/send.py
                                                    (Bot-API-direct,
                                                     owner-only, journaled)
```

**macOS only.** launchd is the scheduler, and `launchctl`, `plutil`, BSD `stat`
flags and zsh-only syntax are load-bearing throughout — not incidentally, but in
the parts that decide when your bot runs and whether it comes back after a
reboot. There is no systemd path and no Windows path, and adding one is not a
small change. If you are not on a mac, this plugin is not for you.

**Support posture: best-effort.** It runs the author's own bots daily, so
breakage that affects those gets fixed. Issues are read and bugs with a
reproduction are welcome; feature requests, other platforms, and anything
needing sustained support are not promised. Treat a release as working code you
are adopting, not as a product with a maintenance commitment behind it.

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

## What the permission allowlist is — and what it is not

Every turn runs with `--permission-mode dontAsk` against an explicit allowlist
(`templates/permissions.json`, reviewed and edited at install). Read this
section before you install, because the temptation to read that file as a
security boundary is strong and it is wrong.

**What it is.** A way to run unattended without prompts and without hangs.
`bypassPermissions` is not usable here: its circuit-breaker prompts still fire
with no TTY, and a turn that blocks on one holds the drain lock forever — a
gateway-wide deadlock, not a lost turn. `dontAsk` plus an allowlist gives no
prompts, no hang, and deterministic denials: a missing rule fails a turn loudly
instead of granting silent power. The deny array is the part with real teeth,
and it is a **deny-list for genuinely destructive verbs and credential paths** —
`~/.ssh/**`, `~/.aws/**`, `~/.gnupg/**`, the shell rc files, the keychain,
`~/Library/LaunchAgents/**`. Those entries stop the obvious footguns and the two
persistence primitives that matter on a machine where this plugin installs
launchd jobs.

**What it is not.** It is **not a sandbox**. The allowlist permits the agent to
run inline python, and an agent that can run inline python can do anything the
user account can do — including reading a path the deny-list names, because
Python's `open()` is not the `Read` tool and no rule in that file applies to it.
The allowlist raises the cost of an accident. It does not contain a determined
process, and nothing here is an isolation boundary. Anything that reaches a
turn — a message, a quoted web page, a file the agent opens — is driving an
agent with a shell. Prompt injection into a privileged turn is unmitigated here
by design.

**What the real boundary is,** and it is a much smaller claim: the transport is
owner-gated at both ends. The poller drops any update from a chat that is not
this instance's configured owner, and `libexec/send.py` refuses any outbound
chat id that is not on the same one-entry allowlist — so a session that talks
itself into `reply --chat-id <someone else>` cannot exfiltrate the conversation.
Turns also run with a scrubbed environment: an explicit minimal set plus the
keys that instance declares, so the other credentials your shell exports do not
reach the model.

Point an instance at a workdir you would be comfortable handing to a capable,
occasionally wrong assistant with your shell.

## Requirements

- **macOS**, with launchd (see above).
- **Python 3.11 or newer.** macOS ships 3.9 in `/usr/bin` — and on a machine
  without Command Line Tools it ships a stub that refuses to run at all — so
  **Homebrew python (or any other 3.11+) is a prerequisite**, not an
  optimisation. The installer's preflight enforces the floor.
- The **`claude` CLI**, logged in. Turns authenticate through the keychain OAuth
  credential and are billed to that subscription; no API key is used or wanted.
- A **dedicated Telegram bot token**. One token, one poller, forever — two
  pollers on one token 409-war each other permanently.

One venv per instance, holding four packages:

```sh
python3 -m venv ~/.local/share/<instance>/venv
~/.local/share/<instance>/venv/bin/python3 -m pip install -r requirements.txt
```

| package | why |
|---|---|
| `PyYAML>=6,<7` | reads the instance config, which everything else resolves through |
| `requests>=2.31,<3` | Bot API calls from the send path and the journals |
| `python-telegram-bot>=20.8,<23` | the long-poller |
| `pytest>=7,<10` | **runtime, not dev** — the supervisor shells out to it |

That last one is not a packaging mistake. When the `claude` CLI self-updates the
supervisor holds every turn and runs the parity suite to decide whether to
release them; without `pytest` in the venv that command fails, parity never
passes, and the bot goes quiet with the cause a dozen log lines from the
symptom. `requirements.txt` carries the reasoning for every bound.

## Layout

```
.claude-plugin/     plugin.json + marketplace.json
bin/                ONE file: the `telegram-agent-estate` dispatcher
libexec/            the executable stack (see libexec/README.md)
templates/
  instance.yaml     the instance config schema, heavily commented
  permissions.json  allowlist template, reviewed and edited on install
  env.template      secrets file (token lives ONLY here)
  plists/           launchd templates for the supervisor and scheduled jobs
  prompts/          the runner's prompts + the owner-facing quota notice
  CLAUDE-ops-section.md   paste into the instance's CLAUDE.md
skills/setup/       install / migrate / troubleshoot skill
tests/              the parity suite, doubling as per-instance self-test
.github/workflows/  macOS CI: manifest validation, the suite, dependency drift
LICENSE             MIT
SECURITY.md         where to report a vulnerability, and what counts as one
```

## Commands

Enabling this plugin puts exactly one name on your PATH:

```sh
telegram-agent-estate provision [--source <url|path>] [--ref <tag>] [--status]
telegram-agent-estate upgrade   [--source <url|path>] [--ref <tag>] [--no-restart]
telegram-agent-estate install   <instance.yaml> [--dry-run] [--no-load]
telegram-agent-estate parity    <instance.yaml> [--real]
```

`uninstall` is declared and not yet implemented; it says so and exits 2.
Everything else — the supervisor, the poller, the turn runner, the
scheduled-job entry point — lives in `libexec/`, which launchd invokes by
absolute path and which is deliberately not on anyone's PATH. Arguments and
exit codes pass through the dispatcher unchanged.

**`install` and `parity` run the provisioned runtime, not the copy you typed.**
`provision` clones a pinned checkout into
`~/.local/share/telegram-agent-estate/runtime/` and records the resolved commit
SHA in `runtime.json` beside it; that checkout is what launchd executes and what
the dispatcher resolves to. The rule, in full:

| where the command lives | what runs |
|---|---|
| `$ESTATE_LIBEXEC` is set | that directory — the explicit override wins |
| inside a plugin cache | the provisioned runtime, or a refusal telling you to provision |
| a plain checkout | itself, with a note on stderr if a different pin is installed |

A cache-resident copy is never run: `/plugin update` installs into a new cache
directory and leaves the old one, so a command that ran the cache would verify
a tree no daemon executes. `provision` and `upgrade` are the exception — they
run from wherever you invoked them, because they are what creates the runtime.

Provisioning is offline-tolerant: with no `git`, or with an unreachable source,
it copies the tree instead and records `"mechanism": "copy"` in the stamp. **A
copied runtime has no upgrade path** — `provision --status` keeps saying so
until you re-provision with git.

The usual entry point is not either of these, though: ask Claude to set up an
instance and the `setup` skill drives the whole flow, including the disclosures
above, before anything is written.

## Releases, and what a pin means

**A tag is the unit of distribution.** Nothing here installs from a branch, and
nothing self-updates. If you are running this, you are running a tag someone
chose, and it stays that way until you say otherwise.

**Cutting one.** The version in `.claude-plugin/plugin.json` is the source of
truth; the plugin entry in `.claude-plugin/marketplace.json` repeats it and a
test fails when the two disagree. To release: bump both, get
`claude plugin validate . --strict` and the suite green, commit, then tag the
commit `v` + that exact version — v0.2.0 for version 0.2.0 — and push the tag.

Semantics are ordinary semver with the 0.x caveat that applies honestly here:
while the major is 0, a minor bump is where a breaking change lands, and there
will be breaking changes — the instance config schema, the layout on disk, and
the dispatcher's verb list are all still moving.

**The version must actually change on every release**, even for a docs-only
fix. Installed plugins land in `~/.claude/plugins/cache/<marketplace>/<plugin>/`
under **a directory named for the version, one per version, old ones left in
place**. Publish twice under one version number and an installer can resolve a
directory that already exists and never fetch what you shipped.

**A pin is a commit SHA, not a tag name.** The checkout that launchd actually
executes is provisioned at a resolved SHA and the stamp file records that SHA,
not the tag it came from. This is not pedantry: a git tag is mutable, can be
moved server-side with no signal on your machine, and what sits behind this one
is code that runs as you, on a timer, unattended. A tag names a release; the
SHA is what you are running.

**Upgrading is explicit, always.** There is no auto-update path and there will
not be one — a daemon whose code changes underneath it without a restart is the
exact failure this project is built to eliminate. An upgrade fetches, verifies
that the checked-out HEAD is the SHA that was asked for, and restarts the
supervisors, in that order.

**Only the latest tag is supported.** There is no backport branch; see
[`SECURITY.md`](SECURITY.md) for what that means when the fix is a security one.
