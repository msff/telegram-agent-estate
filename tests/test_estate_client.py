"""Generic plumbing: state roots, journals, conflict guard, activity flag.

The JSONL primitives came over verbatim, so these tests mostly re-prove the
guards that earned their place (inode swap under flock, partial trailing line)
against the transplanted copy. The rest cover what the move actually changed:
nothing resolves against a module-level repo root any more.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest
import yaml

import estate_client as ec
import instance_config as ic


def make_cfg(tmp_path, name="alpha"):
    root = tmp_path / name
    data = {
        "name": name,
        "label": f"{name} bot",
        "workdir": str(root / "repo"),
        "python": sys.executable,
        "telegram": {"owner_chat_id": 111, "token_file": str(root / "env")},
        "runtime": {"state_dir": str(root / "gwstate"),
                    "log_dir": str(root / "logs")},
    }
    (root / "repo").mkdir(parents=True)
    p = root / "instance.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return ic.load(p)


def test_state_roots_are_distinct_and_follow_the_config(tmp_path):
    """The two roots are genuinely different things — synced agent memory vs
    non-synced machine plumbing — and conflating them puts a lock file on a
    sync volume, where it is not a lock."""
    cfg = make_cfg(tmp_path)
    assert ec.agent_state_dir(cfg) != ec.gateway_state_dir(cfg)
    assert ec.agent_state_dir(cfg).is_dir()
    assert ec.gateway_state_dir(cfg).is_dir()
    assert ec.gateway_state_dir(cfg) not in ec.agent_state_dir(cfg).parents


def test_two_instances_get_separate_roots(tmp_path):
    a, b = make_cfg(tmp_path, "alpha"), make_cfg(tmp_path, "beta")
    assert ec.agent_state_dir(a) != ec.agent_state_dir(b)
    assert ec.gateway_state_dir(a) != ec.gateway_state_dir(b)


def test_load_secrets_reads_the_instance_token_file(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.token_file.write_text(
        '# comment\nTELEGRAM_BOT_TOKEN="abc123"\nOTHER=plain\n\n', encoding="utf-8")
    got = ec.load_secrets(cfg=cfg)
    assert got["TELEGRAM_BOT_TOKEN"] == "abc123"   # quotes stripped
    assert got["OTHER"] == "plain"


def test_load_secrets_only_promotes_named_env_keys(tmp_path, monkeypatch):
    """The old version hardcoded a reading-specific key list, which silently
    dropped every other instance's credentials. Callers name their own now."""
    cfg = make_cfg(tmp_path)
    cfg.token_file.write_text("TELEGRAM_BOT_TOKEN=from-file\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-env")
    monkeypatch.setenv("UNRELATED_SECRET", "nope")

    assert ec.load_secrets(cfg=cfg)["TELEGRAM_BOT_TOKEN"] == "from-file"
    named = ec.load_secrets(cfg=cfg, keys=("TELEGRAM_BOT_TOKEN",))
    assert named["TELEGRAM_BOT_TOKEN"] == "from-env"     # real env wins when named
    assert "UNRELATED_SECRET" not in named


def test_missing_token_file_is_not_an_error(tmp_path):
    """A first run has no env file yet; the caller decides whether that is
    fatal, not the loader."""
    assert ec.load_secrets(cfg=make_cfg(tmp_path)) == {}


def test_interactive_flag_ttl_is_required_not_config_derived(tmp_path):
    """It used to default to a lookup into the reading instance's config.yaml —
    a shape no other instance has, so the default would raise KeyError on the
    second instance's first batch job."""
    cfg = make_cfg(tmp_path)
    with pytest.raises(TypeError):
        ec.interactive_flag_fresh(cfg=cfg)          # ttl is positional-required

    assert ec.interactive_flag_fresh(60, cfg=cfg) is False
    ec.refresh_interactive_flag(cfg)
    assert ec.interactive_flag_fresh(60, cfg=cfg) is True
    assert ec.interactive_flag_fresh(0, cfg=cfg) is False


def test_conflict_guard_flags_and_exits(tmp_path):
    cfg = make_cfg(tmp_path)
    target = ec.agent_state_dir(cfg) / "notes.md"
    target.write_text("real", encoding="utf-8")
    (ec.agent_state_dir(cfg) / "notes (conflicted copy).md").write_text("x", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        ec.check_no_conflicts([target], job_name="drill", cfg=cfg)
    assert exc.value.code == 2
    flag = ec.agent_state_dir(cfg) / "conflict-detected"
    assert "drill" in flag.read_text(encoding="utf-8")


def test_conflict_guard_is_silent_when_clean(tmp_path):
    cfg = make_cfg(tmp_path)
    target = ec.agent_state_dir(cfg) / "notes.md"
    target.write_text("real", encoding="utf-8")
    ec.check_no_conflicts([target], job_name="drill", cfg=cfg)   # must not raise
    assert not (ec.agent_state_dir(cfg) / "conflict-detected").exists()


def test_append_and_read_roundtrip(tmp_path):
    p = tmp_path / "j.jsonl"
    ec.append_jsonl(p, {"a": 1})
    ec.append_jsonl(p, {"b": "два"})            # non-ASCII must survive
    assert ec.read_jsonl(p) == [{"a": 1}, {"b": "два"}]


def test_read_jsonl_tolerates_only_a_trailing_partial_line(tmp_path):
    """A crash can truncate the LAST line and only the last. Corruption further
    up is real and must not be swallowed."""
    p = tmp_path / "j.jsonl"
    p.write_text('{"a": 1}\n{"b": 2}\n{"c": ', encoding="utf-8")
    assert ec.read_jsonl(p) == [{"a": 1}, {"b": 2}]

    p.write_text('{"a": 1}\n{"BROKEN\n{"c": 3}\n', encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        ec.read_jsonl(p)


def test_read_jsonl_on_a_missing_file_is_empty(tmp_path):
    assert ec.read_jsonl(tmp_path / "nope.jsonl") == []


def test_append_survives_a_concurrent_inode_swap(tmp_path):
    """The guard that looks paranoid and is not: a compactor doing
    temp-file + os.replace can swap the inode while a writer is blocked on
    flock. Appending to the orphaned inode loses the record silently."""
    p = tmp_path / "j.jsonl"
    ec.append_jsonl(p, {"n": 0})

    # Swap the inode behind an existing path, exactly as a compactor would.
    tmp = tmp_path / "j.new"
    tmp.write_text('{"n": 0}\n{"compacted": true}\n', encoding="utf-8")
    os.replace(tmp, p)

    ec.append_jsonl(p, {"n": 1})
    rows = ec.read_jsonl(p)
    assert {"compacted": True} in rows and {"n": 1} in rows


def test_concurrent_appends_do_not_interleave(tmp_path):
    """Twenty writers, one file, no torn lines. O_APPEND + flock + a single
    os.write per record is what makes that hold."""
    p = tmp_path / "j.jsonl"
    binpath = str(ic.Path(__file__).resolve().parent.parent / "bin")
    prog = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {binpath!r})
        import estate_client as ec
        for i in range(25):
            ec.append_jsonl({str(p)!r}, {{"w": sys.argv[1], "i": i, "pad": "x" * 300}})
    """)
    procs = [subprocess.Popen([sys.executable, "-c", prog, str(w)]) for w in range(20)]
    for proc in procs:
        assert proc.wait() == 0

    rows = ec.read_jsonl(p)                       # raises on any torn line
    assert len(rows) == 20 * 25
