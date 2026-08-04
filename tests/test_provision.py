"""U7/KTD1: the daemons run a pinned checkout, and every command follows the pin.

WHY THIS FILE EXISTS. An installed plugin lands in
`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` — a NEW directory
per version, with the old ones left in place. A launchd job pointed at one of
those does not fail when the plugin updates. It goes on executing a version
nobody chose, silently, for as long as the mac is up. That is the failure this
whole estate was built to eliminate, so the code launchd executes is provisioned
into a separate checkout that only an explicit `upgrade` moves.

Three properties have to hold together, and each can regress on its own:

  1. THE PIN IS A RESOLVED COMMIT SHA. A tag is mutable and can be moved
     server-side with no signal on this machine. What is behind it here runs as
     the user, on a timer, unattended — so a tag is a request and the SHA is the
     answer, and a tag that has moved since the stamp was written is reported,
     not adopted.
  2. NOTHING IS RESTARTED ON UNVERIFIED CODE. An upgrade checks that the
     checked-out HEAD is the commit that was asked for BEFORE it touches a
     daemon. `git checkout` reporting success is a different claim.
  3. EVERY COMMAND FOLLOWS THE SAME PIN. A `parity` that verified the cache
     while the supervisor ran a different tree would make the one place a user
     looks the one place that cannot see the disagreement.

HOW THE DISPATCHER RESOLVES, AND WHY IT IS CONDITIONAL. Read literally, KTD1
says "resolve through the stamp, fail closed when there is none" — which would
break every development checkout, including this repo and this suite, where no
stamp exists and self-relative resolution is the only correct answer. The rule
is therefore conditioned on where the caller lives: an `$ESTATE_LIBEXEC`
override wins; a CACHE-RESIDENT caller runs the provisioned runtime or refuses
with a provision instruction; anything else runs the code next to it and says so
on stderr when a different pin is installed. Both sides are covered below.

NOTHING HERE MAY TOUCH THE REAL RUNTIME. `$ESTATE_RUNTIME_ROOT` defaults to
`~/.local/share/telegram-agent-estate`, which on a machine that has onboarded is
the code two daemons execute. Every helper in this file takes the root as a
required argument for that reason — there is no way to call the provisioner from
this module without naming a scratch one. This is not hypothetical: writing this
unit created a real runtime there, from a test that only meant to assert the
verb was still a stub.
"""
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
LIBEXEC = PLUGIN_ROOT / "libexec"
DISPATCHER = PLUGIN_ROOT / "bin" / "telegram-agent-estate"
PROVISION = LIBEXEC / "provision-runtime.sh"

# The provisioner's own exit codes. Callers branch on them, so they are named
# here rather than spelled as bare integers at the assertion.
OK, FAILED, REFUSED, TAG_MOVED, HEAD_MISMATCH = 0, 1, 2, 3, 4


# --- process helpers ----------------------------------------------------------

def run(argv, env=None, cwd=None):
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                          timeout=180, env=env, cwd=cwd)


def combined(res):
    return res.stdout + res.stderr


def base_env(root, **extra):
    """The environment every provisioner call in this module runs under.

    `root` is positional and required. See the module docstring.
    """
    env = {**os.environ, "ESTATE_RUNTIME_ROOT": str(root)}
    env.pop("ESTATE_LIBEXEC", None)
    for k, v in extra.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = str(v)
    return env


def provision(root, *args, verb="provision", **envkw):
    return run([PROVISION, verb, *args], env=base_env(root, **envkw))


def stamp_of(root):
    return json.loads((Path(root) / "runtime.json").read_text(encoding="utf-8"))


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


# --- a synthetic source repo --------------------------------------------------

def git(repo, *args):
    """git, with the author's own config held off.

    Signing is disabled explicitly: this machine signs commits through an
    external agent, which in a non-interactive test either fails outright or —
    worse — blocks on a confirmation dialog nobody is looking at. A fixture repo
    is scaffolding; it should not depend on the developer's keychain.
    """
    res = run(["git", "-C", str(repo), "-c", "user.email=t@example.invalid",
               "-c", "user.name=t", "-c", "commit.gpgsign=false",
               "-c", "tag.gpgsign=false", *args])
    assert res.returncode == 0, combined(res)
    return res.stdout.strip()


