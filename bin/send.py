#!/usr/bin/env python3
"""The outbound send primitive: one Telegram message, journaled, owner-only.

Extracted from `first-instance/scripts/send_transaction.py` alongside
`guard.py`. What stayed behind in the reading repo is the R20 digest
transaction — chunking, Readwise tag swaps, `»читать` Reader links — because
none of that is what a second instance means by "send a message".

TWO INVARIANTS, both of which have teeth:

1. OWNER-ONLY OUTBOUND. A non-owner chat_id is refused before any network call.
   The poller enforces the inbound half at the transport edge; this is the
   matching outbound guard, so a prompt-injected session that talks itself into
   calling `reply --chat-id <foreign>` cannot exfiltrate the conversation. The
   allowlist comes from the instance config, so instance A physically cannot
   send into instance B's chat either.

2. THE JOURNAL RECORD IS THE DELIVERY EVIDENCE. Every send stamps the msg-index
   with the turn id from $ESTATE_TURN_ID. That record — not the turn's exit
   status — is how the runner knows a turn already replied before it died, and
   therefore whether a retry would double-send. A turn whose Bash tool was
   denied under `dontAsk` still exits 0 while narrating the refusal, so exit
   status answers a different question than the one being asked.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

import instance_config
import msg_index
from estate_client import load_secrets, log

_URL_RE = re.compile(r'https?://[^\s<>"]+')


class SendError(Exception):
    """Telegram send failed: HTTP error or ok=false in the Bot API reply."""


def default_chat_id(cfg=None):
    """The owner chat. No hardcoded fallback, deliberately: the reading version
    fell back to a literal chat id, which under the plugin would mean a
    misconfigured instance silently sending into the OTHER bot's chat."""
    return (cfg or instance_config.load()).owner_chat_id


def allowed_chat_ids(cfg=None):
    """The 1:1 bot's send-side allowlist — only the owner chat may receive."""
    return {default_chat_id(cfg)}


def render_html(text, link_label=None):
    """Escape for Telegram HTML and turn bare URLs into anchors.

    `link_label` renders every link with one label instead of its URL; the
    reading instance uses it for `»читать`. Left None, links show their URL.
    """
    out, last = [], 0
    for m in _URL_RE.finditer(text):
        out.append(html.escape(text[last:m.start()]))
        url = m.group(0)
        label = html.escape(link_label) if link_label else html.escape(url)
        out.append(f'<a href="{html.escape(url, quote=True)}">{label}</a>')
        last = m.end()
    out.append(html.escape(text[last:]))
    return "".join(out)


def bot_api_sender(chat_id, text, parse_mode=None, cfg=None, link_label=None):
    """Bot API sendMessage, link previews off. Returns the message_id.

    parse_mode=None → plain text, which is bulletproof: nothing in the message
    can break the send. parse_mode='HTML' → run through render_html first. The
    caller's `text` is unchanged either way, so what gets journaled and later
    matched against a quote-reply stays plain.
    """
    c = cfg or instance_config.load()
    token = load_secrets(cfg=c, keys=("TELEGRAM_BOT_TOKEN",)).get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SendError(
            f"TELEGRAM_BOT_TOKEN missing — expected it in {c.token_file}")
    body = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse_mode == "HTML":
        body["text"] = render_html(text, link_label)
        body["parse_mode"] = "HTML"
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage", json=body, timeout=30)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400 or not data.get("ok"):
        raise SendError(f"sendMessage -> {resp.status_code}: {resp.text[:300]}")
    return data["result"]["message_id"]


def reply(text, chat_id=None, cfg=None, sender=None):
    """Send ONE conversational message and journal it. Returns the message_id.

    A single sendMessage: no payload persistence and no chunk journal, because
    there is nothing to resume. The raw index record still goes in, so a later
    quote-reply to THIS message resolves back through msg_index.

    Deliberately NOT the R20 transaction path — scheduled digests must still go
    through persist → send → mark, which handles chunking and resume.
    """
    c = cfg or instance_config.load()
    if chat_id is None:
        chat_id = default_chat_id(c)
    if chat_id not in allowed_chat_ids(c):
        raise SendError(f"reply refused: chat {chat_id} is not on the owner "
                        f"allowlist (outbound is owner-only)")
    send = sender or (lambda cid, t: bot_api_sender(cid, t, cfg=c))
    message_id = send(chat_id, text)

    turn_id = os.environ.get("ESTATE_TURN_ID")
    meta = {"turn_id": turn_id} if turn_id else None
    msg_index.append_raw(chat_id, message_id, text, meta=meta, cfg=c)
    log(f"reply: message_id={message_id} -> chat {chat_id} "
        f"({len(text)} chars), journaled"
        + (f", turn={turn_id}" if turn_id else ""))
    return message_id


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("text", help="message body")
    parser.add_argument("--chat-id", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        reply(args.text, args.chat_id)
    except SendError as exc:
        log(f"send: {exc}", err=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
