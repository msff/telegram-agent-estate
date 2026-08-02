#!/usr/bin/env python3
"""Owned Telegram receive loop (U1/U4 — replaces the channel-plugin fork).

The transport decision was corrected on 2026-06-13 (see docs/SPIKE-U1.md): the
self-vendored channel-plugin fork could not be both stable AND delivered, and
Telethon (an MTProto/user-account library) is the wrong tool for a single-user
bot. The fork's whole reason — surfacing the quoted *fragment* of a reply — went
away with Bot API 7.0 (Dec 2023), which puts `message.quote`
(`TextQuote.text` + UTF-16 `position`) right in the `getUpdates` payload.

So this is a small `python-telegram-bot` long-poll receiver WE own. For every
message from the owner it builds an escaped `<channel>` block carrying
`reply_to_message_id` / `quote_text` / `quote_position` (the exact shape the
brain's Quote-resolution rules already expect) and appends it to the gateway's
receipt WAL. It does NOT send — outbound goes through `send.py` (`reply` for
conversation) and the instance's own transaction path for digests, both
Bot-API-direct and transport-agnostic.

OFFSET OWNERSHIP (U11 cutover). Until the cutover this module used ptb's
`Application.run_polling`, which owns the offset: it confirms an update as soon
as its handler returns, so the WAL write was only a SHADOW of the real delivery
path (an `inject.sh` push into a live `screen` session, with a spool as the
last-resort backstop). ptb was chosen precisely because it owns offset tracking,
409-conflict handling and sleep/wake reconnect — none of which we wanted to
rewrite while the inject was authoritative.

That trade flips once the WAL IS the delivery path. `run_polling` would confirm
an update the moment the handler returned, so a crash between the confirm and
the journal write would lose the message outright — Telegram never re-sends a
confirmed update. So the loop below is manual: it calls `Bot.get_updates`
itself, journals the WHOLE batch, and only then advances the offset (the next
`get_updates` call is what actually confirms the previous batch server-side).
Journal-before-ack stops being a slogan and becomes literally true. The cost is
that the four things ptb handled are now ours, and they are handled explicitly
below: `Conflict` (409, a foreign poller), `RetryAfter` (429), network/timeout
errors, and the sleep/wake reconnect — all in `poll_once`/`poll_forever`.

The message→block logic (`block_from_message_dict`) is pure and operates on the
raw Telegram message JSON (what ptb's `Message.to_dict()` returns), so it is
unit-tested without the library or the network. The async runtime lazy-imports
`telegram` so those tests need nothing installed.

Run (the supervisor does this; token comes from the instance's token_file):
    <python> bin/poller.py --instance=<name> --config=<instance.yaml>

`--instance` is load-bearing rather than cosmetic — see main().
"""
from __future__ import annotations

import argparse
import asyncio
import html
import os
import sys
from datetime import datetime
from pathlib import Path


def log(msg, err=False):
    """Timestamped line to poller.log.

    Every line here used to be bare text. Diagnosing the 2026-07-29 latency
    report meant correlating the poller against WAL records that DO carry
    timestamps, and the poller's own log was useless for it — a receiver whose
    log cannot answer "when did you see this?" is not observable."""
    # flush=True is load-bearing, not decoration: the daemon redirects stdout to
    # poller.log, so Python block-buffers it and lines sit unwritten for a long
    # time. A log that appears minutes late is worse than none — it was briefly
    # lost here and made the poller look dead when it was fine.
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}",
          file=sys.stderr if err else sys.stdout, flush=True)

BINDIR = Path(__file__).resolve().parent
TURN_RUNNER = BINDIR / "turn_runner.py"

sys.path.insert(0, str(BINDIR))

import instance_config  # noqa: E402
import turn_runner  # noqa: E402  (the receipt WAL — journal-on-receipt)


def cfg():
    """This instance's config. Resolved per call — never an import-time
    constant, which cannot follow a test's environment override."""
    return instance_config.load()