RECORDER = """#!/bin/zsh
# Records which copy of itself ran, and with what. The path is the assertion:
# it is how a test tells the runtime's tree from the cache's.
print -r -- "${0:A}" > "${ESTATE_RECORD:-/dev/null}"
print -rl -- "$@" >> "${ESTATE_RECORD:-/dev/null}"
exit 0
"""


def plugin_shaped_tree(root, marker):
    """The smallest tree the provisioner and the dispatcher both accept."""
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "telegram-agent-estate", "version": "9.9.9"}),
        encoding="utf-8")
    write_exec(root / "bin" / "telegram-agent-estate",
               DISPATCHER.read_text(encoding="utf-8"))
    (root / "libexec").mkdir(parents=True, exist_ok=True)
    # The two files the dispatcher genuinely needs: the resolver it sources, and
    # the provisioner it bootstraps from. Everything else is a recorder, because
    # what these trees are for is telling WHICH copy ran.
    (root / "libexec" / "estate-runtime.zsh").write_text(
        (LIBEXEC / "estate-runtime.zsh").read_text(encoding="utf-8"), encoding="utf-8")
    write_exec(root / "libexec" / "provision-runtime.sh",
               PROVISION.read_text(encoding="utf-8"))
    for name in ("parity.sh", "install-instance.sh", "supervisor.sh"):
        write_exec(root / "libexec" / name, RECORDER)
    (root / "marker.txt").write_text(marker, encoding="utf-8")
    return root


@pytest.fixture
def source_repo(tmp_path):
    """A two-commit plugin-shaped repo: v1.0 annotated at c1, v2.0 light at c2.

    Annotated on purpose — `ls-remote` reports an annotated tag twice, as the
    tag object and as the peeled commit, and a resolver that takes the first
    line pins the tag object, which is not what a checkout lands on.
    """
    repo = tmp_path / "src"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    plugin_shaped_tree(repo, "one")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "one")
    c1 = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "-a", "v1.0", "-m", "release one")
    (repo / "marker.txt").write_text("two", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "two")
    c2 = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "v2.0")
    return type("Src", (), {"path": repo, "c1": c1, "c2": c2})


# --- 1. provisioning ----------------------------------------------------------

def test_provisioning_an_empty_target_pins_the_resolved_sha(tmp_path, source_repo):
    """The tag is what you ask for; the SHA is what gets recorded and run."""
    root = tmp_path / "share"
    res = provision(root, "--source", source_repo.path, "--ref", "v1.0")
    assert res.returncode == OK, combined(res)

    runtime = root / "runtime"
    assert (runtime / "libexec" / "parity.sh").is_file()
    assert (runtime / "marker.txt").read_text(encoding="utf-8") == "one"

    s = stamp_of(root)
    assert s["sha"] == source_repo.c1, "the stamp did not record the peeled commit"
    assert s["ref"] == "v1.0" and s["ref_full"] == "refs/tags/v1.0"
    assert s["mechanism"] == "git" and s["upgradable"] == "yes"
    assert s["path"] == str(runtime)
    assert s["plugin_version"] == "9.9.9", "the stamp did not read the checkout's manifest"


def test_the_stamp_records_a_full_sha_even_when_asked_by_abbreviation(
        tmp_path, source_repo):
    """An abbreviation is a fine way to ask and a terrible thing to record: it
    is ambiguous by construction, and the stamp has to name one commit."""
    root = tmp_path / "share"
    res = provision(root, "--source", source_repo.path, "--ref", source_repo.c1[:7])
    assert res.returncode == OK, combined(res)
    assert stamp_of(root)["sha"] == source_repo.c1


def test_reprovisioning_the_same_pin_is_a_noop_and_says_so(tmp_path, source_repo):
    """Idempotence is what makes this safe to put in a setup skill that may be
    re-run; saying so is what stops a user re-running it harder."""
    root = tmp_path / "share"
    assert provision(root, "--source", source_repo.path,
                     "--ref", "v1.0").returncode == OK
    before = stamp_of(root)
    res = provision(root, "--source", source_repo.path, "--ref", "v1.0")
    assert res.returncode == OK, combined(res)
    assert "nothing to do" in combined(res)
    assert stamp_of(root)["provisioned_at"] == before["provisioned_at"], (
        "the no-op rewrote the stamp, so it was not a no-op")


