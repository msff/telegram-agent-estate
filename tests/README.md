# Parity suite as per-instance self-test

`test_gateway_parity.py` is the phase gate that let the first instance cut over,
and it is what a freshly-installed instance runs to prove itself.

It is a straight copy from `first-instance/tests/` today and still imports
that instance's modules. U13 parameterizes it by instance config (`libexec/parity.sh
<instance.yaml> [--real]`) so any instance can run it.

Two modes, and the difference matters:

- **fast** (default) — injected fake invoker, ~1s, no tokens. Catches
  regressions in the WAL / coalescing / retry / dead-letter logic.
- **real** (`--real`) — drives actual `claude -p` turns and **sends `[PARITY]`
  messages to the owner chat**. This is the only mode that can prove the
  permission allowlist complete, because a missing rule shows up as a turn that
  exits 0 and sends nothing — invisible to fast mode by construction.

Real mode earned its cost immediately on the first instance: it caught a
rotation that reported success after writing nothing, and the allowlist gap that
caused it. Both would have silently discarded a day of context every night.
