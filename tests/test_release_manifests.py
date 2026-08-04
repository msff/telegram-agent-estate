"""The manifests, the policy files, and the CI workflow as release artifacts.

Everything here guards something a stranger meets before they meet the code: a
plugin that will not validate cannot be installed at all, a version that
collides with a directory already in the plugin cache can be "installed"
without anything being fetched, and a repo with no licence and no disclosure
address is one a careful person declines to run — which is the right call for
software that installs launchd jobs and holds a bot token.

WHAT CANNOT BE TESTED HERE, said plainly rather than faked: a GitHub Actions
workflow only proves itself on GitHub. Nothing below runs `ci.yml`, so nothing
below shows that CI goes red when the suite fails or when a bogus manifest
field lands — only that the workflow parses, targets macOS, and invokes the two
commands that would produce those failures. The *validator's* half of that
promise IS tested, on a throwaway copy, because it is a local binary and there
is no excuse for asserting it by eye.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_DIR = PLUGIN_ROOT / ".claude-plugin"
PLUGIN_JSON = MANIFEST_DIR / "plugin.json"
MARKETPLACE_JSON = MANIFEST_DIR / "marketplace.json"
LICENSE = PLUGIN_ROOT / "LICENSE"
SECURITY = PLUGIN_ROOT / "SECURITY.md"
WORKFLOW = PLUGIN_ROOT / ".github" / "workflows" / "ci.yml"

CLAUDE_CLI = shutil.which("claude")
needs_cli = pytest.mark.skipif(
    CLAUDE_CLI is None,
    reason="the `claude` CLI is not on PATH; this is the one check CI must run")


def plugin_manifest():
    return json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))


def marketplace_manifest():
    return json.loads(MARKETPLACE_JSON.read_text(encoding="utf-8"))


def validate(target):
    """`claude plugin validate <target> --strict`, as CI runs it.

    The environment is scrubbed of the API key deliberately: this project is
    subscription-billed and no part of it may run against one, so a validator
    call that only passes because a key was lying around would be testing a
    configuration the project forbids.
    """
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    return subprocess.run(
        [CLAUDE_CLI, "plugin", "validate", str(target), "--strict"],
        capture_output=True, text=True, timeout=180, env=env)


# --- the gate itself ---------------------------------------------------------


@needs_cli
def test_plugin_validate_strict_exits_zero():
    """R7, and the whole reason this file exists.

    `--strict` promotes warnings to errors, so this also fails on a field the
    CLI merely ignores — which is worse than an error, because a plugin
    carrying one appears to work and silently does not.
    """
    r = validate(PLUGIN_ROOT)
    assert r.returncode == 0, (
        f"`claude plugin validate . --strict` exited {r.returncode}\n"
        f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}")


@needs_cli
def test_the_validator_actually_rejects_an_unknown_field():
    """Guards the guard, and stands in for CI's manifest job.

    A `validate` that passed everything would make the test above vacuous, and
    the failure it is supposed to catch is precisely an unknown field: this
    repo shipped a `tryAsking` array for weeks on the belief that it did
    something, and the only thing that ever said otherwise was this validator.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "plugin"
        (target / ".claude-plugin").mkdir(parents=True)
        for name in ("plugin.json", "marketplace.json"):
            shutil.copy(MANIFEST_DIR / name, target / ".claude-plugin" / name)

        # the copy must be clean first, or the assertion below proves nothing
        assert validate(target).returncode == 0, (
            "the copied manifests do not validate on their own; this test "
            "cannot attribute the failure below to the injected field")

        doctored = json.loads((target / ".claude-plugin" / "plugin.json")
                              .read_text(encoding="utf-8"))
        doctored["thisFieldDoesNotExist"] = "and never will"
        (target / ".claude-plugin" / "plugin.json").write_text(
            json.dumps(doctored, indent=2), encoding="utf-8")

        r = validate(target)
        assert r.returncode != 0, (
            "the validator accepted an invented manifest field under --strict, "
            "so a green CI manifest job means nothing:\n" + r.stdout)
        assert "thisFieldDoesNotExist" in (r.stdout + r.stderr)


