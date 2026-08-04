"""Project inspection — what onboarding derives so it does not have to ask.

Three properties carry real weight here and the rest is parsing:

  1. **It runs before the venv exists.** Step 3 of the flow, step 9 builds the
     venv, so the only interpreter available is whatever the preflight found and
     the only packages are the standard library's. That is asserted as a
     SUBPROCESS under `PATH=/usr/bin:/bin` with `site` disabled — importing the
     module from a pytest process that has PyYAML installed proves nothing about
     the machine it will actually run on.

  2. **It must not leak a credential.** `~/.claude.json` and a project
     `.mcp.json` hold live keys in `env` blocks, `Authorization` headers, argv,
     and URL query strings. Everything this script prints is read by a model and
     lands in a transcript, so "a value never appears in stdout" is a security
     property, not tidiness.

  3. **The slug it derives becomes four things at once** — a launchd label, a
     lock filename, a state path, and a `pgrep -f` pattern. The cross-check
     against `instance_config` is deliberate: that module owns the rule, and a
     slug this script derives must satisfy it rather than resemble it.
"""
import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import instance_config

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PLUGIN_ROOT / "libexec" / "inspect-project.py"


def _load_module():
    """Imported by path: the filename has a hyphen, so it is not a module name.

    Every entry point in `libexec/` is executed, not imported, so this matches
    how the rest of the suite reaches the shell-adjacent scripts.
    """
    spec = importlib.util.spec_from_file_location("inspect_project", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ip = _load_module()


# --- fixtures -----------------------------------------------------------------

CLAUDE_MD = """\
# Widget Coach

Widget Coach keeps an eye on the widget line and says something when it drifts.

It is not a dashboard.

## How to reach the user

```sh
~/.local/share/widget/venv/bin/python3 scripts/notify.py "text"
scripts/rebuild.sh --full
```

State lives in `state/session-handoff.md` and the exports in `data/widgets/`.
Run `pytest tests/ -q` before touching `scripts/notify.py`.
"""

MCP_JSON = {
    "mcpServers": {
        "widgets": {
            "type": "stdio",
            "command": "/opt/homebrew/bin/uv",
            "args": ["run", "widget-mcp", "--api-key", "sk-live-PROJECTKEY000000000"],
            "env": {"WIDGET_API_KEY": "sk-live-PROJECTKEY000000000",
                    "WIDGET_BASE": "https://widgets.example"},
        },
    },
}

USER_JSON_TEMPLATE = {
    "mcpServers": {
        "notes": {
            "type": "http",
            "url": "https://mcp.example.com/mcp?apiKey=USERSCOPESECRET12345",
            "headers": {"Authorization": "Bearer USERSCOPESECRET12345"},
        },
    },
    "projects": {},
}

SECRETS = ("sk-live-PROJECTKEY000000000", "USERSCOPESECRET12345",
           "LOCALSCOPESECRET67890")


@pytest.fixture
def project(tmp_path):
    """A project with the three things onboarding cares about."""
    root = tmp_path / "widget-coach"
    (root / "scripts").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "CLAUDE.md").write_text(CLAUDE_MD, encoding="utf-8")
    (root / "README.md").write_text(
        "# Widget Coach\n\n"
        "[![build](https://img.shields.io/x)](https://example.com)\n\n"
        "Watches the widget line and complains when it drifts.\n\n"
        "## Install\n\nNot relevant to the summary.\n"
        + ("Filler paragraph. " * 200) + "\n",
        encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps(MCP_JSON), encoding="utf-8")
    notify = root / "scripts" / "notify.py"
    notify.write_text("#!/usr/bin/env python3\nprint('hi')\n", encoding="utf-8")
    notify.chmod(0o755)
    rebuild = root / "scripts" / "rebuild.sh"
    rebuild.write_text("#!/bin/zsh\necho rebuild\n", encoding="utf-8")
    rebuild.chmod(0o755)
    # No exec bit, on purpose: a fresh clone loses the mode all the time and the
    # file is still a script the user may want allowlisted.
    (root / "scripts" / "helper.py").write_text("x = 1\n", encoding="utf-8")
    (root / "bin" / "widgetctl").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "bin" / "widgetctl").chmod(0o755)
    (root / "notes.md").write_text("not a script\n", encoding="utf-8")
    return root


