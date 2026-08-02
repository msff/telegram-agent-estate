#!/usr/bin/env python3
"""Per-chunk message→document index + send journal (U4).

Maps every doc-linked outbound Telegram message (digest chunks, discussion
replies, weekly reflections) back to the Reader documents its text blocks
came from, so a user reply or quote-reply can be resolved to a specific
document. Storage is one append-only JSONL file, `state/msg-index.jsonl`,
written with `O_APPEND` under `flock` and read tolerating one trailing
partial line (crash mid-write) — the shared ledger discipline.

Two record types, because two different processes write them — the split
is what makes drift detectable instead of silent:

  raw        — appended by the TRANSPORT at send time for EVERY outbound
               message: {"type","chat_id","message_id","text","ts"}.
               This is also the send journal (R20).
  annotation — appended by the LIVE SESSION after composing:
               {"type","chat_id","message_id","blocks":[
                   {"start","end","reader_id","title"}, ...], "ts"}.
               start/end are Python-string char offsets into the raw text;
               digest chunking never splits a block across messages, so
               offsets are always chunk-relative.

The scheduled inject diffs raw records against annotations (drift_report):
a sent message the session never mapped to documents is reported, never
silently unresolvable.

resolve() follows the KTD fallback chain and NEVER guesses silently:
exact substring match in the replied-to chunk → quote-position
disambiguation (Telegram positions are UTF-16 code units, not Python
str indexes!) → fuzzy match across recent indexed messages → ambiguous /
unresolved with candidate titles for the session to ask the user about.
"""

import difflib
import fcntl
import html
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import instance_config
from estate_client import agent_state_dir, append_jsonl, log, read_jsonl

INDEX_FILENAME = "msg-index.jsonl"

# Fuzzy-resolution tuning (KTD: exact → position → fuzzy → ask).
FUZZY_WINDOW_DAYS = 14   # paraphrases only make sense against recent sends
FUZZY_FLOOR = 0.6        # below this a block is not a plausible source
FUZZY_RESOLVE = 0.75     # minimum top score to auto-resolve...
FUZZY_GAP = 0.1          # ...and only when clearly ahead of the runner-up
FUZZY_WEAK = 0.5         # best-effort candidates on an unresolved answer
DRIFT_WINDOW_HOURS = 48
MAX_CANDIDATES = 3