def test_reprovisioning_at_a_different_pin_reports_old_and_new(tmp_path, source_repo):
    root = tmp_path / "share"
    assert provision(root, "--source", source_repo.path,
                     "--ref", "v1.0").returncode == OK
    res = provision(root, "--source", source_repo.path, "--ref", "v2.0")
    assert res.returncode == OK, combined(res)
    out = combined(res)
    assert source_repo.c1 in out and source_repo.c2 in out, (
        "a pin change that names neither the old nor the new commit is not a report")
    assert stamp_of(root)["sha"] == source_repo.c2
    assert (root / "runtime" / "marker.txt").read_text(encoding="utf-8") == "two"


def test_a_moved_tag_is_detected_and_refused(tmp_path, source_repo):
    """THE reason the pin is a SHA.

    A tag can be moved server-side with no local signal. Adopting the new
    commit because the tag name still matches would make the pin decorative.
    """
    root = tmp_path / "share"
    assert provision(root, "--source", source_repo.path,
                     "--ref", "v1.0").returncode == OK
    git(source_repo.path, "tag", "-f", "-a", "v1.0", "-m", "moved", source_repo.c2)

    res = provision(root, "--source", source_repo.path, "--ref", "v1.0")
    assert res.returncode == TAG_MOVED, combined(res)
    out = combined(res)
    assert "MOVED" in out
    assert source_repo.c1 in out and source_repo.c2 in out, (
        "the refusal must name both commits or it cannot be acted on")
    assert stamp_of(root)["sha"] == source_repo.c1, "the moved tag was adopted anyway"
    assert (root / "runtime" / "marker.txt").read_text(encoding="utf-8") == "one"

    forced = provision(root, "--source", source_repo.path, "--ref", "v1.0", "--force")
    assert forced.returncode == OK, combined(forced)
    assert stamp_of(root)["sha"] == source_repo.c2


def test_a_branch_moving_is_not_treated_as_a_moved_tag(tmp_path, source_repo):
    """The check has to discriminate. A branch that moved is a branch; refusing
    that would make `--ref main` unusable and teach people to pass --force."""
    root = tmp_path / "share"
    git(source_repo.path, "checkout", "-q", source_repo.c1)
    git(source_repo.path, "branch", "-f", "main", source_repo.c1)
    assert provision(root, "--source", source_repo.path,
                     "--ref", "main").returncode == OK
    assert stamp_of(root)["sha"] == source_repo.c1
    git(source_repo.path, "branch", "-f", "main", source_repo.c2)

    res = provision(root, "--source", source_repo.path, "--ref", "main")
    assert res.returncode == OK, combined(res)
    assert stamp_of(root)["sha"] == source_repo.c2


@pytest.mark.parametrize("segment", ["Dropbox", "Library/Mobile Documents",
                                     "Google Drive", "Library/CloudStorage"])
def test_provisioning_refuses_a_synced_target(tmp_path, source_repo, segment):
    """A sync client rewrites files underneath the process reading them, and
    the daemons read this checkout on every turn. The project has already paid
    for this lesson twice — a corrupted venv and a rebase that fought Dropbox."""
    root = tmp_path.joinpath(*segment.split("/"), "share")
    res = provision(root, "--source", source_repo.path, "--ref", "v1.0")
    assert res.returncode == REFUSED, combined(res)
    assert "REFUSING" in combined(res)
    assert not (root / "runtime").exists(), "it refused and provisioned anyway"


# --- 2. the copy fallback -----------------------------------------------------

def test_without_git_the_copy_fallback_produces_a_working_runtime(
        tmp_path, source_repo):
    """Offline, or on a mac with no git, an install still has to be possible —
    and the resulting runtime still has to be honest about what it is."""
    root = tmp_path / "share"
    res = provision(root, "--source", source_repo.path,
                    ESTATE_GIT=tmp_path / "no-such-git")
    assert res.returncode == OK, combined(res)

    runtime = root / "runtime"
    assert (runtime / "libexec" / "parity.sh").is_file()
    assert os.access(runtime / "libexec" / "parity.sh", os.X_OK), (
        "a copied runtime whose executables lost the exec bit is not a runtime")
    assert not (runtime / ".git").exists(), "the copy dragged the source's git dir along"

    s = stamp_of(root)
    assert s["mechanism"] == "copy"
    assert s["upgradable"] == "no"
    assert s["sha"] == "", "a copy is not pinned; recording a SHA would claim it is"
    assert "no upgrade path" in s["note"].lower()