@pytest.fixture
def user_config(tmp_path, project):
    """A `~/.claude.json` with a user-scope server and a local-scope one."""
    data = json.loads(json.dumps(USER_JSON_TEMPLATE))
    data["projects"][str(project)] = {
        "mcpServers": {
            "local-thing": {
                "command": "npx",
                "args": ["-y", "local-thing"],
                "env": {"LOCAL_TOKEN": "LOCALSCOPESECRET67890"},
            },
        },
        "disabledMcpjsonServers": ["widgets"],
    }
    path = tmp_path / "claude.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def report(target, user_config_path=os.devnull):
    return ip.inspect(str(target), str(user_config_path))


def run_cli(target, user_config_path=os.devnull, interpreter=None, flags=(),
            env=None):
    cmd = [interpreter or sys.executable, *flags, str(SCRIPT), str(target),
           "--user-config", str(user_config_path)]
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


# --- what it finds ------------------------------------------------------------

def test_a_project_with_claude_md_mcp_json_and_scripts_reports_all_three(
        project, user_config):
    out = report(project, user_config)

    assert out["ok"] is True
    assert out["schema"] == ip.SCHEMA
    assert out["claude_md"]["exists"] is True
    assert out["claude_md"]["path"] == str(project / "CLAUDE.md")
    assert {s["name"] for s in out["mcp_servers"]} == {
        "widgets", "local-thing", "notes"}
    assert {s["path"] for s in out["scripts"]} == {
        "scripts/notify.py", "scripts/rebuild.sh", "scripts/helper.py",
        "bin/widgetctl"}
    assert out["git"]["is_repo"] is False   # a tmp dir is not in a repo


def test_the_derived_candidates_name_the_scripts(project, user_config):
    """The point of deriving anything: the flow offers real rules to opt in to,
    not a prompt asking the user to type out their own script paths."""
    rules = [c["rule"] for c in report(project, user_config)["allowlist_candidates"]]

    assert "Bash({python} scripts/notify.py *)" in rules
    assert "Bash(scripts/rebuild.sh *)" in rules
    assert "Bash(bin/widgetctl *)" in rules
    # The one without an exec bit is still offered — as a python rule, since it
    # would be run through an interpreter.
    assert "Bash({python} scripts/helper.py *)" in rules
    # ...and the CLAUDE.md commands are candidates too.
    assert any(r.startswith("Bash(pytest") for r in rules)


def test_a_candidate_rule_never_carries_a_shell_variable(project, user_config):
    """`$PY` in an allow rule is not late binding — it is a rule that never
    matches, because the matcher cannot know what it expands to (measured
    2026-07-28, see templates/permissions.json). The placeholder is `{python}`
    and the flow substitutes a literal path."""
    for candidate in report(project, user_config)["allowlist_candidates"]:
        assert "$" not in candidate["rule"], candidate


def test_a_python_invocation_becomes_an_interpreter_placeholder_rule(
        project, user_config):
    rules = [c["rule"] for c in report(project, user_config)["allowlist_candidates"]]
    assert "Bash({python} scripts/notify.py *)" in rules
    assert not any("venv/bin/python3" in r for r in rules), (
        "the CLAUDE.md's own venv path is another instance's; the flow "
        "substitutes THIS instance's interpreter")


def test_commands_come_out_of_fenced_blocks_and_inline_spans(project, user_config):
    commands = report(project, user_config)["claude_md"]["commands"]
    text = [c["command"] for c in commands]

    assert "scripts/rebuild.sh --full" in text
    assert "pytest tests/ -q" in text
    assert any(c["context"] == "fenced" for c in commands)
    assert all(isinstance(c["line"], int) and c["line"] > 0 for c in commands)


def test_a_file_or_directory_a_claude_md_merely_mentions_is_not_a_command(
        project, user_config):
    """`CLAUDE.md` files are full of path references. Reading `data/widgets/` as
    something to run would put a junk rule in front of a user who is being asked
    to approve rules one by one — which is how per-item confirmation degrades
    into clicking yes."""
    text = [c["command"] for c in report(project, user_config)["claude_md"]["commands"]]

    assert "data/widgets/" not in text
    assert "state/session-handoff.md" not in text
    assert "scripts/notify.py" not in text, (
        "a bare inline mention is a reference, not an invocation")


