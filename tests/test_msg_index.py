import importlib
from datetime import datetime, timedelta

import pytest

import instance_config
import msg_index
from estate_client import append_jsonl, read_jsonl

CHAT = 10000000001


def write_instance(tmp_path, **over):
    """A minimal instance config on disk. Every test gets its own, because under
    the plugin an instance IS its config — there is no ambient state dir to
    redirect."""
    import sys, yaml
    data = {
        "name": "testinst", "label": "Test", "workdir": str(tmp_path / "repo"),
        "python": sys.executable,
        "telegram": {"owner_chat_id": CHAT, "token_file": str(tmp_path / "env")},
        "runtime": {"state_dir": str(tmp_path / "gwstate"),
                    "log_dir": str(tmp_path / "logs")},
    }
    for k, v in over.items():
        data.setdefault(k, {}).update(v) if isinstance(v, dict) else data.__setitem__(k, v)
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    p = tmp_path / "instance.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv(instance_config.CONFIG_ENV, str(write_instance(tmp_path)))


def utf16_len(s):
    """UTF-16 code units of a Python str, the way Telegram counts offsets."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in s)


def build_chunk(doc_specs):
    """Render blocks separated by a blank line; offsets computed, not
    hand-counted. doc_specs = [(reader_id, title, body), ...]."""
    text = ""
    blocks = []
    for reader_id, title, body in doc_specs:
        if text:
            text += "\n\n"
        start = len(text)
        text += body
        blocks.append({"start": start, "end": len(text),
                       "reader_id": reader_id, "title": title})
    return text, blocks


def send_and_annotate(message_id, doc_specs, chat_id=CHAT):
    text, blocks = build_chunk(doc_specs)
    msg_index.append_raw(chat_id, message_id, text)
    msg_index.annotate(chat_id, message_id, blocks)
    return text, blocks


# --- 1. happy path -------------------------------------------------------------

def test_quote_from_block_3_of_8_block_digest_resolves_to_doc_3():
    specs = [(f"doc{i}", f"Title {i}",
              f"{i}. Article number {i} explains subject {i} at length.")
             for i in range(1, 9)]
    send_and_annotate(101, specs)
    res = msg_index.resolve(CHAT, replied_message_id=101,
                            quote_text="subject 3 at length")
    assert res["status"] == "resolved"
    assert res["reader_id"] == "doc3"
    assert res["title"] == "Title 3"
    assert res["candidates"] == []


# --- 2. UTF-16 quote_position ---------------------------------------------------

def test_utf16_offset_to_str_index_handles_emoji_and_cyrillic():
    text = "привет 🚀 мир"
    # "привет " = 7 code points = 7 UTF-16 units (Cyrillic is 1 unit);
    # the rocket emoji is 1 code point but 2 UTF-16 units.
    assert msg_index.utf16_offset_to_str_index(text, 0) == 0
    assert msg_index.utf16_offset_to_str_index(text, 7) == 7    # at the emoji
    assert msg_index.utf16_offset_to_str_index(text, 9) == 8    # right after it
    assert msg_index.utf16_offset_to_str_index(text, 10) == 9   # 'м'
    assert msg_index.utf16_offset_to_str_index(text, 999) == len(text)


def test_duplicate_quote_disambiguated_by_utf16_position():
    phrase = "ключевая мысль автора"
    specs = [
        ("docA", "Статья А", f"🔥 Главное: {phrase} — про рынок."),
        ("docB", "Статья Б", f"Вторая заметка 🚀 повторяет: {phrase} — про код."),
    ]
    text, _ = send_and_annotate(202, specs)

    first_str = text.index(phrase)
    second_str = text.index(phrase, first_str + 1)
    first_u16 = utf16_len(text[:first_str])
    second_u16 = utf16_len(text[:second_str])
    # the emoji make UTF-16 offsets diverge from str indexes — the
    # conversion is load-bearing, not a no-op
    assert first_u16 != first_str
    assert second_u16 != second_str
    assert msg_index.utf16_offset_to_str_index(text, first_u16) == first_str
    assert msg_index.utf16_offset_to_str_index(text, second_u16) == second_str

    res = msg_index.resolve(CHAT, replied_message_id=202,
                            quote_text=phrase, quote_position=second_u16)
    assert res["status"] == "resolved"
    assert res["reader_id"] == "docB"

    res = msg_index.resolve(CHAT, replied_message_id=202,
                            quote_text=phrase, quote_position=first_u16)
    assert res["status"] == "resolved"
    assert res["reader_id"] == "docA"


def test_duplicate_quote_without_position_is_ambiguous():
    phrase = "exactly the same sentence"
    specs = [("docX", "X", f"First block has {phrase} inside."),
             ("docY", "Y", f"Second block also has {phrase} inside.")]
    send_and_annotate(203, specs)
    res = msg_index.resolve(CHAT, replied_message_id=203, quote_text=phrase)
    assert res["status"] == "ambiguous"
    assert res["reader_id"] is None
    assert [c["reader_id"] for c in res["candidates"]] == ["docX", "docY"]


# --- 3. discussion reply (non-digest message) -----------------------------------

def test_quote_of_bot_discussion_reply_resolves_via_index():
    body = "Отличный вопрос — автор утверждает, что внимание дешевле памяти."
    msg_index.append_raw(CHAT, 303, body)
    msg_index.annotate(CHAT, 303, [{"start": 0, "end": len(body),
                                    "reader_id": "doc-disc",
                                    "title": "Attention Costs"}])
    res = msg_index.resolve(CHAT, replied_message_id=303,
                            quote_text="внимание дешевле памяти")
    assert res["status"] == "resolved"
    assert res["reader_id"] == "doc-disc"
    # a plain reply (no quote) to a single-block message also resolves
    res = msg_index.resolve(CHAT, replied_message_id=303)
    assert res["status"] == "resolved"
    assert res["reader_id"] == "doc-disc"


def test_plain_reply_to_multiblock_message_asks_with_candidates_in_block_order():
    specs = [(f"doc{i}", f"T{i}", f"Story {i} body.") for i in range(1, 6)]
    send_and_annotate(304, specs)
    res = msg_index.resolve(CHAT, replied_message_id=304)
    assert res["status"] == "ambiguous"
    assert res["reader_id"] is None
    assert [c["reader_id"] for c in res["candidates"]] == ["doc1", "doc2", "doc3"]


# --- 4. paraphrase never silently resolves ---------------------------------------

def test_paraphrased_quote_asks_instead_of_guessing():
    # doc1 and doc2 are near-duplicates, so no paraphrase can be "clearly
    # ahead" — a silent resolved here would be a guess by construction.
    specs = [
        ("doc1", "Fed 25bp", "The Fed raised interest rates by 25 basis "
                             "points citing sticky inflation pressure."),
        ("doc2", "Fed 50bp", "The Fed raised interest rates by 50 basis "
                             "points citing cooling inflation pressure."),
        ("doc3", "Tomatoes", "Ten tips for growing tomatoes on a small "
                             "balcony during a hot dry summer."),
    ]
    send_and_annotate(404, specs)
    res = msg_index.resolve(
        CHAT, replied_message_id=404,
        quote_text="the federal reserve raised the interest rates citing inflation")
    assert res["status"] in ("ambiguous", "unresolved")
    assert res["reader_id"] is None
    ids = {c["reader_id"] for c in res["candidates"]}
    assert ids & {"doc1", "doc2"}  # the right docs are offered as candidates
    assert "doc3" not in ids


def test_unrelated_quote_is_unresolved_without_candidates():
    send_and_annotate(405, [("doc1", "T1",
                             "The Fed raised interest rates by 25 basis points.")])
    res = msg_index.resolve(CHAT, replied_message_id=405,
                            quote_text="qzx покраска забора жжж 12345 vvv")
    assert res["status"] == "unresolved"
    assert res["reader_id"] is None
    assert res["candidates"] == []


def test_quote_replying_to_unindexed_message_falls_back_to_fuzzy():
    send_and_annotate(406, [("doc-f", "Pelican Economics",
                             "The unique pelican economics essay argued "
                             "that bills beat budgets.")])
    res = msg_index.resolve(CHAT, replied_message_id=999999,
                            quote_text="pelican economics essay argued that "
                                       "bills beat budgets")
    assert res["status"] == "resolved"
    assert res["reader_id"] == "doc-f"


def test_fuzzy_ignores_other_chats_and_old_records():
    # same text in ANOTHER chat must not leak into this chat's resolution
    other_text, other_blocks = build_chunk(
        [("doc-other", "Other", "The unique walrus essay about tusks.")])
    msg_index.append_raw(CHAT + 1, 801, other_text)
    msg_index.annotate(CHAT + 1, 801, other_blocks)
    # a perfect match in THIS chat, but older than the 14-day fuzzy window
    old_ts = (datetime.now() - timedelta(days=20)).isoformat(timespec="seconds")
    body = "The unique walrus essay about tusks."
    path = msg_index.index_path()
    append_jsonl(path, {"type": "raw", "chat_id": CHAT, "message_id": 802,
                        "text": body, "ts": old_ts})
    append_jsonl(path, {"type": "annotation", "chat_id": CHAT,
                        "message_id": 802, "ts": old_ts,
                        "blocks": [{"start": 0, "end": len(body),
                                    "reader_id": "doc-old", "title": "Old"}]})
    res = msg_index.resolve(CHAT, quote_text="unique walrus essay about tusks")
    assert res["status"] == "unresolved"
    assert res["candidates"] == []


# --- 5. digest spanning 3 chunks --------------------------------------------------

def test_digest_spanning_three_chunks_maps_chunk_relative_offsets():
    chunks = [
        [("doc1", "T1", "Alpha story about apples."),
         ("doc2", "T2", "Beta story about bees.")],
        [("doc3", "T3", "Gamma story about gardens."),
         ("doc4", "T4", "Delta story about drones.")],
        [("doc5", "T5", "Epsilon story about engines."),
         ("doc6", "T6", "Zeta story about zebras.")],
    ]
    for i, specs in enumerate(chunks):
        send_and_annotate(501 + i, specs)
    # quote from block 2 of chunk 2 resolves against THAT message's offsets
    res = msg_index.resolve(CHAT, replied_message_id=502,
                            quote_text="Delta story about drones")
    assert res["status"] == "resolved"
    assert res["reader_id"] == "doc4"
    # blocks never split across chunks: every chunk's offsets are
    # self-contained — they start at 0 and end inside the chunk's own text
    for mid in (501, 502, 503):
        msg = msg_index.get_message(CHAT, mid)
        assert msg["blocks"][0]["start"] == 0
        assert all(0 <= b["start"] < b["end"] <= len(msg["text"])
                   for b in msg["blocks"])
    # the same quote via fuzzy (no reply target) still lands on doc4
    res = msg_index.resolve(CHAT, quote_text="Delta story about drones")
    assert res["status"] == "resolved"
    assert res["reader_id"] == "doc4"


# --- 6. restart between send and reply --------------------------------------------

def test_restart_between_send_and_reply_resolves_from_file_alone():
    text, blocks = build_chunk(
        [("doc-r", "Restart Proof", "Persistent story about restarts.")])
    msg_index.append_raw(CHAT, 601, text)
    importlib.reload(msg_index)  # "restart": no in-memory state survives
    msg_index.annotate(CHAT, 601, blocks)
    importlib.reload(msg_index)  # restart again between annotate and reply
    res = msg_index.resolve(CHAT, replied_message_id=601,
                            quote_text="story about restarts")
    assert res["status"] == "resolved"
    assert res["reader_id"] == "doc-r"


def test_resolve_tolerates_partial_trailing_line():
    send_and_annotate(602, [("doc-t", "T", "Tolerant body about journals.")])
    with open(msg_index.index_path(), "a", encoding="utf-8") as f:
        f.write('{"type": "raw", "chat_id"')  # crash mid-write
    res = msg_index.resolve(CHAT, replied_message_id=602,
                            quote_text="body about journals")
    assert res["status"] == "resolved"
    assert res["reader_id"] == "doc-t"


# --- 7. prune ----------------------------------------------------------------------

def test_prune_drops_old_keeps_recent_file_stays_valid_jsonl():
    path = msg_index.index_path()
    old_ts = (datetime.now() - timedelta(days=90)).isoformat(timespec="seconds")
    append_jsonl(path, {"type": "raw", "chat_id": CHAT, "message_id": 1,
                        "text": "old", "ts": old_ts})
    append_jsonl(path, {"type": "annotation", "chat_id": CHAT, "message_id": 1,
                        "blocks": [{"start": 0, "end": 3,
                                    "reader_id": "d", "title": "t"}],
                        "ts": old_ts})
    msg_index.append_raw(CHAT, 2, "recent message")
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"partial')  # prune also repairs a crashed tail
    dropped = msg_index.prune(retention_days=60)
    assert dropped == 2
    rows = read_jsonl(path)
    assert [r["message_id"] for r in rows] == [2]
    # file is clean line-delimited JSON again (no partial tail left behind)
    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert all(line.strip() for line in raw.splitlines())


def test_prune_default_retention_comes_from_config(tmp_path, monkeypatch):
    """Retention used to be read from the reading instance's config.yaml under
    an `index:` key no other instance has — the nightly prune would have died
    with a KeyError on instance two. It comes from the instance config now."""
    monkeypatch.setenv(instance_config.CONFIG_ENV, str(write_instance(
        tmp_path, housekeeping={"msg_index_retention_days": 60})))
    path = msg_index.index_path()
    old_ts = (datetime.now() - timedelta(days=61)).isoformat(timespec="seconds")
    append_jsonl(path, {"type": "raw", "chat_id": CHAT, "message_id": 1,
                        "text": "old", "ts": old_ts})
    msg_index.append_raw(CHAT, 2, "recent")
    assert msg_index.prune() == 1
    assert [r["message_id"] for r in read_jsonl(path)] == [2]


def test_prune_missing_file_is_a_noop():
    assert msg_index.prune(retention_days=60) == 0


# --- 8. drift report -----------------------------------------------------------------

def test_drift_report_flags_raw_without_annotation_within_48h():
    msg_index.append_raw(CHAT, 11, "annotated message")
    msg_index.annotate(CHAT, 11, [{"start": 0, "end": 9,
                                   "reader_id": "d", "title": "t"}])
    msg_index.append_raw(CHAT, 12, "orphan message")
    old_ts = (datetime.now() - timedelta(hours=72)).isoformat(timespec="seconds")
    append_jsonl(msg_index.index_path(),
                 {"type": "raw", "chat_id": CHAT, "message_id": 13,
                  "text": "old orphan", "ts": old_ts})
    assert msg_index.drift_report() == [{"chat_id": CHAT, "message_id": 12}]


def test_drift_report_clears_after_late_annotation():
    msg_index.append_raw(CHAT, 21, "late-annotated message")
    assert msg_index.drift_report() == [{"chat_id": CHAT, "message_id": 21}]
    msg_index.annotate(CHAT, 21, [{"start": 0, "end": 4,
                                   "reader_id": "d", "title": "t"}])
    assert msg_index.drift_report() == []


# --- record hygiene -------------------------------------------------------------------

def test_get_message_returns_merged_view_or_none():
    assert msg_index.get_message(CHAT, 42) is None
    text, blocks = send_and_annotate(42, [("d1", "T", "Body text here.")])
    msg = msg_index.get_message(CHAT, 42)
    assert msg == {"text": text, "blocks": blocks}


def test_annotate_rejects_malformed_blocks_loudly():
    with pytest.raises(ValueError):
        msg_index.annotate(CHAT, 44, [{"start": 5, "end": 2,
                                       "reader_id": "d", "title": "t"}])
    with pytest.raises(ValueError):
        msg_index.annotate(CHAT, 44, [{"start": 0, "end": 5, "title": "t"}])
    assert read_jsonl(msg_index.index_path()) == []  # nothing was appended


def test_reannotation_last_record_wins():
    msg_index.append_raw(CHAT, 45, "0123456789")
    msg_index.annotate(CHAT, 45, [{"start": 0, "end": 10,
                                   "reader_id": "wrong", "title": "W"}])
    msg_index.annotate(CHAT, 45, [{"start": 0, "end": 10,
                                   "reader_id": "right", "title": "R"}])
    res = msg_index.resolve(CHAT, replied_message_id=45)
    assert res["status"] == "resolved"
    assert res["reader_id"] == "right"


def test_plain_reply_to_unindexed_message_is_unresolved():
    res = msg_index.resolve(CHAT, replied_message_id=777)
    assert res == {"status": "unresolved", "reader_id": None,
                   "title": None, "candidates": []}