# Long-poll window, seconds.
#
# In theory this is irrelevant to latency: Telegram returns a held request the
# instant a message arrives. In practice it is the ceiling on receive latency,
# because on a laptop the held connection goes stale (wifi power save, sleep,
# NAT timeout) without either side noticing — the server's push lands nowhere
# and the message is only seen when the NEXT request opens. Measured 2026-07-29:
# a test message took 48s to be journaled, i.e. essentially one full window.
#
# So this value is a trade: shorter = lower worst-case receive latency, more
# requests. 25s costs ~2 requests/min while halving the worst case.
POLL_TIMEOUT = 25
# Backoff for transport errors (network drop, laptop sleep/wake). Doubles to the
# cap, resets on the first clean batch.
BACKOFF_START = 1
BACKOFF_MAX = 60
# A 409 means ANOTHER process is long-polling this token. We hold the singleton
# flock, so it is foreign (a stray dev run, or the auto-loading channel plugin
# finding the token). Back off hard rather than fighting it into a hot loop.
CONFLICT_BACKOFF = 30


def acquire_singleton_lock(lock_path=None):
    """Exclusive non-blocking flock so only ONE poller ever long-polls the bot
    token. Two getUpdates loops on one token 409 each other endlessly; the
    daemon's ensure_poller can race (the nohup subshell defers the process, so
    two quick launches both pass the pgrep check). This lock is the real
    guarantee: a duplicate launch can't acquire it and exits cleanly. Returns
    the held fd (caller must keep it open for the process lifetime) or None if
    another instance already owns it."""
    import fcntl
    if lock_path is None:
        # Per instance. A shared lock path would make the SECOND instance's
        # poller exit as a "duplicate" of the first — one bot permanently deaf,
        # with a clean log and a healthy-looking process table.
        lock_path = cfg().gateway_state_dir / "telegram_poll.lock"
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return None
    return fd


def owner_chat_id() -> int:
    """Owner chat from the instance config. Only this chat may reach the session
    (the brain's security boundary).

    No hardcoded fallback any more: the old one silently returned the reading
    owner's id whenever the config could not be read, which under the plugin
    would let a misconfigured instance accept — and answer — traffic aimed at
    another bot. A config that cannot be read must fail, not guess."""
    return cfg().owner_chat_id


def _kind(msg: dict) -> str:
    """Short label for a non-text message so the session knows something
    arrived rather than silently dropping it."""
    for key in ("photo", "document", "video", "voice", "audio", "sticker",
                "animation", "video_note", "location", "contact", "poll"):
        if key in msg:
            return key
    return "message"


def _file_id(msg: dict):
    """The Telegram file_id of the message's attachment, or None. Photos arrive
    as an ascending size list — take the largest."""
    photos = msg.get("photo")
    if photos:
        return photos[-1].get("file_id")
    for key in ("document", "video", "voice", "audio", "animation",
                "video_note", "sticker"):
        obj = msg.get(key)
        if isinstance(obj, dict) and obj.get("file_id"):
            return obj["file_id"]
    return None


def inbox_dir() -> Path:
    """Instance media inbox. Joins the daemon's >14d sweep (U3)."""
    d = cfg().agent_state_dir / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def block_from_message_dict(msg: dict, owner_id: int,
                            local_path: str | None = None) -> str | None:
    """Build the injected `<channel>` block from a raw Telegram message dict
    (the shape `Message.to_dict()` returns). Returns None when the message is
    not from the owner — the allowlist is enforced here, at the transport edge.

    All outside-world text (body, quote, meta) is HTML-escaped before it enters
    prompt context: the security boundary treats it as untrusted data, and
    escaping stops a forwarded payload from spoofing a `</channel>` close or a
    fake `[SCHEDULED ...]` directive. `quote_text` lands escaped on purpose —
    `msg_index.resolve()` unescapes it itself (the brain passes it verbatim)."""
    chat_id = (msg.get("chat") or {}).get("id")
    from_id = (msg.get("from") or {}).get("id")
    # 1:1 bot: both the chat and the sender must be the owner.
    if chat_id != owner_id or from_id != owner_id:
        return None

    body = msg.get("text") or msg.get("caption") or f"[{_kind(msg)} — no text]"
    message_id = msg.get("message_id")

    attrs = [
        ('source', 'telegram'),
        ('chat_id', str(chat_id)),
        ('message_id', str(message_id)),
    ]
    reply_to = (msg.get("reply_to_message") or {}).get("message_id")
    if reply_to is not None:
        attrs.append(('reply_to_message_id', str(reply_to)))
    quote = msg.get("quote") or {}
    if quote.get("text") is not None:
        attrs.append(('quote_text', html.escape(quote["text"], quote=True)))
    if quote.get("position") is not None:
        attrs.append(('quote_position', str(quote["position"])))
    # Downloaded media (U10): the path lands as an ATTRIBUTE, never in the body.
    # Body text is html-escaped (quotes included), so a message that types
    # `local_path="..."` cannot forge this attribute — the only real one is the
    # wrapper's, mirroring the channel plugin's image_path design.
    if local_path:
        attrs.append(('local_path', html.escape(str(local_path), quote=True)))

    attr_str = " ".join(f'{k}="{v}"' for k, v in attrs)
    return f"<channel {attr_str}>\n{html.escape(body)}\n</channel>"