def test_a_bare_directory_returns_empty_findings_without_erroring(tmp_path):
    bare = tmp_path / "empty"
    bare.mkdir()
    out = report(bare)

    assert out["ok"] is True
    assert out["claude_md"] == {"exists": False, "path": None, "bytes": 0,
                                "commands": [], "commands_truncated": False,
                                "estate_markers": []}
    assert out["mcp_servers"] == []
    assert out["scripts"] == []
    assert out["allowlist_candidates"] == []
    assert out["project_description"]["source"] is None
    assert out["instance"]["slug"] == "empty"


def test_the_description_is_extracted_not_dumped(project, user_config):
    """The brief this feeds gets written into someone's CLAUDE.md and re-read on
    every turn, so a whole README pasted in is a permanent context tax."""
    description = report(project, user_config)["project_description"]

    assert description["source"] == "README.md"
    assert description["title"] == "Widget Coach"
    assert "drifts" in description["summary"]
    assert "img.shields.io" not in description["summary"], "badge line kept"
    assert len(description["summary"]) <= ip.SUMMARY_LIMIT + 2
    assert description["truncated"] is True


def test_the_claude_md_is_the_fallback_description(tmp_path):
    root = tmp_path / "no-readme"
    root.mkdir()
    (root / "CLAUDE.md").write_text(CLAUDE_MD, encoding="utf-8")

    out = report(root)["project_description"]
    assert out["source"] == "CLAUDE.md"
    assert out["title"] == "Widget Coach"
    assert "widget line" in out["summary"]


def test_existing_estate_markers_are_reported_as_sightings(tmp_path):
    """Re-invocation has to know the file already carries a block. The marker
    vocabulary belongs to claude-md-block.py; this only says it saw one."""
    root = tmp_path / "already-installed"
    root.mkdir()
    (root / "CLAUDE.md").write_text(
        "# X\n\n<!-- BEGIN telegram-agent-estate:ops -->\nstuff\n"
        "<!-- END telegram-agent-estate:ops -->\n", encoding="utf-8")
    markers = report(root)["claude_md"]["estate_markers"]

    assert [m["line"] for m in markers] == [3, 5]
    assert "telegram-agent-estate" in markers[0]["text"]


def test_a_git_repository_is_detected_from_a_subdirectory(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()

    assert report(repo / "sub")["git"] == {"is_repo": True, "root": str(repo)}
    assert report(tmp_path)["git"]["is_repo"] is False


# --- synced storage -----------------------------------------------------------

@pytest.mark.parametrize("relative,provider", [
    ("Dropbox/widget-coach", "Dropbox"),
    ("Dropbox (Personal)/widget-coach", "Dropbox"),
    ("Library/Mobile Documents/com~apple~CloudDocs/widget-coach", "iCloud Drive"),
    ("Library/CloudStorage/GoogleDrive-me@example.com/My Drive/widget-coach",
     "Google Drive"),
    ("Google Drive/widget-coach", "Google Drive"),
])
def test_synced_storage_is_flagged(tmp_path, relative, provider):
    """The single most common way this setup breaks: a venv inside a synced
    folder corrupts, and flock on a synced volume is not a lock."""
    root = tmp_path / relative
    root.mkdir(parents=True)
    out = report(root)

    assert out["synced_storage"]["synced"] is True
    assert out["synced_storage"]["provider"] == provider
    assert out["synced_storage"]["via"] == "path"
    assert any("venv" in w for w in out["warnings"])


def test_a_symlink_into_synced_storage_is_flagged_too(tmp_path):
    """A `~/projects/foo` symlink pointing into a synced folder is a normal way
    to organise things, and it looks entirely local from the path alone."""
    real = tmp_path / "Dropbox" / "widget-coach"
    real.mkdir(parents=True)
    link = tmp_path / "projects"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real, target_is_directory=True)
    out = report(link)

    assert out["synced_storage"]["synced"] is True
    assert out["synced_storage"]["via"] == "realpath"


def test_an_ordinary_directory_is_not_flagged(tmp_path):
    root = tmp_path / "projects" / "widget-coach"
    root.mkdir(parents=True)
    assert report(root)["synced_storage"] == {
        "synced": False, "provider": None, "matched": None, "via": None}


# --- MCP scope ----------------------------------------------------------------