# --- the manifests carry what a public plugin needs ---------------------------


@pytest.mark.parametrize("field", [
    "name", "version", "description", "author",
    "homepage", "repository", "license", "keywords",
])
def test_plugin_json_carries_every_recommended_field(field):
    assert field in plugin_manifest(), f"plugin.json has no {field!r}"


@pytest.mark.parametrize("field", ["name", "description", "owner", "plugins"])
def test_marketplace_json_carries_every_recommended_field(field):
    assert field in marketplace_manifest(), f"marketplace.json has no {field!r}"


def test_the_marketplace_description_is_a_description():
    """The missing field was one of the two things `--strict` failed on, and a
    one-word placeholder would satisfy the presence check above while helping
    nobody choose whether to install this."""
    d = marketplace_manifest()["description"]
    assert len(d) > 60, f"marketplace description is too thin to be one: {d!r}"


def test_the_two_manifests_agree_on_the_version():
    """The marketplace entry repeats the version, so it can disagree with it.

    A marketplace advertising a version the plugin does not claim is a listing
    that resolves to something other than what it says.
    """
    entry, = marketplace_manifest()["plugins"]
    assert entry.get("version") == plugin_manifest()["version"], (
        "marketplace.json's plugin entry and plugin.json name different "
        "versions")


def test_the_declared_licence_is_the_one_in_the_licence_file():
    text = LICENSE.read_text(encoding="utf-8")
    assert plugin_manifest()["license"] == "MIT"
    assert "MIT License" in text
    assert "WITHOUT WARRANTY OF ANY KIND" in text


def test_the_licence_names_the_author_the_manifest_names():
    """A copyright line naming nobody is not a grant anyone can rely on."""
    author = plugin_manifest()["author"]["name"]
    line = next((l for l in LICENSE.read_text(encoding="utf-8").splitlines()
                 if l.startswith("Copyright")), None)
    assert line, "the LICENSE has no copyright line"
    assert author in line, f"the copyright line does not name {author!r}: {line}"


def test_the_repository_and_homepage_are_absolute_urls():
    m = plugin_manifest()
    for field in ("homepage", "repository"):
        assert m[field].startswith("https://"), (
            f"{field} is not a URL a stranger can open: {m[field]!r}")


# --- the version must not resolve to something already on disk ---------------


def cached_versions():
    """Every version directory the local plugin cache holds for this plugin.

    `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`. Absent on a
    machine that never installed this — including every CI runner — in which
    case there is nothing to collide with and the sweep is correctly empty.
    """
    name = plugin_manifest()["name"]
    cache = Path.home() / ".claude" / "plugins" / "cache"
    if not cache.is_dir():
        return {}
    found = {}
    for marketplace in cache.iterdir():
        plugin_dir = marketplace / name
        if not plugin_dir.is_dir():
            continue
        for version_dir in plugin_dir.iterdir():
            if version_dir.is_dir():
                found[version_dir.name] = version_dir
    return found


def test_this_version_does_not_resolve_to_a_different_artifact_in_the_cache():
    """An install lands in a directory named for the version, one per version,
    and old ones are never removed. So a version number that is already a
    directory here can be "installed" without a byte being fetched, and the
    clean-room gate would then pass against whatever that directory holds.

    On this machine that directory was 0.1.0, holding a scaffold whose `bin/`
    was a single README — a plausible pass on a plugin that does nothing.

    The check is deliberately NOT "the version differs from every cached
    directory". That form goes red the moment the author installs their own
    release, which is the normal state of a maintained plugin and not a defect.
    What is a defect is a cached directory under this version holding a
    DIFFERENT artifact — i.e. someone shipped twice under one number.
    """
    version = plugin_manifest()["version"]
    stale = cached_versions().get(version)
    if stale is None:
        return                       # nothing occupies this version: the goal
    cached_manifest = stale / ".claude-plugin" / "plugin.json"
    assert cached_manifest.is_file(), (
        f"{stale} occupies version {version} and is not even a plugin; an "
        f"install can resolve to it. Bump the version or delete the tree.")
    assert json.loads(cached_manifest.read_text(encoding="utf-8")) == \
        plugin_manifest(), (
        f"the plugin cache already holds a DIFFERENT artifact under version "
        f"{version} ({stale}). An install can resolve to it and fetch nothing. "
        f"Bump the version.")