def test_upgrading_a_copied_runtime_is_refused_with_the_reason(tmp_path, source_repo):
    """The discoverability half of the fallback: the user should be able to
    find out that this runtime cannot be upgraded, rather than infer it."""
    root = tmp_path / "share"
    assert provision(root, "--source", source_repo.path,
                     ESTATE_GIT=tmp_path / "no-such-git").returncode == OK
    res = provision(root, "--source", source_repo.path, verb="upgrade",
                    ESTATE_GIT=tmp_path / "no-such-git")
    assert res.returncode == REFUSED, combined(res)
    assert "no upgrade path" in combined(res).lower()


def test_status_reports_the_mechanism_and_the_pin(tmp_path, source_repo):
    root = tmp_path / "share"
    assert provision(root, "--status").returncode == OK      # nothing provisioned yet
    assert "no runtime provisioned" in combined(provision(root, "--status"))

    assert provision(root, "--source", source_repo.path,
                     "--ref", "v1.0").returncode == OK
    out = combined(provision(root, "--status"))
    assert source_repo.c1 in out and "v1.0" in out and "git" in out


# --- 3. upgrade ---------------------------------------------------------------

FAKE_LAUNCHCTL = """#!/bin/sh
printf '%s ' "$@" >> "$ESTATE_LAUNCHCTL_LOG"
printf '\\n' >> "$ESTATE_LAUNCHCTL_LOG"
exit 0
"""

# A git that does everything except move HEAD. Not a mock of the verification —
# the verification runs for real against a checkout that genuinely did not
# happen, which is the shape a raced index or a hostile filter would leave.
LYING_GIT = """#!/bin/sh
for a in "$@"; do
  if [ "$a" = "checkout" ]; then exit 0; fi
done
exec %s "$@"
""" % shutil.which("git")


def supervisor_plist(agents, label, runtime):
    agents.mkdir(parents=True, exist_ok=True)
    p = agents / f"{label}.plist"
    p.write_bytes(plistlib.dumps({
        "Label": label,
        "ProgramArguments": ["/bin/zsh", "-l", "-c",
                             f"{runtime}/libexec/supervisor.sh /tmp/x.yaml"],
    }))
    return p


def test_upgrade_verifies_head_before_it_restarts_anything(tmp_path, source_repo):
    """Property 2. The order is the contract: verify, then swap, then restart.

    An upgrade that restarted daemons on a tree it had not checked would be the
    silent auto-update this design exists to forbid — with a human's explicit
    command as the fig leaf.
    """
    root = tmp_path / "share"
    agents = tmp_path / "agents"
    log = tmp_path / "launchctl.log"
    assert provision(root, "--source", source_repo.path,
                     "--ref", "v2.0").returncode == OK
    supervisor_plist(agents, "local.testbot", root / "runtime")

    res = provision(
        root, "--source", source_repo.path, "--ref", "v1.0", verb="upgrade",
        ESTATE_GIT=write_exec(tmp_path / "bin" / "git", LYING_GIT),
        ESTATE_LAUNCHCTL=write_exec(tmp_path / "bin" / "launchctl", FAKE_LAUNCHCTL),
        ESTATE_LAUNCHCTL_LOG=log, ESTATE_LAUNCH_AGENTS=agents)

    assert res.returncode == HEAD_MISMATCH, combined(res)
    assert "REFUSING" in combined(res)
    assert not log.exists(), "it refused the code and restarted the daemons anyway"
    assert stamp_of(root)["sha"] == source_repo.c2, "the unverified tree was adopted"
    assert (root / "runtime" / "marker.txt").read_text(encoding="utf-8") == "two"