def index_path(path=None, cfg=None):
    """Default index location; injectable for tests and housekeeping.

    The AGENT state dir, not the gateway one: the live session reads this to
    resolve a quote-reply back to the document it came from, so it belongs with
    the material the model can see.
    """
    return Path(path) if path else agent_state_dir(cfg) / INDEX_FILENAME


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _parse_ts(ts):
    """Naive-local comparison is fine here: retention granularity is days."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


# --- writers ------------------------------------------------------------------

def append_raw(chat_id, message_id, text, path=None, meta=None):
    """Transport-side: journal every outbound message at send time.

    `meta` (optional dict) is merged into the record — the send transaction
    (R20) stamps `payload_id`/`chunk_idx` here so the index doubles as its
    resume journal. Core record fields win on key collision: `type` and
    friends are load-bearing for the whole index.
    """
    rec = dict(meta or {})
    rec.update({"type": "raw", "chat_id": chat_id, "message_id": message_id,
                "text": text, "ts": _now_iso()})
    append_jsonl(index_path(path), rec)


def annotate(chat_id, message_id, blocks, path=None):
    """Session-side: record block→document char ranges for a sent chunk.

    Validated loudly at write time: a malformed annotation would not fail
    here but as a wrong quote resolution days later — reject it now.
    """
    checked = []
    for b in blocks:
        missing = {"start", "end", "reader_id", "title"} - set(b)
        if missing:
            raise ValueError(f"annotation block missing keys {sorted(missing)}: {b}")
        start, end = b["start"], b["end"]
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end:
            raise ValueError(f"annotation block has invalid range {start}..{end}: {b}")
        checked.append({"start": start, "end": end,
                        "reader_id": b["reader_id"], "title": b["title"]})
    append_jsonl(index_path(path), {
        "type": "annotation", "chat_id": chat_id, "message_id": message_id,
        "blocks": checked, "ts": _now_iso()})


# --- readers ------------------------------------------------------------------

def get_message(chat_id, message_id, path=None):
    """Merged view {"text", "blocks"} of one sent message, or None.

    Last record of each type wins: a resumed send transaction (R20) may
    legitimately re-append either record, and the latest is authoritative.
    """
    text = None
    blocks = None
    for row in read_jsonl(index_path(path)):
        if row.get("chat_id") != chat_id or row.get("message_id") != message_id:
            continue
        if row.get("type") == "raw":
            text = row.get("text")
        elif row.get("type") == "annotation":
            blocks = row.get("blocks", [])
    if text is None and blocks is None:
        return None
    return {"text": text, "blocks": blocks or []}


def utf16_offset_to_str_index(text, utf16_offset):
    """Convert a Telegram quote_position (UTF-16 code units) to a Python
    str index.

    Python strings count code points; Telegram counts UTF-16 units.
    Cyrillic is 1 unit in both, but emoji and other astral-plane chars are
    2 UTF-16 units — using the raw offset would skew every position after
    the first emoji in a digest. Offsets past the end clamp to len(text).
    """
    units = 0
    for i, ch in enumerate(text):
        if units >= utf16_offset:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


# --- resolution ---------------------------------------------------------------

def _result(status, block=None, candidates=None):
    return {"status": status,
            "reader_id": block["reader_id"] if block else None,
            "title": block["title"] if block else None,
            "candidates": candidates or []}


def _candidates(blocks):
    """Candidate list for an "ask the user" answer; deduped by document."""
    out, seen = [], set()
    for b in blocks:
        if b["reader_id"] in seen:
            continue
        seen.add(b["reader_id"])
        out.append({"reader_id": b["reader_id"], "title": b["title"]})
        if len(out) == MAX_CANDIDATES:
            break
    return out


def _block_for_offset(blocks, offset):
    for b in blocks:
        if b["start"] <= offset < b["end"]:
            return b
    return None


def _occurrences(text, quote):
    offs, start = [], 0
    while True:
        i = text.find(quote, start)
        if i < 0:
            return offs
        offs.append(i)
        start = i + 1


def _norm(s):
    return " ".join(s.lower().split())


def _fuzzy_score(quote, block_text):
    """Similarity of a (possibly paraphrased) quote to one block's text.

    A quote is usually one sentence out of a multi-sentence digest block;
    a whole-slice SequenceMatcher ratio would punish the block for its
    length. So: exact (normalized) containment scores 1.0, comparable
    lengths get a plain ratio, and long blocks get a quote-sized sliding
    window with the best window ratio kept — the thresholds in resolve()
    then mean the same thing for short replies and long digest blocks.
    """
    q = _norm(quote)
    b = _norm(block_text)
    if not q or not b:
        return 0.0
    if q in b:
        return 1.0
    if len(b) <= 2 * len(q):
        return difflib.SequenceMatcher(None, q, b).ratio()
    win = len(q)
    step = max(1, win // 2)
    best = 0.0
    for i in range(0, len(b) - win + 1, step):
        best = max(best, difflib.SequenceMatcher(None, q, b[i:i + win]).ratio())
    return max(best, difflib.SequenceMatcher(None, q, b[-win:]).ratio())


def _recent_messages(chat_id, path, days):
    """Merged per-message views for one chat, restricted to recent records.

    A message is "recent" if its newest record is inside the window;
    records without a parseable ts are kept — better to consider a stale
    candidate than to silently ignore an indexed message.
    """
    cutoff = datetime.now() - timedelta(days=days)
    merged = {}
    for row in read_jsonl(path):
        if row.get("chat_id") != chat_id:
            continue
        m = merged.setdefault(row.get("message_id"),
                              {"text": None, "blocks": None, "ts": None})
        if row.get("type") == "raw":
            m["text"] = row.get("text")
        elif row.get("type") == "annotation":
            m["blocks"] = row.get("blocks")
        ts = _parse_ts(row.get("ts"))
        if ts and (m["ts"] is None or ts > m["ts"]):
            m["ts"] = ts
    return {mid: m for mid, m in merged.items()
            if m["text"] and m["blocks"] and (m["ts"] is None or m["ts"] >= cutoff)}


def _fuzzy_resolve(chat_id, quote_text, path):
    """Step 3 of the chain: SequenceMatcher across all recent indexed
    blocks of the chat. Scores deduped by document first — two chunks
    mentioning the same doc are agreement, not ambiguity."""
    best_by_doc = {}
    for m in _recent_messages(chat_id, path, FUZZY_WINDOW_DAYS).values():
        for b in m["blocks"]:
            score = _fuzzy_score(quote_text, m["text"][b["start"]:b["end"]])
            cur = best_by_doc.get(b["reader_id"])
            if cur is None or score > cur[0]:
                best_by_doc[b["reader_id"]] = (score, b["title"])
    scored = sorted(
        ({"score": s, "reader_id": rid, "title": title}
         for rid, (s, title) in best_by_doc.items()),
        key=lambda x: -x["score"])

    plausible = [x for x in scored if x["score"] >= FUZZY_FLOOR]
    if plausible:
        top = plausible[0]
        runner_up = plausible[1]["score"] if len(plausible) > 1 else 0.0
        if top["score"] >= FUZZY_RESOLVE and top["score"] - runner_up >= FUZZY_GAP:
            return _result("resolved", top)
        # plausible but not confident → ask, never guess (KTD)
        return _result("ambiguous", candidates=[
            {"reader_id": x["reader_id"], "title": x["title"]}
            for x in plausible[:MAX_CANDIDATES]])
    weak = [x for x in scored if x["score"] >= FUZZY_WEAK][:2]
    return _result("unresolved", candidates=[
        {"reader_id": x["reader_id"], "title": x["title"]} for x in weak])


def resolve(chat_id, replied_message_id=None, quote_text=None,
            quote_position=None, path=None):
    """Resolve a reply/quote-reply to a document. Returns
    {"status": "resolved"|"ambiguous"|"unresolved",
     "reader_id", "title", "candidates": [{"reader_id","title"}...]}.

    Fallback chain (KTD — never guess silently):
      1. replied-to message annotated, no quote → single block resolves,
         multiple blocks ask with candidates in block order;
      2. quote → exact substring in the replied-to chunk, quote_position
         (UTF-16!) disambiguates duplicate matches;
      3. no exact match (paraphrase, or reply to an unindexed message) →
         fuzzy across the chat's recent indexed blocks.
    """
    path = index_path(path)
    if quote_text:
        # The plugin fork HTML-escapes quote_text (escapeMeta — untrusted
        # inbound data) before it reaches the session, while the journal
        # stores the RAW sent text. Unescape so exact matching compares
        # like with like — otherwise any quote with an apostrophe (&#39;)
        # silently degrades to fuzzy.
        quote_text = html.unescape(quote_text)
    msg = (get_message(chat_id, replied_message_id, path=path)
           if replied_message_id is not None else None)

    if msg and msg["blocks"]:
        blocks = msg["blocks"]
        if not quote_text:
            if len(blocks) == 1:
                return _result("resolved", blocks[0])
            return _result("ambiguous", candidates=_candidates(blocks))
        if msg["text"]:
            hits = [(off, _block_for_offset(blocks, off))
                    for off in _occurrences(msg["text"], quote_text)]
            hits = [(off, b) for off, b in hits if b is not None]
            distinct = []
            for _, b in hits:
                if b not in distinct:
                    distinct.append(b)
            if len(distinct) == 1:
                return _result("resolved", distinct[0])
            if len(distinct) > 1:
                if quote_position is not None:
                    idx = utf16_offset_to_str_index(msg["text"], quote_position)
                    # Telegram documents quote_position as approximate —
                    # take the occurrence nearest the converted index.
                    _, block = min(hits, key=lambda h: abs(h[0] - idx))
                    return _result("resolved", block)
                return _result("ambiguous", candidates=_candidates(distinct))
        # no exact match in the replied-to chunk → fall through to fuzzy

    if quote_text:
        log(f"msg_index.resolve: no exact match for quote in chat {chat_id} "
            f"(replied={replied_message_id}) — trying fuzzy")
        return _fuzzy_resolve(chat_id, quote_text, path)

    return _result("unresolved")


# --- housekeeping --------------------------------------------------------------

def prune(retention_days=None, path=None, cfg=None):
    """Drop records older than retention; rewrite atomically. Returns the
    number of dropped records.

    Runs in the 03:00 housekeeping window. flock is held across the whole
    read→temp→rename so concurrent appends serialize against it; a writer
    already blocked on the OLD inode's lock when the rename lands would
    append to the orphaned file — accepted, since nothing sends at 03:00.
    Records without a parseable ts count as expired: every writer stamps
    ts, so such lines are foreign/corrupt and keeping them forever would
    defeat retention.
    """
    path = index_path(path, cfg)
    if retention_days is None:
        # Was `load_config()["index"]["retention_days"]` — a key that exists only
        # in the reading instance's config.yaml, so on any other instance the
        # nightly housekeeping call would have died with a KeyError.
        retention_days = (cfg or instance_config.load()).msg_index_retention_days
    if not path.exists():
        return 0
    cutoff = datetime.now() - timedelta(days=retention_days)
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        rows = read_jsonl(path)
        kept = [r for r in rows
                if (ts := _parse_ts(r.get("ts"))) is not None and ts >= cutoff]
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for r in kept:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    dropped = len(rows) - len(kept)
    log(f"msg_index.prune: kept {len(kept)}, dropped {dropped} "
        f"(retention {retention_days}d) in {path}")
    return dropped


def drift_report(path=None):
    """Raw sends from the last 48h with no annotation, as
    [{"chat_id","message_id"}...] in send order.

    The transport journals every send; the session annotates after
    composing. A gap means quote-replies to that message cannot resolve —
    the scheduled inject reports it so drift is loud, never silent.
    A raw record without a parseable ts is treated as recent (loud side).
    """
    cutoff = datetime.now() - timedelta(hours=DRIFT_WINDOW_HOURS)
    seen, order, annotated = set(), [], set()
    for row in read_jsonl(index_path(path)):
        key = (row.get("chat_id"), row.get("message_id"))
        if row.get("type") == "raw":
            ts = _parse_ts(row.get("ts"))
            if ts is not None and ts < cutoff:
                continue
            if key not in seen:
                seen.add(key)
                order.append(key)
        elif row.get("type") == "annotation":
            annotated.add(key)
    return [{"chat_id": c, "message_id": m}
            for c, m in order if (c, m) not in annotated]
