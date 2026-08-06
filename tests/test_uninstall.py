"""Teardown: stop the instance, remove what was installed, keep what was theirs.

An uninstaller is the one command in this plugin whose bugs are unrecoverable.
Everything else fails by not doing something; this fails by deleting a thing
that is gone. So the assertions below are mostly about *restraint* — what it
declines to remove, and what it refuses to remove at all:

  1. **The default is a plan.** Without `--yes` nothing is touched, and the exit
     code distinguishes "I printed a plan because you asked" (`--dry-run`, 0)
     from "I printed a plan instead of acting" (3). A caller that cannot tell
     those apart will eventually treat the second as success.

  2. **A neighbour's plist is never removed.** Two instances that share a `name`
     is the failure mode the installer already refuses to create; the
     uninstaller has to refuse the mirror image, because here the damage is a
     deletion rather than an overwrite.

  3. **State and secrets survive by default.** The receipt WAL holds messages
     nobody answered and the send journal is how a retry knows not to send
     twice. Deleting them is a decision with its own flag.

  4. **The workdir is never collateral.** The only bytes it may change in the
     user's repo are between the two marker pairs, and `test_claude_md_block.py`
     owns that guarantee — what is asserted here is that the uninstaller calls
     it, that `--keep-claude-md` stops it, and that the handoff file is still
     there afterwards.

  5. **It still works after the venv is gone.** Deleting the venv is a natural
     first move when tearing something down, and an uninstaller that then cannot
     unload the launchd jobs leaves a timer running with no way to stop it.

The processes half is deliberately not simulated: `pgrep`/`pkill` against a real
process tree is not something a test should manufacture on a machine that is
running two live instances. What IS asserted is the pattern the script would
match on, because a pattern keyed to a path under `libexec/` rather than to the
instance tag is how one instance's teardown kills its neighbour's poller.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import instance_config

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
LIBEXEC = PLUGIN_ROOT / "libexec"
UNINSTALL = LIBEXEC / "uninstall-instance.sh"
INSTALL = LIBEXEC / "install-instance.sh"
# Each block's content must be its OWN template: the editor refuses content
# carrying a marker for a different block, which is exactly what handing it the
# ops template as a brief body would do.
BLOCK_TEMPLATE = {
    "ops": PLUGIN_ROOT / "templates" / "CLAUDE-ops-section.md",
    "brief": PLUGIN_ROOT / "templates" / "CLAUDE-agent-brief.md",
}

# Nothing here may collide with a real instance on the machine running the
# suite: the script derives `pkill` patterns from the instance name, so a
# scratch config that borrowed a live instance's name would aim them at a live
# poller. Hence a name no one would choose.
SCRATCH = "estate-teardown-probe"

PROSE_BEFORE = "# My project\n\nMy own instructions, which predate this plugin.\n\n"
PROSE_AFTER = "\n## Something I wrote afterwards\n\nAlso mine.\n"


def run(argv, agents=None, **kw):
    env = {**os.environ}
    if agents is not None:
        env["ESTATE_LAUNCH_AGENTS"] = str(agents)
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                          timeout=120, env=env, **kw)


def combined(res):
    return res.stdout + res.stderr


class Instance:
    """A complete instance on disk, with its jobs installed into a scratch
    LaunchAgents directory.

    `--no-load` throughout: these plists are rendered and inspected, never
    handed to the machine's real launchd.
    """

    def __init__(self, root, name=SCRATCH, python=None, with_claude_md=True):
        self.root = Path(root)
        self.name = name
        (self.root / "repo" / "prompts").mkdir(parents=True, exist_ok=True)
        (self.root / "repo" / "state").mkdir(parents=True, exist_ok=True)
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        (self.root / "gw").mkdir(parents=True, exist_ok=True)
        (self.root / "repo" / "prompts" / "job.txt").write_text("do it\n", encoding="utf-8")

        self.handoff = self.root / "repo" / "state" / "session-handoff.md"
        self.handoff.write_text("## Current state (as of 2026-08-06)\n\nremembered\n",
                                encoding="utf-8")

        self.token_file = self.root / "env"
        self.token_file.write_text("TELEGRAM_BOT_TOKEN=fake\n", encoding="utf-8")
        os.chmod(self.token_file, 0o600)

        self.settings_file = self.root / "permissions.json"
        self.settings_file.write_text('{"permissions": {"defaultMode": "dontAsk"}}\n',
                                      encoding="utf-8")

        self.claude_md = self.root / "repo" / "CLAUDE.md"
        if with_claude_md:
            self.claude_md.write_text(PROSE_BEFORE, encoding="utf-8")

        self.state_dir = self.root / "gw"
        self.log_dir = self.root / "logs"
        self.workdir = self.root / "repo"

        data = {
            "name": name,
            "label": "Teardown Probe",
            "launchd_prefix": "co.example.test",
            "workdir": str(self.workdir),
            "python": python or sys.executable,
            "telegram": {"owner_chat_id": 10_000_000_009,
                         "token_file": str(self.token_file)},
            "runtime": {"state_dir": str(self.state_dir), "log_dir": str(self.log_dir)},
            "permissions": {"mode": "dontAsk",
                            "settings_file": str(self.settings_file)},
            "schedules": [{"job": "morning-digest", "hour": 9, "minute": 0,
                           "prompt_file": "prompts/job.txt"}],
        }
        self.config = self.root / f"{name}.yaml"
        # width= so a long venv path is never folded onto a second line: the sed
        # reader the degraded path depends on reads one line and stops.
        self.config.write_text(yaml.safe_dump(data, width=10_000), encoding="utf-8")

        self.agents = self.root / "agents"
        self.agents.mkdir(exist_ok=True)

    def install(self):
        res = run([INSTALL, self.config, "--no-load"], agents=self.agents)
        assert res.returncode == 0, combined(res)
        return sorted(self.agents.glob("*.plist"))

    def add_blocks(self, *names):
        """Put real marker blocks into the CLAUDE.md, the way onboarding does."""
        body = self.claude_md.read_text(encoding="utf-8")
        if PROSE_AFTER not in body:
            self.claude_md.write_text(body + PROSE_AFTER, encoding="utf-8")
        args = ["%s=%s" % (n, BLOCK_TEMPLATE[n]) for n in names]
        res = run([sys.executable, LIBEXEC / "claude-md-block.py", "set",
                   self.claude_md] + args)
        assert res.returncode == 0, combined(res)

    def uninstall(self, *flags):
        return run([UNINSTALL, self.config, *flags], agents=self.agents)

    @property
    def plists(self):
        return sorted(p.name for p in self.agents.glob("*.plist"))


@pytest.fixture
def inst(tmp_path):
    return Instance(tmp_path / "inst")


# --- 1. the default is a plan, and the two plans are distinguishable ----------

def test_without_yes_it_prints_a_plan_and_changes_nothing(inst):
    inst.install()
    before = inst.plists
    assert before, "nothing was installed, so the test proves nothing"

    res = inst.uninstall()

    assert res.returncode == 3, combined(res)
    assert "Re-run with --yes" in combined(res)
    assert inst.plists == before, "a plan removed a plist"


def test_dry_run_prints_the_same_plan_and_exits_zero(inst):
    inst.install()
    before = inst.plists

    res = inst.uninstall("--dry-run")

    assert res.returncode == 0, combined(res)
    assert inst.plists == before
    for name in before:
        assert name in combined(res), "the plan does not name what it would remove"


def test_the_two_no_op_exits_are_different_codes(inst):
    """One is an answer, the other is a refusal. A caller that cannot tell them
    apart will read "I did nothing because you did not confirm" as success."""
    inst.install()
    assert inst.uninstall("--dry-run").returncode == 0
    assert inst.uninstall().returncode == 3


# --- 2. it refuses to tear down a neighbour ----------------------------------

def test_a_plist_belonging_to_another_config_is_refused(tmp_path):
    """The installer's clobber check, mirrored. Two instances sharing a name is
    a state the installer refuses to create — but it can still be reached by
    hand, and here the consequence is a deletion rather than an overwrite."""
    a = Instance(tmp_path / "a")
    a.install()
    before = a.plists

    # A second config, same name and prefix, different file: what the installer
    # would have refused to install over.
    other = Instance(tmp_path / "b")
    res = a.uninstall()  # sanity: it would otherwise proceed
    assert res.returncode == 3

    # Re-stamp a's plists as if they belonged to b.
    for p in a.agents.glob("*.plist"):
        p.write_text(p.read_text(encoding="utf-8").replace(str(a.config),
                                                           str(other.config)),
                     encoding="utf-8")

    res = a.uninstall("--yes")

    assert res.returncode == 4, combined(res)
    assert "REFUSED" in combined(res)
    assert str(other.config) in combined(res), "the refusal does not name the owner"
    assert a.plists == before, "a refusal still deleted something"


# --- 3. what --yes actually removes ------------------------------------------

def test_yes_removes_every_plist_it_installed(inst):
    inst.install()
    assert inst.plists

    res = inst.uninstall("--yes")

    assert res.returncode == 0, combined(res)
    assert inst.plists == []


def test_a_job_plist_orphaned_by_a_config_edit_is_still_removed(inst):
    """The plists are found by glob, not by re-reading `schedules:`.

    A job deleted from the config after install keeps its plist, keeps firing,
    and is invisible to anything that trusts the config to describe the machine.
    That orphan is the most likely thing to outlive a teardown.
    """
    inst.install()
    data = yaml.safe_load(inst.config.read_text(encoding="utf-8"))
    data["schedules"] = []
    inst.config.write_text(yaml.safe_dump(data, width=10_000), encoding="utf-8")

    res = inst.uninstall("--yes")

    assert res.returncode == 0, combined(res)
    assert inst.plists == [], "an orphaned job plist survived the uninstall"


def test_uninstalling_something_that_was_never_installed_is_not_an_error(inst):
    res = inst.uninstall("--yes")
    assert res.returncode == 0, combined(res)
    assert "no launchd jobs installed" in combined(res)


def test_it_is_idempotent(inst):
    inst.install()
    assert inst.uninstall("--yes").returncode == 0
    second = inst.uninstall("--yes")
    assert second.returncode == 0, combined(second)


# --- 4. the user's own files ---------------------------------------------------

def test_it_removes_the_blocks_and_leaves_the_rest_byte_identical(inst):
    inst.add_blocks("ops", "brief")
    with_blocks = inst.claude_md.read_bytes()
    assert b"BEGIN telegram-agent-estate:ops" in with_blocks

    res = inst.uninstall("--yes")

    assert res.returncode == 0, combined(res)
    after = inst.claude_md.read_bytes()
    assert b"telegram-agent-estate:ops" not in after
    assert b"telegram-agent-estate:brief" not in after
    assert after.startswith(PROSE_BEFORE.encode()), "prose before the block changed"
    assert PROSE_AFTER.encode() in after, "prose after the block was eaten"


def test_keep_claude_md_leaves_the_blocks_alone(inst):
    inst.add_blocks("ops")
    before = inst.claude_md.read_bytes()

    res = inst.uninstall("--yes", "--keep-claude-md")

    assert res.returncode == 0, combined(res)
    assert inst.claude_md.read_bytes() == before


def test_a_project_with_no_claude_md_is_not_an_error(tmp_path):
    inst = Instance(tmp_path / "inst", with_claude_md=False)
    inst.install()
    res = inst.uninstall("--yes")
    assert res.returncode == 0, combined(res)


def test_the_handoff_and_the_workdir_survive(inst):
    inst.add_blocks("ops")
    inst.install()

    res = inst.uninstall("--yes", "--purge-state", "--purge-secrets")

    assert res.returncode == 0, combined(res)
    assert inst.handoff.exists(), "the agent's memory was deleted by an uninstall"
    assert "remembered" in inst.handoff.read_text(encoding="utf-8")
    assert inst.workdir.is_dir()
    assert (inst.workdir / "prompts" / "job.txt").exists()


# --- 5. state and secrets are opt-in ------------------------------------------

def test_state_and_secrets_survive_by_default(inst):
    inst.install()
    (inst.state_dir / "inbound-wal.jsonl").write_text("{}\n", encoding="utf-8")

    res = inst.uninstall("--yes")

    assert res.returncode == 0, combined(res)
    assert (inst.state_dir / "inbound-wal.jsonl").exists(), "the WAL was deleted"
    assert inst.token_file.exists(), "the token file was deleted without --purge-secrets"
    assert inst.settings_file.exists()


def test_purge_state_deletes_the_state_and_logs(inst):
    inst.install()
    (inst.state_dir / "inbound-wal.jsonl").write_text("{}\n", encoding="utf-8")
    (inst.log_dir / "daemon.log").write_text("...\n", encoding="utf-8")

    res = inst.uninstall("--yes", "--purge-state")

    assert res.returncode == 0, combined(res)
    assert not inst.state_dir.exists()
    assert not inst.log_dir.exists()
    assert inst.token_file.exists(), "--purge-state took the secrets with it"


def test_purge_secrets_deletes_the_token_and_says_it_is_not_revoked(inst):
    inst.install()

    res = inst.uninstall("--yes", "--purge-secrets")

    assert res.returncode == 0, combined(res)
    assert not inst.token_file.exists()
    assert not inst.settings_file.exists()
    assert inst.state_dir.exists(), "--purge-secrets took the state with it"
    out = combined(res)
    assert "BotFather" in out, (
        "deleting the file does not revoke the token, and a teardown that "
        "implies otherwise leaves a live bot the user believes is gone")


# --- 6. after the venv is gone -------------------------------------------------

def test_it_tears_down_when_the_configured_python_is_missing(tmp_path):
    """The venv is a natural first casualty of a teardown. An uninstaller that
    needs it cannot remove the launchd jobs it installed, which leaves a timer
    running that the user has no way to stop."""
    inst = Instance(tmp_path / "inst")
    inst.install()
    inst.add_blocks("ops")
    assert inst.plists

    data = yaml.safe_load(inst.config.read_text(encoding="utf-8"))
    data["python"] = str(tmp_path / "deleted-venv" / "bin" / "python3")
    inst.config.write_text(yaml.safe_dump(data, width=10_000), encoding="utf-8")

    res = inst.uninstall("--yes")

    assert res.returncode == 0, combined(res)
    assert inst.plists == [], "the jobs outlived the venv"
    assert b"telegram-agent-estate:ops" not in inst.claude_md.read_bytes()
    assert "degraded" in combined(res).lower() or "will not run" in combined(res)


def test_a_purge_is_refused_rather_than_guessed_when_degraded(tmp_path):
    """`instance_config` is what knows that an unset `channel_state_dir` falls
    back to the log dir. A degraded read does not, and a teardown that guesses
    at which directory to `rm -rf` is not one worth having."""
    inst = Instance(tmp_path / "inst")
    inst.install()
    data = yaml.safe_load(inst.config.read_text(encoding="utf-8"))
    data["python"] = str(tmp_path / "gone" / "python3")
    inst.config.write_text(yaml.safe_dump(data, width=10_000), encoding="utf-8")

    res = inst.uninstall("--yes", "--purge-state")

    assert res.returncode == 2, combined(res)
    assert inst.state_dir.exists(), "a degraded run purged anyway"
    assert "--purge-state needs the full config" in combined(res)


# --- 7. usage and identity -----------------------------------------------------

def test_a_missing_config_explains_why_the_name_matters(tmp_path):
    res = run([UNINSTALL, tmp_path / "nope.yaml"])
    assert res.returncode == 2
    assert "unreadable instance config" in combined(res)
    assert "LaunchAgents" in combined(res), (
        "a user whose config is gone is left with plists and no instructions")


def test_no_argument_is_usage(tmp_path):
    res = run([UNINSTALL])
    assert res.returncode == 2
    assert "usage: uninstall-instance.sh" in combined(res)


def test_an_unknown_option_is_refused_rather_than_read_as_a_config(tmp_path):
    """`--purge-everything` must not be silently taken for a config path and
    then, being unreadable, exit 2 with a message about the wrong thing."""
    inst = Instance(tmp_path / "inst")
    res = run([UNINSTALL, inst.config, "--purge-everything"])
    assert res.returncode == 2
    assert "unknown option" in combined(res)


def test_a_config_with_no_name_refuses_before_matching_any_process(tmp_path):
    """Every `pkill` pattern in this script is built from the instance name. An
    empty name yields a pattern that matches far more than it should, so the
    refusal has to come first."""
    inst = Instance(tmp_path / "inst")
    data = yaml.safe_load(inst.config.read_text(encoding="utf-8"))
    del data["name"]
    inst.config.write_text(yaml.safe_dump(data, width=10_000), encoding="utf-8")

    res = run([UNINSTALL, inst.config, "--yes"], agents=inst.agents)

    assert res.returncode == 2, combined(res)
    assert "launchd identity" in combined(res)


# --- 8. the process patterns ---------------------------------------------------

def test_processes_are_matched_by_instance_tag_never_by_a_libexec_path():
    """Every instance executes the same `libexec/poller.py`, so a pattern keyed
    to that path selects the neighbours too — and this script's use of it is
    `pkill`. The rule is stated in `libexec/README.md`; this is the assertion
    that it was followed here.
    """
    body = UNINSTALL.read_text(encoding="utf-8")

    # Where patterns are AUTHORED: the `stop_matching` call sites and any direct
    # pgrep/pkill. `"$pattern"` inside the helper is the parameter those call
    # sites fill, so it is the one string that carries no instance key of its
    # own — and it is excluded by name rather than by shape, so a second,
    # differently-named indirection would fail this test rather than slip past.
    patterns = [p for p in
                re.findall(r'(?:pgrep|pkill)[^\n]*-f "([^"]+)"', body)
                + re.findall(r'stop_matching "[^"]*" "([^"]+)"', body)
                if p != "$pattern"]
    assert patterns, "no process patterns found — did the matching move?"
    for pat in patterns:
        assert "$NAME" in pat or "$CONFIG" in pat, (
            f"pattern {pat!r} is not keyed to this instance; it would match "
            "another instance's processes")
        assert "LIBEXEC" not in pat, (
            f"pattern {pat!r} matches a shared path — see libexec/README.md")


def test_the_kill_is_term_before_kill():
    """A poller SIGKILLed mid-write leaves a torn line in the journal that is
    delivery evidence for a retry to read."""
    body = UNINSTALL.read_text(encoding="utf-8")
    assert "pkill -TERM" in body
    term = body.index("pkill -TERM")
    kill = body.index("pkill -KILL")
    assert term < kill, "SIGKILL is issued before SIGTERM"
