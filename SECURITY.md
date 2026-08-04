# Security policy

This plugin installs launchd jobs that run on your machine as you, holds a
Telegram bot token, and drives an agent with a shell. If you find a way it
harms someone who installed it, please tell the maintainer before you tell
anyone else.

## Reporting a vulnerability

**Preferred — private, and it does not depend on the maintainer's inbox:**

  https://github.com/msff/telegram-agent-estate/security/advisories/new

That is GitHub's private vulnerability reporting form. It opens a thread only
you and the maintainer can see, and it is the channel to use for anything you
would not post in a public issue.

**Do not email the address in the manifests.** The `author` block of
[`.claude-plugin/plugin.json`](.claude-plugin/plugin.json) carries a GitHub
`users.noreply.github.com` address. It exists so the manifest has a real,
attributable identity — it does **not** receive mail, and anything sent there
is discarded silently, which is the worst possible fate for a vulnerability
report. The advisory form above is the private channel.

**If the advisory form is unavailable to you** (no GitHub account, or the form
is down), open a public issue saying only that you have a security report and
would like a contact address. Say nothing else — no reproduction, no affected
version, no hint at the mechanism.

Please **do not** open a public issue for a vulnerability, and do not include a
live bot token, an owner chat id, or any other credential in a report — a
redacted reproduction is more useful than a working one.

## What to expect

**Support posture: best-effort**, the same as the README states for everything
else here. There is no bounty, no SLA, and one maintainer.

- Acknowledgement: within about a week. If a fortnight passes with nothing,
  assume it was missed rather than ignored, and nudge the advisory thread —
  it is the only private channel, so there is nowhere else to retry.
- A confirmed issue gets a fix on the `main` branch and a new tag. Tags are the
  unit of distribution (see the README's release section), so the fix reaches
  an installed user only when they run an explicit upgrade — say so in the
  advisory rather than assuming anyone auto-updates. Nothing here does.
- Credit in the advisory if you want it, and none if you would rather not.
- Only the **latest tag** is supported. There is no backport branch.

## Threat model — read this before reporting

Some of what looks like a vulnerability here is a documented design position,
and a report about it will be closed as such. The README's section on the
permission allowlist is the long version; this is the short one.

**Not a vulnerability, by design:**

- **The permission allowlist is not a sandbox.** Turns run with
  `--permission-mode dontAsk` against an allowlist that permits inline python,
  and anything that can run inline python can do whatever your user account
  can do — including reading a path the deny array names, because `open()` is
  not the `Read` tool. The allowlist raises the cost of an accident. It does
  not contain a determined process. "The agent can escape the allowlist" is the
  documented state of affairs, not a finding.
- **Prompt injection into a turn is unmitigated.** A message, a quoted web
  page, or a file the agent opens can steer it. This is a gateway to an agent
  with a shell; treat every input as untrusted and point an instance only at a
  workdir you would hand to a capable, occasionally wrong assistant.
- **The agent can read the machine it runs on.** Turns run under your account.

**In scope, and worth reporting:**

- Anything that lets a chat which is **not** the configured owner get a turn
  run, or get a reply delivered to it. The owner-gated transport at both ends —
  the poller's inbound filter and the outbound guard in `libexec/send.py` — is
  the one real boundary this project claims, so a way around either is a
  genuine break.
- A **credential leaving the machine** or landing somewhere it should not: the
  bot token appearing in a log, a journal, a WAL record, an error message, or
  any file that is not the mode-600 env file; a generated config with secrets
  written inside a cloud-synced folder; a token reaching a turn's environment
  that the scrub was supposed to remove.
- **Turn environment leakage** — an environment variable the scrub should have
  stripped reaching the model, `ANTHROPIC_API_KEY` surviving into a turn (that
  also silently moves billing off the subscription), or the keychain credential
  being read by anything other than the CLI itself.
- **Anything the installer writes with wrong ownership or permissions**, a
  path it will follow across a symlink it did not create, or a rendered launchd
  plist that executes a path a non-root user could substitute.
- Anything in the **onboarding flow** that writes a secret into the target
  project's workdir, into git, or into a shared location.

If you are unsure which side of that line a finding sits on, report it
privately and say so. An over-report costs a paragraph; an under-report costs
whoever installs this next.