def test_upgrade_restarts_only_the_supervisors_pointing_at_this_runtime(
        tmp_path, source_repo):
    """The positive control for the test above — and the whole point of the
    verb: an upgrade nobody restarts is an upgrade nobody is running."""
    root = tmp_path / "share"
    agents = tmp_path / "agents"
    log = tmp_path / "launchctl.log"
    assert provision(root, "--source", source_repo.path,
                     "--ref", "v1.0").returncode == OK
    supervisor_plist(agents, "local.mine", root / "runtime")
    supervisor_plist(agents, "local.someone-else", tmp_path / "other-runtime")

    res = provision(root, "--source", source_repo.path, "--ref", "v2.0",
                    verb="upgrade",
                    ESTATE_LAUNCHCTL=write_exec(tmp_path / "bin" / "launchctl",
                                                FAKE_LAUNCHCTL),
                    ESTATE_LAUNCHCTL_LOG=log, ESTATE_LAUNCH_AGENTS=agents)
    assert res.returncode == OK, combined(res)
    assert stamp_of(root)["sha"] == source_repo.c2

    calls = log.read_text(encoding="utf-8")
    assert "kickstart -k" in calls and "local.mine" in calls
    assert "someone-else" not in calls, (
        "an upgrade of one runtime restarted a daemon running another")


def test_upgrade_can_be_told_not_to_restart_and_says_what_that_means(
        tmp_path, source_repo):
    root = tmp_path / "share"
    agents = tmp_path / "agents"
    log = tmp_path / "launchctl.log"
    assert provision(root, "--source", source_repo.path,
                     "--ref", "v1.0").returncode == OK
    supervisor_plist(agents, "local.mine", root / "runtime")

    res = provision(root, "--source", source_repo.path, "--ref", "v2.0",
                    "--no-restart", verb="upgrade",
                    ESTATE_LAUNCHCTL=write_exec(tmp_path / "bin" / "launchctl",
                                                FAKE_LAUNCHCTL),
                    ESTATE_LAUNCHCTL_LOG=log, ESTATE_LAUNCH_AGENTS=agents)
    assert res.returncode == OK, combined(res)
    assert not log.exists()
    assert "OLD code" in combined(res)


def test_upgrade_without_a_provisioned_runtime_says_to_provision(tmp_path, source_repo):
    res = provision(tmp_path / "share", "--source", source_repo.path,
                    "--ref", "v1.0", verb="upgrade")
    assert res.returncode == REFUSED, combined(res)
    assert "provision" in combined(res)


# --- 4. interrupted provisions ------------------------------------------------

def test_an_interrupted_provision_is_detected_and_retried_cleanly(
        tmp_path, source_repo):
    """The stamp is written last, so a checkout without one is a provision that
    died mid-flight. Adopting it would mean running a half-populated tree."""
    root = tmp_path / "share"
    (root / "runtime" / "libexec").mkdir(parents=True)
    (root / "runtime" / "libexec" / "parity.sh").write_text("half", encoding="utf-8")
    (root / "runtime.incoming.4242" / "libexec").mkdir(parents=True)

    res = provision(root, "--source", source_repo.path, "--ref", "v1.0")
    assert res.returncode == OK, combined(res)
    out = combined(res)
    assert "interrupted provision" in out
    assert not (root / "runtime.incoming.4242").exists(), "leftovers survived"
    assert (root / "runtime" / "marker.txt").read_text(encoding="utf-8") == "one"
    assert stamp_of(root)["sha"] == source_repo.c1
    assert list(root.glob("runtime.old.*")) == []


def test_a_stamp_whose_checkout_vanished_is_reported_and_replaced(
        tmp_path, source_repo):
    root = tmp_path / "share"
    assert provision(root, "--source", source_repo.path,
                     "--ref", "v1.0").returncode == OK
    shutil.rmtree(root / "runtime")

    res = provision(root, "--source", source_repo.path, "--ref", "v1.0")
    assert res.returncode == OK, combined(res)
    assert "not there" in combined(res)
    assert stamp_of(root)["sha"] == source_repo.c1
    assert (root / "runtime" / "marker.txt").is_file()


def test_a_bare_provision_refuses_rather_than_provisioning_from_defaults(tmp_path):
    """Every option has a default, so a bare invocation cannot be told from a
    mis-fire — and what it writes is the code launchd executes. This exact
    hole put a real runtime in ~/.local/share while this unit was being built.
    """
    root = tmp_path / "share"
    res = run([PROVISION, "provision"], env=base_env(root))
    assert res.returncode == REFUSED, combined(res)
    assert not root.exists(), "the refusal still created the root"


# --- 5. how the dispatcher resolves -------------------------------------------