def test_the_cache_sweep_would_notice_a_directory_at_all():
    """Guards the guard: a wrong cache path makes the check above pass on any
    version at all. Skips where there is genuinely no cache, which is the
    honest outcome on a runner and not a silent pass."""
    cache = Path.home() / ".claude" / "plugins" / "cache"
    if not cache.is_dir():
        pytest.skip("no local plugin cache on this machine")
    found = cached_versions()
    if not found:
        pytest.skip("this plugin has never been installed on this machine")
    assert all(re.fullmatch(r"\d+\.\d+\.\d+.*", v) for v in found), (
        f"the cache walk returned non-version directories: {sorted(found)}")


# --- CI ----------------------------------------------------------------------
#
# Presence and shape only. See the module docstring: a workflow is proved on
# GitHub or not at all, and pretending otherwise here would be worse than
# saying so.


def workflow():
    """The parsed workflow.

    `yaml.safe_load` reads the bare key `on` as the boolean True — YAML 1.1's
    booleans, which GitHub's own schema collides with — so triggers are looked
    up under True, not "on". A test that looked them up under "on" would find
    nothing and quietly assert nothing.
    """
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def triggers(wf):
    return wf.get("on", wf.get(True))


def test_the_workflow_is_valid_yaml_with_jobs_in_it():
    wf = workflow()
    assert isinstance(wf, dict)
    assert wf.get("jobs"), "the workflow declares no jobs"


def test_every_job_runs_on_macos():
    """The suite is platform-bound — launchd, `plutil`, BSD `stat` flags,
    zsh-only syntax. An ubuntu runner fails for reasons that have nothing to do
    with the change under test."""
    for name, job in workflow()["jobs"].items():
        runner = job.get("runs-on", "")
        assert "macos" in str(runner), f"job {name!r} runs on {runner!r}"


def test_some_job_runs_the_manifest_validator():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"claude plugin validate \S+ --strict", text), (
        "no job invokes the strict validator")


def test_some_job_runs_the_test_suite():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"-m pytest tests/", text), "no job runs the suite"


def test_the_workflow_says_how_the_claude_cli_is_acquired():
    """It is in no GitHub runner image. A workflow that just calls `claude`
    fails with `command not found` and reads like a broken repo."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert ("claude.ai/install.sh" in text
            or "@anthropic-ai/claude-code" in text), (
        "the workflow calls the claude CLI without installing it")


def test_there_is_a_scheduled_trigger_and_a_job_gated_to_it():
    """Push-triggered CI stops emitting signal once pushes stop, which is
    exactly when a dependency major ships and reaches a stranger's first
    install instead of the maintainer."""
    wf = workflow()
    assert "schedule" in triggers(wf), "the workflow has no schedule trigger"
    gated = [n for n, j in wf["jobs"].items()
             if "schedule" in str(j.get("if", ""))]
    assert gated, "nothing runs only on the schedule; the trigger is decorative"


def test_the_scheduled_job_installs_unpinned_and_checks_the_ceilings():
    wf = workflow()
    name = next(n for n, j in wf["jobs"].items()
                if "schedule" in str(j.get("if", "")))
    body = yaml.safe_dump(wf["jobs"][name])
    assert "requirements.txt" in body
    assert "specifier" in body or "ceiling" in body.lower(), (
        "the scheduled job installs unpinned but never compares what resolved "
        "against the declared bounds, so a new major that happens not to break "
        "the suite passes unnoticed")


def test_the_workflow_never_supplies_an_api_key():
    """P-KTD1. Every turn authenticates through the keychain OAuth credential;
    an API key in the environment silently moves billing off the subscription.
    "Add a repository secret" is therefore not an available fix for anything
    in this workflow, and the file must not quietly become the place one lands.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    # A YAML key assignment, anchored to line start — NOT a shell expansion.
    # `${ANTHROPIC_API_KEY:-}` is how the guard step reads the variable in
    # order to refuse it, and matching that would forbid the very check this
    # test wants the workflow to keep.
    assigned = re.findall(r"(?m)^\s*ANTHROPIC_API_KEY\s*:", text)
    assert not assigned, "the workflow assigns ANTHROPIC_API_KEY"
    assert "secrets.ANTHROPIC" not in text