def is_owner_message(msg: dict, owner_id: int) -> bool:
    """The transport-edge allowlist, as a standalone predicate so the media
    path can gate BEFORE spending a getFile/download on a stranger's payload.
    1:1 bot: both the chat and the sender must be the owner."""
    return ((msg.get("chat") or {}).get("id") == owner_id
            and (msg.get("from") or {}).get("id") == owner_id)


async def download_media(bot, msg: dict):
    """Fetch the message's attachment into the instance inbox; return the local
    path (str) or None. CALLERS MUST OWNER-GATE FIRST — this spends network on
    whatever it is handed.

    Best-effort: a download failure must never lose the message, so it logs and
    returns None; the turn then simply sees no `local_path`."""
    file_id = _file_id(msg)
    if not file_id:
        return None
    try:
        f = await bot.get_file(file_id)
        suffix = Path(getattr(f, "file_path", "") or "").suffix or ".bin"
        dest = inbox_dir() / f"{msg.get('message_id')}-{file_id[:12]}{suffix}"
        await f.download_to_drive(str(dest))
        return str(dest)
    except Exception as exc:  # noqa: BLE001
        log("media download failed for message "
              f"{msg.get('message_id')}: {exc}", err=True)
        return None


async def handle_message_dict(msg: dict, owner: int, bot,
                              journal_fn=None) -> bool:
    """One inbound message, transport-agnostic and testable without ptb.

    Order is the security contract: owner gate -> download -> build block ->
    journal. A non-owner message returns before any network spend.

    Returns True when the message was journaled, False when it was legitimately
    skipped (not the owner). It RAISES if the journal write fails — that is the
    journal-before-ack contract: the caller must not advance the Telegram offset
    for a batch it could not durably record, because a confirmed update is never
    re-sent. Failing loudly here costs a re-delivery; swallowing it costs the
    message."""
    if not is_owner_message(msg, owner):
        log("dropped non-owner message from "
              f"chat={(msg.get('chat') or {}).get('id')}", err=True)
        return False
    local_path = await download_media(bot, msg) if _file_id(msg) else None
    block = block_from_message_dict(msg, owner, local_path=local_path)
    if block is None:                       # defense in depth; gate already ran
        return False
    journal_fn = journal_fn or turn_runner.journal_inbound
    journal_fn(msg.get("_update_id"), "message", owner, block,
               message_id=msg.get("message_id"))
    return True


# --- drain trigger ----------------------------------------------------------