def cache_tree(tmp_path, name="0.2.0"):
    """A dispatcher that lives where an installed plugin lives.

    The segment `plugins/cache/<marketplace>/<plugin>/<version>/` is the shape
    Claude Code installs into, and it is what marks this copy as one an update
    replaces wholesale.
    """
    root = tmp_path / "cachehome" / "plugins" / "cache" / "mkt" / \
        "telegram-agent-estate" / name
    plugin_shaped_tree(root, "cache")
    return root


def checkout_tree(tmp_path, name="checkout"):
    """A dispatcher in a plain git checkout — a developer's, or the author's."""
    root = tmp_path / name
    plugin_shaped_tree(root, "checkout")
    return root


def test_a_cache_resident_dispatcher_refuses_to_run_the_cache(tmp_path):
    """The strict half of the rule.

    Falling back to the cache's own `libexec/` here is the exact staleness this
    unit exists to abolish, and it would be invisible: the command would work.
    """
    cache = cache_tree(tmp_path)
    record = tmp_path / "record"
    res = run([cache / "bin" / "telegram-agent-estate", "parity", "cfg.yaml"],
              env=base_env(tmp_path / "share", ESTATE_RECORD=record))
    assert res.returncode == 2, combined(res)
    assert not record.exists(), "it refused and executed the cache's copy anyway"
    out = combined(res)
    assert "provision" in out, "a refusal with no instruction is a dead end"
    assert "ESTATE_LIBEXEC" in out, "the development escape hatch is not named"


def test_a_cache_resident_dispatcher_runs_the_provisioned_runtime(
        tmp_path, source_repo):
    """The strict half, satisfied. This is the property R8 is made of."""
    root = tmp_path / "share"
    cache = cache_tree(tmp_path)
    assert provision(root, "--source", source_repo.path,
                     "--ref", "v1.0").returncode == OK

    record = tmp_path / "record"
    res = run([cache / "bin" / "telegram-agent-estate", "parity", "cfg.yaml"],
              env=base_env(root, ESTATE_RECORD=record))
    assert res.returncode == 0, combined(res)
    ran = record.read_text(encoding="utf-8").splitlines()
    assert ran[0] == str(root / "runtime" / "libexec" / "parity.sh"), ran
    assert ran[1:] == ["cfg.yaml"], "arguments stopped passing through verbatim"


def test_a_plain_checkout_runs_itself(tmp_path):
    """The permissive half. A checkout is the code the person is looking at;
    redirecting it elsewhere would be the same surprise, inverted — and it would
    make this repo's own suite untestable."""
    tree = checkout_tree(tmp_path)
    record = tmp_path / "record"
    res = run([tree / "bin" / "telegram-agent-estate", "parity", "cfg.yaml"],
              env=base_env(tmp_path / "share", ESTATE_RECORD=record))
    assert res.returncode == 0, combined(res)
    assert record.read_text(encoding="utf-8").splitlines()[0] == \
        str(tree / "libexec" / "parity.sh")


def test_a_plain_checkout_says_when_the_daemons_are_on_a_different_pin(
        tmp_path, source_repo):
    """Permissive, but not quiet. The answer stays "your checkout"; it just
    stops being a secret that the daemons are running something else."""
    root = tmp_path / "share"
    assert provision(root, "--source", source_repo.path,
                     "--ref", "v1.0").returncode == OK
    tree = checkout_tree(tmp_path)
    record = tmp_path / "record"
    res = run([tree / "bin" / "telegram-agent-estate", "parity", "cfg.yaml"],
              env=base_env(root, ESTATE_RECORD=record))
    assert res.returncode == 0, combined(res)
    assert record.read_text(encoding="utf-8").splitlines()[0] == \
        str(tree / "libexec" / "parity.sh"), "the note changed the resolution"
    assert source_repo.c1 in res.stderr and str(root / "runtime") in res.stderr


def test_an_explicit_libexec_override_wins_over_the_strict_rule(tmp_path):
    """The escape hatch the refusal advertises has to work, or the refusal is
    telling users to run something that does not."""
    cache = cache_tree(tmp_path)
    other = checkout_tree(tmp_path, "elsewhere")
    record = tmp_path / "record"
    env = base_env(tmp_path / "share", ESTATE_RECORD=record)
    env["ESTATE_LIBEXEC"] = str(other / "libexec")
    res = run([cache / "bin" / "telegram-agent-estate", "parity", "cfg.yaml"], env=env)
    assert res.returncode == 0, combined(res)
    assert record.read_text(encoding="utf-8").splitlines()[0] == \
        str(other / "libexec" / "parity.sh")