def test_mcp_servers_are_reported_with_the_scope_they_came_from(
        project, user_config):
    """The user is deciding, per item, whether a server the bot may call is one
    they meant to expose. "This one is not from your repo, it is your machine's
    user-scope config" is most of that judgement."""
    by_name = {s["name"]: s for s in report(project, user_config)["mcp_servers"]}

    assert by_name["widgets"]["scope"] == "project"
    assert by_name["widgets"]["source"] == str(project / ".mcp.json")
    assert by_name["local-thing"]["scope"] == "local"
    assert str(project) in by_name["local-thing"]["scope_detail"]
    assert by_name["local-thing"]["source"] == str(user_config)
    assert by_name["notes"]["scope"] == "user"
    assert by_name["notes"]["scope_detail"] == "top-level mcpServers"


def test_a_per_project_disable_is_reported(project, user_config):
    by_name = {s["name"]: s for s in report(project, user_config)["mcp_servers"]}
    assert by_name["widgets"]["disabled_in_project"] is True
    assert by_name["local-thing"]["disabled_in_project"] is False


def test_a_server_declared_in_two_scopes_produces_a_warning(
        tmp_path, project, user_config):
    data = json.loads(user_config.read_text(encoding="utf-8"))
    data["mcpServers"]["widgets"] = {"command": "npx"}
    user_config.write_text(json.dumps(data), encoding="utf-8")
    out = report(project, user_config)

    assert any("more than one scope" in w and "widgets" in w
               for w in out["warnings"])
    assert sorted(s["scope"] for s in out["mcp_servers"] if s["name"] == "widgets") \
        == ["project", "user"]


def test_the_transport_and_a_usable_shorthand_survive_redaction(
        project, user_config):
    by_name = {s["name"]: s for s in report(project, user_config)["mcp_servers"]}

    assert by_name["widgets"]["transport"] == "stdio"
    assert by_name["widgets"]["command"] == "uv"     # basename only
    assert by_name["notes"]["transport"] == "http"
    assert by_name["notes"]["url_host"] == "mcp.example.com"


def test_a_malformed_mcp_json_warns_rather_than_ending_the_inspection(tmp_path):
    root = tmp_path / "broken"
    root.mkdir()
    (root / ".mcp.json").write_text("{not json", encoding="utf-8")
    (root / "CLAUDE.md").write_text("# X\n\nA thing.\n", encoding="utf-8")
    out = report(root)

    assert out["ok"] is True
    assert out["mcp_servers"] == []
    assert any("not valid JSON" in w for w in out["warnings"])
    assert out["project_description"]["title"] == "X"


# --- the secret boundary ------------------------------------------------------

def test_no_credential_value_appears_anywhere_in_the_output(project, user_config):
    """Everything printed here is read by a model and lands in a transcript.

    Covers all four hiding places at once: an `env` block, an argv, an
    `Authorization` header, and a URL query string."""
    out = report(project, user_config)
    serialized = json.dumps(out)

    for secret in SECRETS:
        assert secret not in serialized, f"{secret} leaked into the report"

    cli = run_cli(project, user_config)
    assert cli.returncode == 0
    for secret in SECRETS:
        assert secret not in cli.stdout, f"{secret} leaked into stdout"
        assert secret not in cli.stderr, f"{secret} leaked into stderr"


def test_the_variable_names_a_server_needs_are_reported_instead(
        project, user_config):
    """The names are the point: later steps write `${VAR}` into mcp-config.json
    and put the values in a mode-600 env file with the names in
    `env_passthrough`."""
    by_name = {s["name"]: s for s in report(project, user_config)["mcp_servers"]}

    assert by_name["widgets"]["env_var_names"] == ["WIDGET_API_KEY", "WIDGET_BASE"]
    assert by_name["local-thing"]["env_var_names"] == ["LOCAL_TOKEN"]
    assert by_name["widgets"]["values_withheld"] is True
    assert "env" in by_name["widgets"]["config_keys"]


def test_a_reference_to_a_variable_is_reported_as_a_variable(tmp_path):
    """A config that already uses `${VAR}` is the shape the flow wants to end
    up with — the name must survive, and it must not be mistaken for a value."""
    root = tmp_path / "vars"
    root.mkdir()
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "x": {"command": "${TOOL_PATH}", "env": {"KEY": "${SOME_TOKEN}"}}}}),
        encoding="utf-8")
    server = report(root)["mcp_servers"][0]

    assert server["env_var_names"] == ["KEY", "SOME_TOKEN", "TOOL_PATH"]