async def trigger_drain(state=None, runner=None, python=None) -> None:
    """Ask the turn runner to drain, out of band.

    Deliberately a subprocess rather than an in-process `turn_runner.drain()`
    call: a turn takes tens of seconds, and the poll loop must stay free to keep
    journaling messages during it — that is what makes mid-turn arrivals coalesce
    into the NEXT turn instead of queueing behind the poller. It also keeps a
    turn-side crash from taking the receiver down with it.

    At most one trigger is in flight; extra ones would only bounce off the
    drain's flock. A trigger that loses that race is safe now that the drain
    re-folds before releasing the lock (turn_runner.wal_size)."""
    state = state if state is not None else _DRAIN_STATE
    task = state.get("task")
    if task is not None and not task.done():
        return
    runner = runner or str(TURN_RUNNER)
    python = python or sys.executable

    async def _run():
        try:
            proc = await asyncio.create_subprocess_exec(
                python, runner, "drain",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE)
            _, err = await proc.communicate()
            if proc.returncode != 0:
                log(f"drain exited {proc.returncode}: "
                      f"{(err or b'').decode()[:300]}", err=True)
        except Exception as exc:  # noqa: BLE001
            log(f"drain trigger failed: {exc}", err=True)

    state["task"] = asyncio.ensure_future(_run())


_DRAIN_STATE: dict = {}


# --- the poll loop (offset ownership) ---------------------------------------

async def poll_once(bot, owner, offset, *, handler=None, trigger=None):
    """One getUpdates round trip. Returns the NEXT offset to use.

    The whole batch is journaled before the returned offset moves past it, and
    the returned offset is what confirms the batch on the following call — so a
    crash anywhere in here costs a re-delivery (which the WAL dedups on
    update_id), never a lost message.

    A non-owner update advances the offset without being journaled: it is
    handled, just discarded. Not advancing would re-fetch a stranger's message
    forever."""
    handler = handler or handle_message_dict
    updates = await bot.get_updates(offset=offset, timeout=POLL_TIMEOUT,
                                    allowed_updates=["message"])
    if not updates:
        return offset
    journaled = 0
    for update in updates:
        msg = getattr(update, "effective_message", None) or getattr(
            update, "message", None)
        if msg is None:
            continue
        d = msg.to_dict()
        # Carry the update_id into the dict so the handler (pure w.r.t. ptb
        # types) can key the WAL on it.
        d["_update_id"] = update.update_id
        if await handler(d, owner, bot):
            journaled += 1
    # Ack point: every update in this batch is durably journaled (or was
    # deliberately dropped), so it is safe to confirm them by advancing.
    next_offset = updates[-1].update_id + 1
    if journaled:
        # Log the receive->journal moment explicitly. The WAL records when the
        # entry was written; this is the only place that can say when the poller
        # actually SAW it, which is the difference between "the receiver was
        # slow" and "the turn was slow" — the exact question a latency report
        # asks, and the one this log could not answer on 2026-07-29.
        log(f"journaled {journaled} update(s) up to {updates[-1].update_id}, "
            f"triggering drain")
        await (trigger or trigger_drain)()
    return next_offset


async def poll_forever(bot, owner, *, max_rounds=None, sleeper=None,
                       handler=None, trigger=None) -> None:
    """The receive loop. `max_rounds`/`sleeper` exist so tests can run it
    headlessly without real time passing."""
    from telegram.error import Conflict, NetworkError, RetryAfter, TimedOut

    sleep = sleeper or asyncio.sleep
    offset = None
    backoff = BACKOFF_START
    rounds = 0
    while max_rounds is None or rounds < max_rounds:
        rounds += 1
        try:
            offset = await poll_once(bot, owner, offset, handler=handler,
                                     trigger=trigger)
            backoff = BACKOFF_START
        except RetryAfter as exc:
            wait = getattr(exc, "retry_after", CONFLICT_BACKOFF)
            log(f"rate-limited, sleeping {wait}s", err=True)
            await sleep(float(wait))
        except Conflict as exc:
            # Someone else is polling this token. We hold the singleton flock,
            # so it is not another copy of us — surfacing it is the point.
            log("409 conflict (foreign poller on this "
                  f"token): {exc} — backing off {CONFLICT_BACKOFF}s", err=True)
            await sleep(CONFLICT_BACKOFF)
        except (TimedOut, NetworkError) as exc:
            # Includes the laptop sleep/wake case: the held connection dies and
            # comes back as a network error. Backoff, never a hot loop.
            log(f"transport error ({type(exc).__name__}: "
                  f"{exc}), retrying in {backoff}s", err=True)
            await sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
        except Exception as exc:  # noqa: BLE001
            # Anything else — including a WAL write failure, which deliberately
            # propagates so the offset does NOT advance past an unjournaled
            # batch. Telegram re-delivers it on the next round.
            log(f"poll round failed ({type(exc).__name__}: "
                  f"{exc}), retrying in {backoff}s", err=True)
            await sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)