def test_provision_and_upgrade_bootstrap_from_the_cache_without_a_stamp(tmp_path):
    """The one exception to the strict rule, and it is not optional: these two
    verbs are what WRITES the stamp, so requiring one would make a fresh
    machine unprovisionable."""
    cache = cache_tree(tmp_path)
    for verb in ("provision", "upgrade"):
        res = run([cache / "bin" / "telegram-agent-estate", verb, "--status"],
                  env=base_env(tmp_path / "share"))
        assert res.returncode == 0, combined(res)
        assert "no runtime provisioned" in combined(res)


def test_the_dispatcher_still_reports_an_unknown_subcommand_after_the_rewire():
    """Guards the wiring: `provision` and `upgrade` share one entry point and
    are told which verb they are, so a typo must not fall into that path."""
    res = run([DISPATCHER, "provisionn"])
    assert res.returncode == 2
    assert "unknown subcommand" in combined(res)


# --- 6. what launchd is handed ------------------------------------------------

def build_instance(root, name, chat):
    """A complete, self-contained instance rooted at `root`.

    Deliberately a local copy of the one in test_dispatcher.py rather than an
    import: these two modules assert about different things and neither should
    be able to break the other by tightening its own fixture.
    """
    root = Path(root)
    (root / "repo" / "prompts").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "gw").mkdir(parents=True, exist_ok=True)
    (root / "repo" / "prompts" / "job.txt").write_text("do it\n", encoding="utf-8")
    (root / "env").write_text(f"TELEGRAM_BOT_TOKEN=fake-{name}\n", encoding="utf-8")
    os.chmod(root / "env", 0o600)
    data = {
        "name": name, "label": f"{name.title()} Bot",
        "workdir": str(root / "repo"), "python": sys.executable,
        "telegram": {"owner_chat_id": chat, "token_file": str(root / "env")},
        "runtime": {"state_dir": str(root / "gw"), "log_dir": str(root / "logs")},
        "schedules": [{"job": "morning-digest", "hour": 9, "minute": 0,
                       "prompt_file": "prompts/job.txt"}],
    }
    p = root / f"{name}.yaml"
    p.write_text(yaml.safe_dump(data, width=10_000), encoding="utf-8")
    return p