def test_a_credential_shaped_command_in_a_claude_md_is_redacted(tmp_path):
    """A project's own docs are not guaranteed clean either, and the command
    text is what the allowlist candidates are built from."""
    root = tmp_path / "leaky"
    root.mkdir()
    (root / "CLAUDE.md").write_text(
        "# L\n\n```sh\ncurl -H 'Authorization: Bearer ghp_AAAAAAAAAAAAAAAAAAAA' x\n"
        "scripts/x.sh --api-key=hunter2hunter2 --user ${USER_NAME}\n```\n",
        encoding="utf-8")
    out = report(root)
    text = json.dumps(out)

    assert "ghp_AAAAAAAAAAAAAAAAAAAA" not in text
    assert "hunter2hunter2" not in text
    assert ip.REDACTED in text
    assert "${USER_NAME}" in text, "a variable reference is not a secret"


# --- the slug -----------------------------------------------------------------

@pytest.mark.parametrize("dirname,expected", [
    ("widget-coach", "widget-coach"),
    ("Widget Coach", "widget-coach"),
    ("widget.coach.v2", "widget-coach-v2"),
    ("2026 notes", "agent-2026-notes"),
    ("  Spaced Out  ", "spaced-out"),
    ("weird__name!!", "weird-name"),
])
def test_the_derived_slug_is_the_expected_shape(dirname, expected):
    assert ip.slugify(dirname) == expected


@pytest.mark.parametrize("dirname", [
    "Widget Coach", "widget.coach.v2", "2026 notes", "weird__name!!",
    "ПроектЪ", "a" * 80, "-leading-dash-", "UPPER.CASE Name 9",
])
def test_a_derived_slug_is_safe_as_a_launchd_label_a_filename_and_a_pattern(
        dirname):
    """`instance_config` owns this rule — the slug must SATISFY it, not
    resemble it. The name becomes a launchd label, a lock filename, a state
    path and a `pgrep -f` pattern, and a slash, a dot or a space produces a
    different kind of wrong in each."""
    slug = ip.slugify(dirname)

    assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug), slug
    assert not set(slug) & set(".*+?[]()|^$\\{} \t/"), slug
    assert slug == os.path.basename(slug)
    assert len(slug) <= ip.MAX_SLUG + len(ip.SLUG_FALLBACK) + 1

    cfg = instance_config.InstanceConfig({
        "name": slug, "label": "X", "workdir": "/tmp/x",
        "python": "/usr/bin/python3",
        "telegram": {"owner_chat_id": 1, "token_file": "/tmp/env"},
        "runtime": {"state_dir": "/tmp/s", "log_dir": "/tmp/l"},
    })
    argv = f"/usr/bin/python3 /opt/estate/libexec/poller.py {cfg.instance_tag()}"
    assert re.search(cfg.poller_match(), argv)
    assert cfg.launchd_label().endswith(slug)


def test_a_slug_that_had_to_be_repaired_says_so():
    """The flow confirms rather than assumes, and it can only surface a
    surprising slug if the surprise is reported."""
    notes = []
    assert ip.slugify("2026 notes", notes) == "agent-2026-notes"
    assert any("digit" in n for n in notes)

    notes = []
    assert ip.slugify("ПроектЪ", notes) == ip.SLUG_FALLBACK
    assert any("placeholder" in n for n in notes)

    notes = []
    ip.slugify("a" * 80, notes)
    assert any("truncated" in n for n in notes)


@pytest.mark.parametrize("dirname,expected", [
    ("widget-coach", "Widget Coach"),
    ("widget_coach.v2", "Widget Coach V2"),
    ("SKMS Main", "SKMS Main"),
])
def test_the_human_label_keeps_what_a_person_wrote(dirname, expected):
    assert ip.humanize(dirname) == expected


def test_the_report_carries_both_names_and_where_they_came_from(tmp_path):
    root = tmp_path / "2. Widget Coach"
    root.mkdir()
    out = report(root)

    assert out["instance"]["slug"] == "agent-2-widget-coach"
    assert out["instance"]["label"] == "2 Widget Coach"
    assert out["instance"]["derived_from"] == "2. Widget Coach"
    assert out["instance"]["notes"]


