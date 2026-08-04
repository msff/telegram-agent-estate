# Prompt templates

<!-- shipped-prompt-language: en -->

Everything shipped in this directory (outside `examples/`) is **English**. The
marker above is machine-readable and `tests/test_templates.py` asserts the files
match it — see "Language" below for why that is a test and not a preference.

## The four the runner issues

These fire from the runner itself, not from a schedule. Each is overridable per
instance via `prompts.*` in the instance config; unset uses the file here.

| file | config key | when it fires | must not |
|---|---|---|---|
| `prime.txt` | `prompts.prime` | first turn of a session (fresh start, or after a rotation dropped the id) | — |
| `flush.txt` | `prompts.flush` | the silent rotation turn, before the session id is dropped | send anything to Telegram |
| `silent-directive.txt` | `prompts.silent_directive` | prefix on a scheduled job marked `silent: true` | send anything to Telegram |
| `reply-directive.txt` | `prompts.reply_directive` | prefix on every other scheduled job | — |

## The one owner-facing notice

`quota-notice.txt` (`prompts.quota_notice`) is not a prompt. It is the message
the runner sends **you** when the usage window is nearly spent and it holds your
message rather than spill into paid credits. It ships here because it has the
same problem the prompts have — it is text a human reads, and no model composes
it, so nothing downstream can translate it after the fact. Hardcoded, it would
be the one place the installer's language stopped mattering.

It takes no placeholders. If the file named by `prompts.quota_notice` cannot be
read the runner falls back to this directory's copy, and then to a built-in
English line: unlike a prompt, whose absence should fail the turn loudly, this
string only explains a silence that is already happening.

`{handoff}` (prime, flush) is substituted with the instance's handoff file,
relative to its workdir; `{chat_id}` (reply directive) with the owner chat.
**Keep the placeholders.** A prompt naming a path literally will, on an instance
configured with a different handoff location, tell the model to read a file that
does not exist — and the turn then exits 0 having restored nothing. That failure
is invisible from the outside: the session looks alive, answers normally, and
has simply forgotten everything. The whole point of the mtime check in
`rotate()` is that this class of failure leaves no other trace.

## The three schedule starters

`morning.txt`, `evening.txt` and `weekly.txt` back the three example schedules in
`templates/instance.yaml`. They are **starters, not defaults** — they say nothing
about what your agent actually does, and they are meant to be replaced.

Two ways to replace them, and the second is the one to prefer:

- edit them here — but the plugin directory is upgraded underneath you, so an
  edit here is lost on the next update;
- copy them into the instance (`<workdir>/prompts/…` or `~/.config/<name>/…`)
  and point `schedules[].prompt_file` at your copy. `run-job.sh` resolves a
  relative `prompt_file` against the workdir first and the plugin second, so
  your copy wins by existing.

Delete the schedules you do not want rather than leaving them pointed at a
prompt file that is not there: a missing `prompt_file` exits the job with
status 2, on the schedule, forever, and only the job's log says so.

## Language

The shipped set is English because a public plugin cannot assume its installer's
language. `examples/ru/` holds a Russian translation of the four runner prompts
and the quota notice, kept as a worked example of an override rather than as a
fallback.

If your agent thinks in another language, translate the four and point
`prompts.*` at your files. This is **not optional polish**: a prompt in the
wrong language degrades the one turn whose entire job is restoring context, and
that degradation leaves no trace anywhere else. There is a second, smaller
reason the first instances wrote theirs in Cyrillic on purpose: a prompt that
shares no tokens with the ASCII strings the tests assert on cannot be confused
with them.