def run() -> int:
    """Long-poll getUpdates and journal owner messages into the receipt WAL.
    Blocks forever; the daemon supervises this process."""
    from estate_client import load_secrets  # noqa: E402
    from telegram import Bot  # noqa: E402
    from telegram.request import HTTPXRequest  # noqa: E402

    # Singleton: refuse to be the second poller (would 409 the first forever).
    lock_fd = acquire_singleton_lock()
    if lock_fd is None:
        log("another poller holds the lock — exiting", err=True)
        return 0
    # lock_fd intentionally kept open for the process lifetime (the loop blocks).

    c = cfg()
    token = load_secrets(cfg=c, keys=("TELEGRAM_BOT_TOKEN",)).get("TELEGRAM_BOT_TOKEN")
    if not token:
        log(f"TELEGRAM_BOT_TOKEN missing — expected it in {c.token_file}", err=True)
        return 2
    owner = owner_chat_id()

    async def _main():
        # read timeout must exceed the long-poll window or every idle round trip
        # raises TimedOut and the loop backs off for nothing.
        request = HTTPXRequest(connection_pool_size=4,
                               read_timeout=POLL_TIMEOUT + 10,
                               connect_timeout=10)
        bot = Bot(token, request=request)
        # `async with bot` calls initialize() -> getMe, which is a NETWORK call
        # OUTSIDE poll_forever's error handling. On a laptop that sleeps and
        # wakes all day a DNS blip here is routine, and an unhandled one kills
        # the process — observed at the U11 cutover: the poller died on
        # `[Errno 8] nodename nor servname provided` and only came back on the
        # supervisor's next ensure_poller tick. Retry it here instead, with the
        # same backoff the poll loop uses.
        backoff = BACKOFF_START
        while True:
            try:
                await bot.initialize()
                break
            except Exception as exc:  # noqa: BLE001
                log("bot init failed "
                      f"({type(exc).__name__}: {exc}), retrying in {backoff}s", err=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
        try:
            log(f"long-polling for owner chat {owner} "
                  f"(offset-owning; journal -> WAL -> drain)")
            # A drain on startup settles anything journaled before a crash or
            # left pending by a quota window that has since recovered.
            await trigger_drain()
            await poll_forever(bot, owner)
        finally:
            await bot.shutdown()

    asyncio.run(_main())
    return 0


def main(argv=None):
    """CLI entry. `--instance=<name>` is not decoration — it is how this process
    is identified.

    Under the plugin every instance runs THIS file, so `pgrep -f poller.py`
    matches all of them and a supervisor restarting its own poller would reap
    its neighbour's. The tag goes in argv so `InstanceConfig.poller_match()` can
    select exactly one instance's process. It is also cross-checked against the
    config below, so a supervisor that passes one instance's tag with another's
    config fails at startup instead of running under a confused identity.
    """
    parser = argparse.ArgumentParser(description="Telegram poller (journal -> WAL)")
    parser.add_argument("--instance", default=None, metavar="NAME",
                        help="instance name; must match the loaded config")
    parser.add_argument("--config", default=None, metavar="PATH",
                        help=f"instance YAML (default: ${instance_config.CONFIG_ENV})")
    args = parser.parse_args(argv)

    if args.config:
        os.environ[instance_config.CONFIG_ENV] = str(args.config)
    try:
        c = cfg()
    except instance_config.ConfigError as exc:
        log(str(exc), err=True)
        return 2
    if args.instance and args.instance != c.name:
        log(f"--instance={args.instance} does not match the loaded config "
            f"({c.name} from {c.source}) — refusing to start under a confused "
            f"identity", err=True)
        return 2
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