# --- the interpreter constraint -----------------------------------------------

def _interpreters():
    """`sys.executable -S` proves independence from the venv's site-packages;
    `/usr/bin/python3` proves it also runs on the interpreter a clean macOS
    actually has, which is the one a preflight may well resolve to."""
    out = [pytest.param(sys.executable, id="suite-interpreter")]
    if os.path.exists("/usr/bin/python3"):
        out.append(pytest.param("/usr/bin/python3", id="system-python"))
    return out


@pytest.mark.parametrize("interpreter", _interpreters())
def test_it_runs_with_no_third_party_packages_available(
        interpreter, project, user_config, tmp_path):
    """THE constraint. This executes at step 3 and the venv is built at step 9,
    so an `import yaml` here is a flow that dies before it has asked anything.

    `-S` drops site-packages (where PyYAML lives), `-I` drops PYTHONPATH and the
    user site directory, and the PATH is the one the plists stamp minus the
    Homebrew prefix. Asserted as a subprocess: importing the module from a
    pytest process that already has every dependency loaded would prove nothing.
    """
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")}
    result = run_cli(project, user_config, interpreter=interpreter,
                     flags=["-I", "-S"], env=env)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    parsed = json.loads(result.stdout)
    assert parsed["ok"] is True
    assert parsed["schema"] == ip.SCHEMA
    assert {s["name"] for s in parsed["mcp_servers"]} == {
        "widgets", "local-thing", "notes"}


@pytest.mark.skipif(not hasattr(sys, "stdlib_module_names"),
                    reason="needs python 3.10+ to know what the stdlib is")
def test_every_import_in_the_module_is_stdlib():
    """The subprocess test catches this on the machines it runs on; this one
    names the offending import instead of failing with a ModuleNotFoundError,
    and it catches a lazily-imported package inside a function."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    assert roots
    third_party = sorted(r for r in roots if r not in sys.stdlib_module_names)
    assert not third_party, (
        f"inspect-project.py runs before the venv exists and may import stdlib "
        f"only; it imports {third_party}")


# --- failing usefully ---------------------------------------------------------

def test_a_missing_target_fails_with_a_message_not_a_traceback(tmp_path):
    result = run_cli(tmp_path / "nope")

    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "no such directory" in result.stderr
    assert str(tmp_path / "nope") in result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not-found"


def test_a_file_is_not_a_project(tmp_path):
    target = tmp_path / "a-file"
    target.write_text("x", encoding="utf-8")
    result = run_cli(target)

    assert result.returncode == 2
    assert "not a directory" in result.stderr
    assert json.loads(result.stdout)["error"]["code"] == "not-a-directory"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_an_unreadable_target_says_so(tmp_path):
    root = tmp_path / "locked"
    root.mkdir()
    root.chmod(0o000)
    try:
        result = run_cli(root)
    finally:
        root.chmod(0o755)

    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "permission denied" in result.stderr.lower()
    assert json.loads(result.stdout)["error"]["code"] == "unreadable"


def test_a_missing_user_config_is_not_an_error(project, tmp_path):
    """A stranger's machine may have no `~/.claude.json` at all — that is a
    project with no user-scope servers, not a failed inspection."""
    out = report(project, tmp_path / "no-such-claude.json")

    assert out["ok"] is True
    assert {s["name"] for s in out["mcp_servers"]} == {"widgets"}
    assert out["warnings"] == []


# --- the contract the next part depends on ------------------------------------

def test_the_report_is_json_serializable_and_declares_its_opt_ins(
        project, user_config):
    """`opt_in_required` is contract, not commentary: under `dontAsk` an allow
    entry means the bot runs that thing with no prompt, so a repo containing
    `scripts/deploy-prod.sh` would otherwise put production one message away."""
    out = report(project, user_config)
    round_tripped = json.loads(json.dumps(out))

    assert round_tripped == out
    assert out["opt_in_required"] == ["mcp_servers", "allowlist_candidates"]
    for key in ("schema", "ok", "workdir", "instance", "git", "synced_storage",
                "claude_md", "project_description", "mcp_servers", "scripts",
                "allowlist_candidates", "warnings"):
        assert key in out, key
