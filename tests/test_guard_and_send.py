"""Scheduling safety (guard.py) and the outbound send primitive (send.py).

Only the generic half of the first instance's original combined send/guard
suite came over. The rest of that file tests its digest transaction — chunked
resume, tag swaps, per-item deep links — which stayed in that instance's own
repo along with the code.

The isolation tests at the bottom are new, and they are the ones that matter
for U13: markers and leases used to be distinct because each instance resolved
them against its own checkout.
"""
import sys
from types import SimpleNamespace

import pytest
import yaml

import guard
import instance_config
import msg_index
import send

# A chat id that belongs to nobody. The author's real owner id used to sit
# here, which reads as a neutral fixture value right up until a stranger
# runs the suite against their own instance.
CHAT = 10_000_000_001


def make_cfg(tmp_path, name="alpha", chat=CHAT):
    root = tmp_path / name
    (root / "repo" / "state").mkdir(parents=True)
    data = {
        "name": name, "label": f"{name} bot", "workdir": str(root / "repo"),
        "python": sys.executable,
        "telegram": {"owner_chat_id": chat, "token_file": str(root / "env")},
        "runtime": {"state_dir": str(root / "gw"), "log_dir": str(root / "logs")},
    }
    p = root / "instance.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return instance_config.load(p)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = make_cfg(tmp_path)
    monkeypatch.setenv(instance_config.CONFIG_ENV, str(c.source))
    return c


# --- the guard ----------------------------------------------------------------

def test_guard_marker_lease_fresh_and_stale(cfg):
    import time
    from datetime import datetime

    assert guard.guard("digest", cfg=cfg) == guard.GUARD_OK

    lease = guard.lease_path("digest", cfg)
    lease.write_text("x", encoding="utf-8")
    assert guard.guard("digest", cfg=cfg) == guard.GUARD_LEASE_FRESH

    # A STALE lease must NOT block: it means a session died mid-compose, and
    # the retry is exactly what should re-enter.
    import os
    old = time.time() - 40 * 60
    os.utime(lease, (old, old))
    assert guard.guard("digest", lease_stale_minutes=20, cfg=cfg) == guard.GUARD_OK

    today = datetime.now().strftime("%Y-%m-%d")
    guard.marker_path("digest", today, cfg).write_text("done", encoding="utf-8")
    assert guard.guard("digest", cfg=cfg) == guard.GUARD_MARKER


def test_guard_not_before_suppresses_a_wake_catch_up(cfg):
    """launchd fires a job missed during sleep ONCE on wake. A 00:30 catch-up of
    the 20:30 job would run immediately and write the NEW day's marker, so that
    evening's real fire no-ops — a full day skipped, with nothing in any log
    that says why."""
    assert guard.guard("evening", not_before="23:59", cfg=cfg) == guard.GUARD_TOO_EARLY
    assert guard.guard("evening", not_before="00:00", cfg=cfg) == guard.GUARD_OK


# --- the send primitive -------------------------------------------------------

def test_bot_api_sender_payload_shape_and_failure(cfg, monkeypatch):
    cfg.token_file.write_text("TELEGRAM_BOT_TOKEN=123:ABC\n", encoding="utf-8")
    posts = []

    def fake_post(url, json=None, timeout=None):
        posts.append((url, json, timeout))
        return SimpleNamespace(status_code=200, text="",
                               json=lambda: {"ok": True, "result": {"message_id": 777}})

    monkeypatch.setattr(send.requests, "post", fake_post)
    assert send.bot_api_sender(CHAT, "hello", cfg=cfg) == 777
    url, body, timeout = posts[0]
    assert url == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert body == {"chat_id": CHAT, "text": "hello", "disable_web_page_preview": True}
    assert "parse_mode" not in body           # plain text only
    assert timeout == 30

    monkeypatch.setattr(send.requests, "post", lambda *a, **k: SimpleNamespace(
        status_code=400, text="Bad Request: chat not found", json=lambda: {"ok": False}))
    with pytest.raises(send.SendError):
        send.bot_api_sender(CHAT, "hello", cfg=cfg)


def test_missing_token_names_the_file_it_expected(cfg, monkeypatch):
    """The old error told the reader to look in one hardcoded instance's path,
    which on any other instance is the wrong file."""
    with pytest.raises(send.SendError, match=str(cfg.token_file)):
        send.bot_api_sender(CHAT, "hi", cfg=cfg)


# Token-shaped, and assembled at runtime: written out as one literal it would
# trip this repo's own secret scan, which is exactly what it imitates.
FAKE_TOKEN = "1" * 10 + ":" + "A" * 35


def _unreachable(url, **kwargs):
    """What `requests` raises when DNS is down: its text repeats the URL."""
    raise send.requests.ConnectionError(
        "HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries "
        f"exceeded with url: {url.split('api.telegram.org', 1)[1]}")