def real_cache_tree(tmp_path):
    """A cache-resident copy of THIS plugin — the real dispatcher and the real
    installer, at the path an installed plugin actually occupies."""
    root = tmp_path / "cachehome" / "plugins" / "cache" / "mkt" / \
        "telegram-agent-estate" / "0.2.0"
    root.mkdir(parents=True)
    for part in ("bin", "libexec", "templates", ".claude-plugin"):
        shutil.copytree(PLUGIN_ROOT / part, root / part,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return root


@pytest.fixture
def real_runtime(tmp_path):
    """A runtime holding this working tree, provisioned the way an offline
    install would. `--copy` and not a clone on purpose: the assertions below are
    about the code that is here NOW, not about the last commit."""
    root = tmp_path / "share"
    res = provision(root, "--source", PLUGIN_ROOT, "--copy")
    assert res.returncode == OK, combined(res)
    return root


def test_rendered_plists_point_at_the_runtime_not_the_cache(tmp_path, real_runtime):
    """launchd holds the absolute path copied at install time and executes it
    unattended until the next install. Stamping the cache's path there is how a
    daemon ends up running a version nobody chose."""
    cache = real_cache_tree(tmp_path)
    agents = tmp_path / "agents"
    agents.mkdir()
    cfg = build_instance(tmp_path / "inst", "pinbot", 10_000_000_004)

    env = base_env(real_runtime, ESTATE_LAUNCH_AGENTS=agents)
    res = run([cache / "libexec" / "install-instance.sh", cfg, "--no-load"], env=env)
    assert res.returncode == 0, combined(res)

    plists = sorted(agents.glob("*.plist"))
    assert len(plists) == 2, [p.name for p in plists]
    runtime_libexec = str(real_runtime / "runtime" / "libexec")
    for p in plists:
        cmd = plistlib.loads(p.read_bytes())["ProgramArguments"][-1]
        assert cmd.startswith(runtime_libexec + "/"), cmd
        assert str(cache) not in cmd, "launchd was handed the plugin cache"
        assert str(PLUGIN_ROOT) not in cmd, "launchd was handed the invoking checkout"


def test_parity_resolves_to_the_checkout_the_supervisor_executes(
        tmp_path, real_runtime):
    """Property 3, stated as the equality it has to be.

    Both sides are read from the artifacts themselves — the path launchd will
    exec, and the path the dispatcher actually ran — so this cannot pass by
    agreeing with a constant that is wrong.
    """
    cache = real_cache_tree(tmp_path)
    agents = tmp_path / "agents"
    agents.mkdir()
    cfg = build_instance(tmp_path / "inst", "samebot", 10_000_000_005)

    env = base_env(real_runtime, ESTATE_LAUNCH_AGENTS=agents)
    assert run([cache / "libexec" / "install-instance.sh", cfg, "--no-load"],
               env=env).returncode == 0

    supervisor = next(
        plistlib.loads(p.read_bytes())["ProgramArguments"][-1].split()[0]
        for p in agents.glob("*.plist")
        if "supervisor.sh" in plistlib.loads(p.read_bytes())["ProgramArguments"][-1])

    # Swap the runtime's parity for a recorder: what is asserted is WHICH copy
    # ran, and running the real suite inside the suite would prove nothing else.
    record = tmp_path / "record"
    write_exec(real_runtime / "runtime" / "libexec" / "parity.sh", RECORDER)
    res = run([cache / "bin" / "telegram-agent-estate", "parity", str(cfg)],
              env=base_env(real_runtime, ESTATE_RECORD=record))
    assert res.returncode == 0, combined(res)
    ran = record.read_text(encoding="utf-8").splitlines()[0]

    assert Path(ran).parent == Path(supervisor).parent, (
        f"parity verified {ran} while the supervisor runs {supervisor}")
    assert Path(ran).parent == real_runtime / "runtime" / "libexec"


def test_the_installer_and_the_dispatcher_cannot_disagree(tmp_path, real_runtime):
    """One rule, one copy of it, asserted as agreement rather than assumed.

    The installer decides what launchd executes and the dispatcher decides what
    a hand-run command verifies. Two independently drifting derivations of "the
    same directory" is precisely the bug R8 forbids.
    """
    cache = real_cache_tree(tmp_path)
    agents = tmp_path / "agents"
    agents.mkdir()
    cfg = build_instance(tmp_path / "inst", "agreebot", 10_000_000_006)
    assert run([cache / "libexec" / "install-instance.sh", cfg, "--no-load"],
               env=base_env(real_runtime, ESTATE_LAUNCH_AGENTS=agents)).returncode == 0
    from_installer = Path(next(
        plistlib.loads(p.read_bytes())["ProgramArguments"][-1].split()[0]
        for p in agents.glob("*.plist"))).parent

    record = tmp_path / "record"
    write_exec(real_runtime / "runtime" / "libexec" / "parity.sh", RECORDER)
    run([cache / "bin" / "telegram-agent-estate", "parity", str(cfg)],
        env=base_env(real_runtime, ESTATE_RECORD=record))
    from_dispatcher = Path(record.read_text(encoding="utf-8").splitlines()[0]).parent

    assert from_installer == from_dispatcher == real_runtime / "runtime" / "libexec"


def test_the_copied_runtime_is_a_runtime_and_not_a_shell(real_runtime):
    """Guards the fixture the two tests above lean on: if `--copy` produced a
    tree missing the pieces an install reads, they would be asserting about a
    path rather than about a working install."""
    runtime = real_runtime / "runtime"
    for needed in ("libexec/install-instance.sh", "libexec/estate-bootstrap.zsh",
                   "libexec/estate-runtime.zsh", "libexec/instance_config.py",
                   "templates/plists/supervisor.plist.tmpl",
                   "templates/plists/job.plist.tmpl"):
        assert (runtime / needed).exists(), f"the copied runtime has no {needed}"
    assert os.stat(runtime / "libexec" / "install-instance.sh").st_mode & stat.S_IXUSR
