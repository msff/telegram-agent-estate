# Prompt templates

The two prompts the RUNNER itself issues, as opposed to the scheduled-job
prompts named by `schedules[].prompt_file` in the instance config.

| file | when it fires | must not |
|---|---|---|
| `prime.txt` | first turn of a session (fresh start, or after a rotation dropped the id) | — |
| `flush.txt` | the silent rotation turn, before the session id is dropped | send anything to Telegram |

`{handoff}` is substituted with the instance's handoff file, relative to its
workdir. **Keep the placeholder.** A prompt naming a path literally will, on an
instance configured with a different handoff location, tell the model to read a
file that does not exist — and the turn then exits 0 having restored nothing.
That failure is invisible from the outside: the session looks alive, answers
normally, and has simply forgotten everything. The whole point of the mtime
check in `rotate()` is that this class of failure leaves no other trace.

These ship in Russian because the first two instances think in Russian. An
instance whose brain works in another language should point
`prompts.prime`/`prompts.flush` at its own files — translating is not optional
polish, since a prompt in the wrong language degrades the turn that restores
context.