def test_a_network_failure_never_carries_the_token(cfg, monkeypatch, capsys):
    """The Bot API URL holds the token, and `requests` repeats that URL in its
    connection errors. Passed on as is, a DNS outage put the token into every
    log line and traceback that printed the error."""
    cfg.token_file.write_text(f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\n", encoding="utf-8")
    monkeypatch.setattr(send.requests, "post", _unreachable)

    with pytest.raises(send.SendError) as raised:
        send.bot_api_sender(CHAT, "hello", cfg=cfg)
    assert FAKE_TOKEN not in str(raised.value)
    assert "/bot<redacted>/sendMessage" in str(raised.value)
    assert raised.value.__suppress_context__   # no traceback prints the original

    assert send.main(["hello"]) == 1           # the CLI used to die on a traceback
    out = capsys.readouterr()
    assert FAKE_TOKEN not in out.out + out.err
    assert "send: sendMessage failed" in out.err


def test_a_failed_owner_alert_logs_no_token(cfg, monkeypatch, capsys):
    """The path that actually leaked: a turn fails, the runner alerts the owner,
    the alert fails too, and the runner logs why."""
    import turn_runner
    cfg.token_file.write_text(f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\n", encoding="utf-8")
    monkeypatch.setattr(send.requests, "post", _unreachable)

    turn_runner._default_alert("a turn failed")
    out = capsys.readouterr().out
    assert "owner alert failed" in out
    assert FAKE_TOKEN not in out


def test_reply_refuses_non_owner_chat(cfg, monkeypatch):
    """A prompt-injected session that talks itself into `reply --chat-id
    <foreign>` must not reach the network at all."""
    sent = []
    monkeypatch.setattr(msg_index, "append_raw",
                        lambda *a, **k: sent.append("journaled"))
    with pytest.raises(send.SendError, match="not on the owner allowlist"):
        send.reply("exfil", chat_id=999999, cfg=cfg,
                   sender=lambda cid, t: sent.append(cid) or 1)
    assert sent == []                        # nothing sent, nothing journaled


def test_reply_to_owner_is_allowed(cfg, monkeypatch):
    monkeypatch.setattr(msg_index, "append_raw", lambda *a, **k: None)
    assert send.reply("hi", chat_id=CHAT, cfg=cfg, sender=lambda cid, t: 4321) == 4321


def test_reply_stamps_turn_id_from_env(cfg, monkeypatch):
    """The stamp is the delivery evidence the runner's retry-safety check reads.
    Without it a turn that sent and then died looks identical to one that never
    sent, and the retry double-sends."""
    monkeypatch.setenv("ESTATE_TURN_ID", "turn-abc")
    recorded = {}
    monkeypatch.setattr(msg_index, "append_raw",
                        lambda cid, mid, text, **k: recorded.update(k))
    send.reply("hi", chat_id=CHAT, cfg=cfg, sender=lambda cid, t: 7777)
    assert recorded.get("meta") == {"turn_id": "turn-abc"}


def test_reply_without_turn_id_env_stamps_no_meta(cfg, monkeypatch):
    monkeypatch.delenv("ESTATE_TURN_ID", raising=False)
    recorded = {}
    monkeypatch.setattr(msg_index, "append_raw",
                        lambda cid, mid, text, **k: recorded.update(k))
    send.reply("hi", chat_id=CHAT, cfg=cfg, sender=lambda cid, t: 7777)
    assert recorded.get("meta") is None


def test_render_html_escapes_and_anchorizes():
    assert send.render_html("a & b <c>") == "a &amp; b &lt;c&gt;"
    got = send.render_html("see https://example.com/x now")
    assert got == 'see <a href="https://example.com/x">https://example.com/x</a> now'
    labelled = send.render_html("see https://example.com/x", link_label="»читать")
    assert '>»читать</a>' in labelled


# --- isolation: the U13 property ----------------------------------------------

def test_two_instances_do_not_share_markers_or_leases(tmp_path):
    """The quiet catastrophe. "morning-digest" is not an unusual job name, and
    under a shared marker root instance A reads B's marker, concludes its own
    work is done, and goes silent for the day with every process healthy and
    every log clean."""
    a, b = make_cfg(tmp_path, "alpha"), make_cfg(tmp_path, "beta", chat=222)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")

    guard.marker_path("morning-digest", today, a).write_text("done", encoding="utf-8")
    guard.lease_path("morning-digest", a).write_text("busy", encoding="utf-8")

    assert guard.guard("morning-digest", cfg=a) == guard.GUARD_MARKER
    assert guard.guard("morning-digest", cfg=b) == guard.GUARD_OK   # unaffected
    assert not guard.marker_path("morning-digest", today, b).exists()
    assert not guard.lease_path("morning-digest", b).exists()


def test_an_instance_cannot_send_into_the_other_instances_chat(tmp_path):
    """Outbound allowlists are per instance, so a misconfigured or injected A
    cannot reach B's owner chat even though both share this code."""
    a = make_cfg(tmp_path, "alpha", chat=111)
    b = make_cfg(tmp_path, "beta", chat=222)
    assert send.allowed_chat_ids(a) == {111}
    assert send.allowed_chat_ids(b) == {222}
    with pytest.raises(send.SendError):
        send.reply("cross-talk", chat_id=222, cfg=a, sender=lambda cid, t: 1)


def test_payload_dirs_are_per_instance(tmp_path):
    a, b = make_cfg(tmp_path, "alpha"), make_cfg(tmp_path, "beta", chat=222)
    guard.persist_payload({"payload_id": "digest-1", "chunks": ["x"]}, cfg=a)
    assert (guard.payloads_dir(a) / "digest-1.json").exists()
    assert not (guard.payloads_dir(b) / "digest-1.json").exists()