def test_the_workflow_refuses_to_run_with_an_api_key_present():
    """The prohibition above is only a rule about this file. This asserts the
    workflow also enforces it at run time, so a key reaching a job some other
    way — an org-level variable, a fork's settings — stops the run instead of
    silently moving billing off the subscription."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"\$\{ANTHROPIC_API_KEY:-\}", text), (
        "no job checks the environment for an API key before running")


# --- the policy files exist and say something --------------------------------


@pytest.mark.parametrize("path", [LICENSE, SECURITY])
def test_the_policy_file_exists_and_is_not_empty(path):
    assert path.is_file(), f"{path.name} is missing"
    assert len(path.read_text(encoding="utf-8").strip()) > 200, (
        f"{path.name} is too short to be one")


def test_security_md_names_somewhere_to_send_a_report():
    """R16. This installs launchd daemons and handles bot tokens on strangers'
    machines; it will eventually receive a report, and a policy with no address
    in it sends that reporter to a public issue instead."""
    text = SECURITY.read_text(encoding="utf-8")
    has_url = re.search(r"https://\S+", text)
    points_at_manifest = "plugin.json" in text
    assert has_url or points_at_manifest, (
        "SECURITY.md names no disclosure channel at all")


def test_security_md_says_the_allowlist_is_not_a_finding():
    """The single most likely report this repo will get is "the agent can
    escape the permission allowlist", which is the documented design. Saying so
    in the policy is what keeps the honest reports from being buried under it.
    """
    text = SECURITY.read_text(encoding="utf-8").lower()
    assert "not a sandbox" in text
    assert "prompt injection" in text


def test_security_md_states_which_versions_are_supported():
    text = SECURITY.read_text(encoding="utf-8").lower()
    assert "latest tag" in text or "supported" in text


def test_the_readme_states_the_release_convention():
    """A tag is the unit of distribution here, so how one is cut and what a pin
    means has to live somewhere durable — not in a plan document a user never
    sees."""
    text = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8").lower()
    assert "tag is the unit of distribution" in text
    assert "commit sha" in text, (
        "the README does not say that a pin is a SHA rather than a tag name, "
        "which is the whole point: a tag is mutable and this is code launchd "
        "executes unattended")


# --- the contact address must not become a dead drop --------------------------

def test_a_noreply_author_address_is_never_offered_as_a_reporting_channel():
    """The manifests carry a GitHub users.noreply address: attributable, and it
    receives nothing. That is fine until a policy tells someone to write to it,
    at which point a vulnerability report is discarded in silence — the worst
    outcome available to this file. If the address ever becomes a real inbox
    again, this test goes red and the policy can offer it deliberately."""
    manifests = [json.loads((PLUGIN_ROOT / ".claude-plugin" / n).read_text())
                 for n in ("plugin.json", "marketplace.json")]
    emails = {(m.get("author") or m.get("owner") or {}).get("email") for m in manifests}
    emails.discard(None)
    assert emails, "no contact address in either manifest"

    security = (PLUGIN_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    if all(e.endswith("users.noreply.github.com") for e in emails):
        lowered = security.lower()
        assert "do not email" in lowered, (
            "the manifest address does not receive mail; SECURITY.md must say so")
        for verb in ("email the maintainer at", "write to the address",
                     "send an email to"):
            assert verb not in lowered, (
                f"SECURITY.md directs reporters to {verb!r}, but the manifest "
                f"address is a noreply that discards mail silently")
    # Whatever the address, a private channel has to exist.
    assert "security/advisories/new" in security
