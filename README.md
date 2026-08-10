# telegram-agent-estate

**Chat with your own Claude agent from Telegram.**

You message it, it answers. It can also start conversations on its own — a
morning briefing, a weekly summary, a nightly tidy-up — using whatever files,
data and tools you've given it. It runs on your Mac, bills to your Claude
subscription, and keeps the conversation going through crashes, restarts and
closed laptops.

Useful for anything you want to reach from your phone: an assistant that watches
your data and pings you when something changes, something you ask questions of
during the day, or a chore that runs on a timer and reports back.

## How it works

```
Telegram ──▶ poller ──▶ message log ──▶ turn runner ──▶ claude -p --resume
             (watches)   (on disk)      (one at a time)        │
                                                               ▼
                                                       libexec/send.py
                                                       (replies to you only)
```

A small program watches Telegram and **writes every message to disk before
anything else happens**. A second program reads that log and answers one message
at a time by running the `claude` command. Replies go back through
`libexec/send.py`.

Writing it down first is the whole trick. If the answer crashes, or the machine
reboots, or the network drops — your message is still on disk and gets answered
when things come back. Nothing important lives in memory, so nothing dies with
the process.

**Why not just leave a `claude` session running?** That's the obvious version,
and every way it breaks is a quiet one:

| | a session left running | this |
|---|---|---|
| you edit `CLAUDE.md` | needs a restart, which costs the conversation | picked up on the next message |
| it crashes mid-answer | that message is gone | it gets answered on restart |
| login expires overnight | bot goes silent, no error anywhere | every message logs in fresh |
| message arrives while it's down | lost | waits its turn, then gets answered |

The conversation lives in a file rather than in a running process. That one
change is what makes the whole right-hand column true.

## Before you start

**macOS only.** Scheduling is done with launchd, Apple's built-in scheduler,
and it's woven through everything that decides when your bot runs and whether it
survives a reboot. There is no Linux or Windows version, and adding one would
not be a small change.

**Support posture: best-effort.** The author's own bots run on this daily, so
anything that breaks those gets fixed, and bug reports with steps to reproduce
are welcome. Feature requests, other platforms, and anything needing sustained
support are not promised. Treat a release as working code you're adopting, not
as a product with a maintenance commitment behind it.

**It is not a sandbox** — the most important sentence on this page. Your agent
gets a real shell. During setup you'll write an allowlist of what it may run,
and that list permits inline python, which means the agent can do anything your
user account can do whatever else the list says. The allowlist makes accidents
much less likely. It does not contain a determined process, and you should not
treat it as a wall.

What *is* real is much smaller, and worth knowing: the transport is owner-gated
at both ends. Messages from any chat but yours are dropped on arrival, and
`libexec/send.py` refuses to send anywhere but your chat — so a confused session
can't leak the conversation to someone else. Turns also run with a cleaned
environment, so the other credentials your shell exports never reach the model.

Point a bot at a folder you'd be comfortable handing to a capable,
occasionally wrong assistant with your password.

**You'll need:**

- A Mac.
- **Python 3.11 or newer.** macOS ships 3.9, which is too old, so install
  **Homebrew** python (or any other 3.11+). Setup checks this and stops early if
  it's missing, rather than failing halfway through.
- The `claude` CLI, logged in. Turns bill to that subscription; no API key is
  used or wanted.
- **A Telegram bot token of its own.** One token per bot, never shared — two
  programs polling the same token fight each other forever.

## Setup

**The easy way — ask Claude Code to do it.**

```sh
/plugin marketplace add msff/telegram-agent-estate
/plugin install telegram-agent-estate
```

Then, in the folder you want the bot to work in, just say *"set up a Telegram
bot for this project"*. The bundled `setup` skill drives the whole thing: it
checks your machine, tells you what it's about to do before it does it, takes
the token without ever displaying it, finds your chat id, writes the config,
builds the permission list with you one entry at a time, and proves the bot
actually replies before telling you it's done.

**By hand**, if you'd rather, it's five steps:

1. **Get a token** from [@BotFather](https://t.me/botfather) on Telegram
   (`/newbot`).

2. **Put it in a file only you can read.** Never in a launchd plist, never in
   the repo:
   ```sh
   umask 077
   mkdir -p ~/.config/my-agent
   printf 'TELEGRAM_BOT_TOKEN=%s\n' 'PASTE_TOKEN_HERE' > ~/.config/my-agent/env
   chmod 600 ~/.config/my-agent/env
   ```

3. **Build a venv** for this bot and install its four dependencies:
   ```sh
   python3 -m venv ~/.local/share/my-agent/venv
   ~/.local/share/my-agent/venv/bin/python3 -m pip install -r requirements.txt
   ```

4. **Copy `templates/instance.yaml`** to somewhere like
   `~/.config/my-agent/instance.yaml` and fill it in. It's commented throughout;
   the fields that matter are the bot's `name`, the `workdir` it works in, the
   `python` from step 3, and your own `owner_chat_id`. The allowlist template is
   `templates/permissions.json` — start restrictive and add as you go.

5. **Install and check it:**
   ```sh
   telegram-agent-estate provision
   telegram-agent-estate install ~/.config/my-agent/instance.yaml
   telegram-agent-estate parity  ~/.config/my-agent/instance.yaml
   ```
   `provision` downloads a pinned copy of this code for launchd to run,
   `install` writes the scheduled jobs and starts it, and `parity` is a
   self-test that tells you whether it genuinely works.

Now message your bot. It should answer.

### The four dependencies

| package | what it's for |
|---|---|
| `PyYAML>=6,<7` | reads your config file |
| `requests>=2.31,<3` | sends messages to Telegram |
| `python-telegram-bot>=20.8,<23` | receives them |
| `pytest>=7,<10` | **needed to run the bot, not just to develop it** |

That last one catches people out. When the `claude` CLI updates itself, the bot
holds incoming messages and runs its own self-test before accepting them again.
No pytest, no self-test, and the bot stays held — silently, with the cause a
long way from the symptom. `requirements.txt` explains every version bound.

## Scheduled work

Add jobs to your config and they become real scheduled tasks:

```yaml
schedules:
  - job: morning-brief
    hour: 9
    minute: 0
    not_before: "06:00"
    prompt_file: prompts/morning.txt

  - job: weekly-reflection
    weekday: 0            # 0 = Sunday
    hour: 11
    minute: 0
    silent: true          # think it through, but don't message me
```

**These schedule a conversation, not a message.** Nothing is written in advance.
At 09:00 the agent wakes up, reads the prompt, goes and looks at whatever it
needs, and composes the message right then — so what you get is always current.
If you want it to send a fixed piece of text, make that text the prompt.

What that gives you over a `cron` line running the same command:

- **It won't send twice.** A guard tracks what's already gone out today and
  reports back in exit codes — `0` ran it, `3` already done today, `4` one is
  in flight, `5` too early — which every layer passes along untouched. Blur `3`
  into a generic failure and duplicate suppression becomes a duplicate message,
  so nothing blurs it.
- **`not_before` stops time-travel.** If your Mac was asleep at 09:00 and wakes
  at 02:00, the missed job can fire then — and a 2am "good morning" would use up
  the day's marker so the real one never arrives.
- **Scheduled jobs never get mixed into your chat.** Messages that arrive
  together are answered together; every scheduled job gets a turn to itself.
- **It backs off near your usage limit.** Past 0.90 of your five-hour window,
  scheduled work waits rather than eating the rest of it, and catches up on its
  own once the window recovers — so automation never pushes your own
  conversations into overflow.
- **A failed job isn't a lost job.** It retries, then parks with an alert to
  you, the same way an unanswered message does.

**One thing launchd will not do:** wake a sleeping Mac, or catch up a run it
missed. Close the lid at 09:00, open it at 11:00, and the 09:00 job simply
didn't happen. That's survivable for a briefing and fatal for maintenance —
which is why the daily housekeeping isn't a scheduled job at all, but a
clock check that fires on the first chance it gets after the hour. If something
truly must happen every day, build it that way rather than on a timer.

## Commands

Enabling the plugin puts exactly one command on your PATH:

```sh
telegram-agent-estate provision                    # fetch the pinned code launchd runs
telegram-agent-estate install    <instance.yaml>   # write the jobs and start it
telegram-agent-estate parity     <instance.yaml>   # self-test
telegram-agent-estate upgrade    [--ref v0.2.0]    # move to another release
telegram-agent-estate uninstall  <instance.yaml>   # remove what was installed
```

**`uninstall` prints a plan and does nothing** until you add `--yes`. It removes
what the plugin put there — the scheduled jobs, the running processes, the
blocks it added to your `CLAUDE.md` — and leaves what's yours: your folder, your
venv, your notes, and, unless you ask otherwise, your saved state and your
token.

**`install` and `parity` run the provisioned copy, not the folder you're
standing in**, because the provisioned copy is what launchd actually executes.
Checking anything else would be checking a tree no bot runs.

## What's in the box

```
bin/            the single `telegram-agent-estate` command
libexec/        everything it runs (see libexec/README.md)
templates/      instance config, allowlist, launchd and prompt templates
skills/setup/   the guided install / migrate / repair / uninstall skill
tests/          the self-test suite
```

## Versions

**A tag is the unit of distribution.** Nothing installs from a branch and
nothing updates itself, so you're always running a release someone picked, and
it stays that way until you say otherwise. Code that changes underneath a
running daemon is the exact failure this project exists to remove.

**A pin is a commit SHA, not a tag name.** `provision` records the exact commit
it installed. A tag can be moved server-side with no sign of it on your machine,
and this is code that runs as you, unattended, on a timer.

Cutting a release, if you're forking: bump the version in
`.claude-plugin/plugin.json` and repeat it in `marketplace.json` — a test fails
if they disagree — then tag the commit `v` + that version. **The number must
change every time, even for a docs fix**, because installs land in a directory
named for the version and a repeat number can resolve to a directory that
already exists.

Only the newest tag is supported; there's no backport branch. See
[`SECURITY.md`](SECURITY.md) for what that means when the fix is a security one.

MIT licensed.
