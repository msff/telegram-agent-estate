"""Instance config — the seam that has to keep two bots from becoming one.

These tests are mostly about ISOLATION, not parsing. Under the plugin both
instances execute the same files, so every name that used to be distinct by
virtue of living in a different checkout now has to be manufactured from the
instance config. A parsing bug here is an inconvenience; an isolation bug here
is one bot killing the other's poller.
"""
import textwrap

import pytest
import yaml

import instance_config as ic


MINIMAL = {
    "name": "alpha",
    "label": "Alpha Bot",
    "workdir": "/tmp/alpha",
    "python": "/usr/bin/python3",
    "telegram": {"owner_chat_id": 111, "token_file": "~/.config/alpha/env"},
    "runtime": {"state_dir": "~/.local/state/alpha", "log_dir": "~/logs/alpha"},
}


def write_cfg(tmp_path, data, name="instance.yaml"):
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_load_requires_an_explicit_config(monkeypatch):
    """No default location, on purpose: an instance that cannot say who it is
    would otherwise silently adopt whichever config happened to be found —
    i.e. another instance's state dir and bot token."""
    monkeypatch.delenv(ic.CONFIG_ENV, raising=False)
    with pytest.raises(ic.ConfigError, match="no instance config"):
        ic.load()


def test_load_reads_the_env_var(tmp_path, monkeypatch):
    p = write_cfg(tmp_path, MINIMAL)
    monkeypatch.setenv(ic.CONFIG_ENV, str(p))
    assert ic.load().name == "alpha"


def test_missing_required_fields_fail_loudly(tmp_path):
    broken = {k: v for k, v in MINIMAL.items() if k != "python"}
    with pytest.raises(ic.ConfigError, match="python"):
        ic.load(write_cfg(tmp_path, broken))


def test_malformed_yaml_names_the_file(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("name: [unclosed\n", encoding="utf-8")
    with pytest.raises(ic.ConfigError, match="not valid YAML"):
        ic.load(p)


def test_home_relative_paths_are_expanded(tmp_path):
    """`~/.config/...` left unexpanded becomes a literal `./~` directory —
    silent, wrong, and writable."""
    cfg = ic.load(write_cfg(tmp_path, MINIMAL))
    assert "~" not in str(cfg.token_file)
    assert cfg.token_file.is_absolute()
    assert cfg.gateway_state_dir.is_absolute()


def test_a_name_with_a_slash_is_rejected(tmp_path):
    """The name becomes a launchd label, a lock filename, and a pgrep pattern.
    A slash in any of those produces a different kind of wrong in each."""
    bad = dict(MINIMAL, name="alpha/beta")
    with pytest.raises(ic.ConfigError, match="bare slug"):
        ic.load(write_cfg(tmp_path, bad))


def test_handoff_is_relative_to_workdir_not_the_state_dir(tmp_path):
    """The handoff is the agent's memory and lives in the repo it thinks in.
    Resolving it against the state dir would put two instances' memories in
    different trees than their CLAUDE.md, and the flush turn would write to a
    file nothing reads."""
    cfg = ic.load(write_cfg(tmp_path, MINIMAL))
    assert cfg.handoff_path == cfg.workdir / "state/session-handoff.md"


def test_absolute_handoff_overrides_the_workdir_join(tmp_path):
    data = dict(MINIMAL, housekeeping={"handoff_file": "/tmp/elsewhere/h.md"})
    cfg = ic.load(write_cfg(tmp_path, data))
    assert str(cfg.handoff_path) == "/tmp/elsewhere/h.md"


# --- the isolation properties -------------------------------------------------

def two_instances(tmp_path):
    a = ic.load(write_cfg(tmp_path, MINIMAL, "a.yaml"))
    beta = dict(
        MINIMAL, name="beta", label="Beta Bot", workdir="/tmp/beta",
        telegram={"owner_chat_id": 222, "token_file": "~/.config/beta/env"},
        runtime={"state_dir": "~/.local/state/beta", "log_dir": "~/logs/beta"},
    )
    return a, ic.load(write_cfg(tmp_path, beta, "b.yaml"))


def test_two_instances_share_no_derived_name(tmp_path):
    """The U13 gate, in miniature. Every name that a sweep, lock, or probe keys
    on must differ between two instances — because under the plugin they no
    longer differ by executable path."""
    a, b = two_instances(tmp_path)
    for attr in ("gateway_state_dir", "agent_state_dir", "log_dir", "token_file", "handoff_path", "workdir"):
        assert getattr(a, attr) != getattr(b, attr), attr
    assert a.instance_tag() != b.instance_tag()
    assert a.poller_match() != b.poller_match()
    assert a.launchd_label() != b.launchd_label()
    assert a.launchd_label("morning") != b.launchd_label("morning")
    assert a.owner_chat_id != b.owner_chat_id


def test_neither_instances_poller_pattern_matches_the_others_argv(tmp_path):
    """Not just "the patterns differ" — the pattern must not MATCH the other
    instance's real command line. Two instances now run the same poller.py, so
    a probe keyed on the script path matches both and a supervisor restarting
    its own poller reaps its neighbour's."""
    import re
    a, b = two_instances(tmp_path)
    argv_a = f"/usr/bin/python3 /opt/estate/libexec/poller.py {a.instance_tag()}"
    argv_b = f"/usr/bin/python3 /opt/estate/libexec/poller.py {b.instance_tag()}"

    assert re.search(a.poller_match(), argv_a)
    assert re.search(b.poller_match(), argv_b)
    assert not re.search(a.poller_match(), argv_b)
    assert not re.search(b.poller_match(), argv_a)


def test_poller_pattern_does_not_match_a_bare_mention_of_the_instance(tmp_path):
    """A supervisor's own grep, an editor, or a log tail whose argv merely
    contains the instance name must not read as a live poller. The reading
    instance shipped exactly this bug: a probe matching a bare string counted
    the grep that was looking for it."""
    import re
    a, _ = two_instances(tmp_path)
    for impostor in (
        f"grep -f {a.instance_tag()}",
        f"tail -f /var/log/{a.name}.log",
        f"vim /opt/estate/libexec/poller.py",
    ):
        assert not re.search(a.poller_match(), impostor), impostor


def test_defaults_do_not_leak_another_instances_identity(tmp_path):
    """Optional fields fall back to values derived from THIS instance, never to
    a shared global. channel_state_dir defaulting to a common directory would
    hand a turn the other bot's token — the 409 poller war."""
    a, b = two_instances(tmp_path)
    assert a.channel_state_dir == a.log_dir
    assert a.channel_state_dir != b.channel_state_dir
