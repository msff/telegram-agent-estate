"""The shipped templates, checked as templates rather than as one bot's config.

`templates/` is the only part of this repo a stranger edits by hand, and until
U2 it was first-instance's live configuration with the comments still
attached: real chat id, real `/Users/...` paths, three schedules whose prompt
files did not ship, and an allowlist naming another instance's private scripts.
Every test here pins one way that regresses.

The interesting one is `test_every_key_the_code_reads_is_in_the_template`. It
walks `instance_config.py` for the KEY LITERALS the accessors read, not for the
accessor NAMES: about ten accessors are named differently from the key they
read (`gateway_state_dir` <- `runtime.state_dir`, `prime_prompt_file` <-
`prompts.prime`, `handoff_path` <- `housekeeping.handoff_file`, …), so a
name-based comparison would report those ten as missing while a genuinely
undocumented key sailed through.
"""
import ast
import json
import re
from pathlib import Path

import pytest
import yaml

import instance_config as ic

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = PLUGIN_ROOT / "templates"
TEMPLATE_CONFIG = TEMPLATES / "instance.yaml"
TEMPLATE_PERMS = TEMPLATES / "permissions.json"
OPS_SECTION = TEMPLATES / "CLAUDE-ops-section.md"
PROMPTS = TEMPLATES / "prompts"

# The runner reads these four unconditionally; the three below back the example
# schedules. Both sets ship, in the language templates/prompts/README.md
# declares.
RUNNER_PROMPTS = ("prime.txt", "flush.txt",
                  "silent-directive.txt", "reply-directive.txt")
SCHEDULE_PROMPTS = ("morning.txt", "evening.txt", "weekly.txt")


def template_data():
    return yaml.safe_load(TEMPLATE_CONFIG.read_text(encoding="utf-8"))


def template_files():
    return [p for p in TEMPLATES.rglob("*") if p.is_file()]


# --- it is still a loadable config ------------------------------------------


def test_the_template_parses_and_loads():
    """A template that does not load is a template nobody can start from."""
    cfg = ic.load(TEMPLATE_CONFIG)
    assert cfg.name and cfg.label
    assert cfg.workdir.is_absolute() and cfg.python.is_absolute()


def test_loading_the_template_yields_every_documented_field():
    """Touch every accessor. A field documented in the template but misspelled
    (or a required one quietly dropped) surfaces here rather than at 03:00 in a
    housekeeping run nobody watches."""
    c = ic.load(TEMPLATE_CONFIG)
    values = {
        "name": c.name, "label": c.label, "workdir": c.workdir,
        "python": c.python, "launchd_prefix": c.launchd_prefix,
        "owner_chat_id": c.owner_chat_id, "token_file": c.token_file,
        "channel_state_dir": c.channel_state_dir,
        "gateway_state_dir": c.gateway_state_dir,
        "agent_state_dir": c.agent_state_dir, "log_dir": c.log_dir,
        "backend": c.backend, "turn_timeout": c.turn_timeout,
        "job_timeout": c.job_timeout, "quota_defer_at": c.quota_defer_at,
        "max_retries": c.max_retries, "permission_mode": c.permission_mode,
        "permission_settings": c.permission_settings,
        "mcp_config": c.mcp_config, "env_passthrough": c.env_passthrough,
        "prime_prompt_file": c.prime_prompt_file,
        "flush_prompt_file": c.flush_prompt_file,
        "silent_directive_file": c.silent_directive_file,
        "reply_directive_file": c.reply_directive_file,
        "housekeeping_hour": c.housekeeping_hour,
        "handoff_path": c.handoff_path,
        "housekeeping_script": c.housekeeping_script,
        "log_cap_bytes": c.log_cap_bytes,
        "handoff_snapshot_pattern": c.handoff_snapshot_pattern,
        "handoff_order": c.handoff_order,
        "msg_index_retention_days": c.msg_index_retention_days,
        "schedules": c.schedules,
    }
    assert values["backend"] in ("A", "B")
    assert values["permission_mode"] == "dontAsk"
    assert values["handoff_order"] in ("date", "file")
    assert values["env_passthrough"] == ["TELEGRAM_STATE_DIR"]
    assert len(values["schedules"]) >= 1
    assert c.launchd_label("morning-brief").endswith(f".{c.name}-morning-brief")
    # The four prompt overrides ship UNSET so the plugin's own defaults apply;
    # a template that pointed them at files it does not carry would crash every
    # turn on the first read.
    for key in ("prime_prompt_file", "flush_prompt_file",
                "silent_directive_file", "reply_directive_file"):
        assert values[key] is None, f"{key} must ship unset, got {values[key]}"


def test_launchd_prefix_is_declared_by_the_template_not_inherited():
    """The code's fallback prefix is the author's reverse domain, so a template
    that stays silent about this field puts every third-party install under it.
    Declared explicitly, an installer can see and replace it."""
    data = template_data()
    assert "launchd_prefix" in data, "the template must declare launchd_prefix"
    prefix = data["launchd_prefix"]
    assert prefix and prefix != ic.InstanceConfig(
        {**data, "launchd_prefix": None}).launchd_prefix, (
        "the template's launchd_prefix must not be the code's personal default")


def test_agent_state_dir_is_derived_and_needs_no_template_key():
    """Excluded from the key sweep below on purpose: it reads no config key at
    all, so there is nothing for the template to document."""
    c = ic.load(TEMPLATE_CONFIG)
    assert c.agent_state_dir == c.workdir / "state"


# --- every key the code can read is documented ------------------------------


def _config_key_literals():
    """String literals `instance_config.py` uses as config keys.

    AST, not grep and not `dir(InstanceConfig)`: the accessor names are not the
    keys (see module docstring), and a regex over the source would also collect
    the key names that appear in comments and error messages.
    """
    tree = ast.parse((PLUGIN_ROOT / "bin" / "instance_config.py")
                     .read_text(encoding="utf-8"))
    keys = set()

    def _const_str(node):
        return node.value if isinstance(node, ast.Constant) and isinstance(
            node.value, str) else None

    for node in ast.walk(tree):
        # `_dig(self._data, "housekeeping.handoff_file")` — dotted paths.
        if isinstance(node, ast.Call):
            fn = node.func
            if (isinstance(fn, ast.Name) and fn.id == "_dig"
                    and len(node.args) >= 2):
                lit = _const_str(node.args[1])
                if lit:                      # `_dig(self._data, k)` in the
                    keys.add(lit)            # REQUIRED loop is not a literal
            # `self._data.get("schedules")` — top-level keys with a default.
            if (isinstance(fn, ast.Attribute) and fn.attr == "get"
                    and isinstance(fn.value, ast.Attribute)
                    and fn.value.attr == "_data" and node.args):
                lit = _const_str(node.args[0])
                if lit:
                    keys.add(lit)
        # `self._data["name"]` — the required identity fields, which raise
        # rather than defaulting.
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "_data"):
            lit = _const_str(node.slice)
            if lit:
                keys.add(lit)
    return keys


def _resolves(data, dotted):
    """True when every segment of `dotted` is a KEY in the template.

    Presence, not truthiness: an optional field ships as `mcp_config:` with no
    value, which documents it while leaving the code on its fallback.
    """
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def test_the_key_sweep_reads_keys_and_not_accessor_names():
    """Guards the guard. If this ever collects `gateway_state_dir` instead of
    `runtime.state_dir`, the sweep below has silently become a name comparison
    and will pass on a template missing half its fields."""
    keys = _config_key_literals()
    assert len(keys) >= 25, f"the AST walk collected too little: {sorted(keys)}"
    for key in ("runtime.state_dir", "prompts.prime",
                "housekeeping.handoff_file", "telegram.owner_chat_id"):
        assert key in keys
    for accessor in ("gateway_state_dir", "prime_prompt_file", "handoff_path",
                     "agent_state_dir"):
        assert accessor not in keys


def test_every_key_the_code_reads_is_in_the_template():
    data = template_data()
    keys = _config_key_literals() | set(ic.REQUIRED)
    missing = sorted(k for k in keys if not _resolves(data, k))
    assert not missing, (
        "instance_config reads config keys the template never mentions, so an "
        f"installer cannot know they exist: {missing}")


# --- nothing personal ships --------------------------------------------------


PERSONAL = {
    "the owner chat id": re.compile(r"\b10000000001\b"),
    "the author's home dir": re.compile(r"/Users/example\b"),
    "the author's reverse domain": re.compile(r"\bco\.example\b|\bexample\b"),
    "another instance's slug": re.compile(r"first-instance|second-instance"),
    "a synced-folder path": re.compile(r"~/Dropbox|/vault\b"),
}


@pytest.mark.parametrize("what,pattern", sorted(PERSONAL.items()))
def test_no_template_file_carries_personal_data(what, pattern):
    hits = []
    for path in template_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(PLUGIN_ROOT)}:{n}: {line.strip()}")
    assert not hits, f"{what} appears in a shipped template:\n" + "\n".join(hits)


# --- the schedules point at prompts that exist -------------------------------


def _resolve_prompt(pf, workdir):
    """run-job.sh's resolution order: instance-relative, then plugin-relative."""
    p = Path(pf).expanduser()
    if p.is_absolute():
        return p
    return p if (workdir / p).exists() else PLUGIN_ROOT / p


def test_every_shipped_schedule_names_a_prompt_file_that_exists():
    """A missing prompt_file is not a startup error — `run-job.sh` exits 2 on
    the schedule, every day, and only that job's log says so. All three of the
    template's schedules shipped broken this way."""
    c = ic.load(TEMPLATE_CONFIG)
    for entry in c.schedules:
        pf = entry.get("prompt_file")
        assert pf, f"schedule {entry.get('job')!r} has no prompt_file"
        resolved = _resolve_prompt(pf, c.workdir)
        assert resolved.exists(), (
            f"schedule {entry.get('job')!r} points at {pf}, which resolves to "
            f"{resolved} — nothing ships there")


def test_the_shipped_schedule_prompts_are_the_ones_the_template_names():
    named = {Path(e["prompt_file"]).name for e in ic.load(TEMPLATE_CONFIG).schedules}
    assert named == set(SCHEDULE_PROMPTS)


# --- the permission skeleton -------------------------------------------------


def _perms():
    return json.loads(TEMPLATE_PERMS.read_text(encoding="utf-8"))["permissions"]


def _rule_arg(rule):
    return rule[rule.index("(") + 1:rule.rindex(")")]


def test_permission_skeleton_is_valid_json_and_still_dontAsk():
    perms = _perms()
    assert perms["defaultMode"] == "dontAsk"
    assert not any("dangerously" in r.lower() for r in perms["allow"])


def test_no_allow_rule_names_a_path_outside_the_instance_workdir():
    """The shipped allowlist may only carry rules that are true for every
    instance, and that means workdir-relative ones. The version this replaced
    allowed seven scripts of another bot's repo under an absolute venv path —
    rules that grant a stranger nothing and tell an attacker where to look."""
    outside = re.compile(r"(?:^|[\s\"'=])(?:~|/)")
    offenders = [r for r in _perms()["allow"] if outside.search(_rule_arg(r))]
    assert not offenders, f"allow rules leaving the workdir: {offenders}"


def test_no_write_rule_survives_on_either_side():
    """MEASURED 2026-08-03, four probe turns under `dontAsk` — the CLI ignores
    `Write(path)` rules entirely and says so on every turn's stderr, once per
    rule, whether or not a file tool is used: "Write(...) is not matched by file
    permission checks — only Edit(path) rules are."

    The deny-side ones were the arguable case, since Edit needs an existing file
    while Write creates one. The probes closed it: `allow Edit(~/probe/**)`
    alone let the Write tool CREATE a file, and adding `deny Edit(~/probe/**)`
    stopped it. Edit rules cover creation, so the gap the Write rules guarded
    does not exist and the rules guarding it did nothing but add log noise."""
    perms = _perms()
    strays = [r for r in perms["allow"] + perms["deny"] if r.startswith("Write(")]
    assert not strays, f"inert Write rules, one stderr warning per turn: {strays}"
    assert "Edit(./state/**)" in perms["allow"], (
        "the rotation edits the handoff every night; without this rule it "
        "silently writes nothing and the day's context is gone")


CORE_DENIED_PATHS = (
    "~/.ssh/**", "~/.aws/**", "~/.gnupg/**", "~/.netrc",
    "~/.zshrc", "~/.zshenv", "~/.zprofile",
    "~/.bashrc", "~/.bash_profile", "~/.profile",
    "~/.claude.json", "~/Library/Keychains/**", "//etc/**",
)


@pytest.mark.parametrize("path", CORE_DENIED_PATHS)
def test_deny_keeps_the_generic_credential_paths(path):
    """The deny array is the only real protection in this file, and every entry
    in it names a home or system path BY CONSTRUCTION — so "no path outside the
    workdir" cannot be asserted here without deleting the protection."""
    deny = _perms()["deny"]
    for verb in ("Read", "Edit"):
        assert f"{verb}({path})" in deny, f"{verb} is not denied on {path}"


def test_deny_covers_the_persistence_primitives():
    """~/.ssh/authorized_keys and a fresh plist in LaunchAgents are the two
    persistent-backdoor primitives that matter on a machine where this plugin
    installs launchd jobs. `Edit(<path>)` is what stops both — including
    creation, which probe 3 above confirmed."""
    deny = _perms()["deny"]
    for path in ("~/.ssh/**", "~/Library/LaunchAgents/**", "~/.claude/**"):
        assert f"Edit({path})" in deny


def test_deny_names_no_user_specific_path():
    deny = _perms()["deny"]
    for pattern in PERSONAL.values():
        assert not [r for r in deny if pattern.search(r)]
    assert not [r for r in deny if "/Users/" in r]


# --- prompts -----------------------------------------------------------------


def _declared_prompt_language():
    m = re.search(r"shipped-prompt-language:\s*([a-z]{2})",
                  (PROMPTS / "README.md").read_text(encoding="utf-8"))
    assert m, "templates/prompts/README.md must declare the shipped language"
    return m.group(1)


CYRILLIC = re.compile(r"[Ѐ-ӿ]")


@pytest.mark.parametrize("name", RUNNER_PROMPTS + SCHEDULE_PROMPTS)
def test_shipped_prompts_are_in_the_declared_language(name):
    """A prompt in the wrong language degrades the one turn whose whole job is
    restoring context, and that failure leaves no other trace. Shipping the
    Russian set as the default would have made silent context loss the
    out-of-box behaviour for every non-Russian installer."""
    assert _declared_prompt_language() == "en"
    text = (PROMPTS / name).read_text(encoding="utf-8")
    assert text.strip(), f"{name} is empty"
    assert not CYRILLIC.search(text), f"{name} is not in the declared language"


def test_the_russian_set_still_ships_as_an_example_override():
    ru = PROMPTS / "examples" / "ru"
    assert {p.name for p in ru.glob("*.txt")} == set(RUNNER_PROMPTS)
    for p in ru.glob("*.txt"):
        assert CYRILLIC.search(p.read_text(encoding="utf-8"))


def test_prompt_placeholders_survive_translation():
    """`{handoff}` and `{chat_id}` are substituted by the runner. A prompt that
    spells the path out instead points a differently-configured instance at a
    file that does not exist — and that turn exits 0 having restored nothing."""
    for name in ("prime.txt", "flush.txt"):
        for base in (PROMPTS, PROMPTS / "examples" / "ru"):
            assert "{handoff}" in (base / name).read_text(encoding="utf-8")
    for base in (PROMPTS, PROMPTS / "examples" / "ru"):
        assert "{chat_id}" in (base / "reply-directive.txt").read_text(
            encoding="utf-8")


def test_the_silent_directives_forbid_sending():
    for base in (PROMPTS, PROMPTS / "examples" / "ru"):
        for name in ("silent-directive.txt", "flush.txt"):
            text = (base / name).read_text(encoding="utf-8")
            assert "Telegram" in text and "{chat_id}" not in text


# --- the ops section a new user pastes into their CLAUDE.md ------------------


def _send_py_options():
    """`send.py`'s CLI as argparse declares it: (positionals, flags)."""
    tree = ast.parse((PLUGIN_ROOT / "bin" / "send.py").read_text(encoding="utf-8"))
    positional, flags = [], set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument" and node.args
                and isinstance(node.args[0], ast.Constant)):
            name = node.args[0].value
            (flags.add(name) if name.startswith("-") else positional.append(name))
    return positional, flags


def _ops_send_command():
    lines = [l.strip() for l in OPS_SECTION.read_text(encoding="utf-8").splitlines()
             if "send.py" in l and not l.strip().startswith(("-", "*", ">"))]
    assert len(lines) == 1, f"expected one send command, found {lines}"
    return lines[0]


def test_the_ops_section_send_command_matches_send_py():
    """It used to name `scripts/send_transaction.py reply --text "…"` — a script
    that does not ship, with a CLI shape send.py does not have. A brain told to
    run that command cannot reply at all, and a turn that cannot reply is
    retried and then dead-lettered."""
    positional, flags = _send_py_options()
    cmd = _ops_send_command()
    assert "send_transaction.py" not in OPS_SECTION.read_text(encoding="utf-8")
    assert (PLUGIN_ROOT / "bin" / "send.py").exists()
    # Pin the shape that makes the documented command runnable — one positional
    # `text`, and `--chat-id` still optional — rather than the exact flag set.
    # send.py may legitimately grow flags the ops section has no reason to name
    # (`--config` exists for humans and setup flows; a turn is handed the
    # instance through the environment), and freezing the set here would fail on
    # every one of them without telling anyone anything.
    assert positional == ["text"]
    assert "--chat-id" in flags
    # Whatever the ops command DOES use has to be real, though.
    used = {tok for tok in cmd.split() if tok.startswith("--")}
    assert used <= flags, f"ops section uses flags send.py does not have: {used - flags}"

    tail = cmd.split("send.py", 1)[1].strip()
    assert tail.startswith('"'), (
        f"the text is send.py's first positional argument, but the command "
        f"passes {tail.split()[0]!r} before it")
    # Flags named anywhere the section talks about send.py, not just on the
    # command line — the prose around it is what the brain reads too.
    prose = OPS_SECTION.read_text(encoding="utf-8")
    named = re.findall(r"`(--[\w-]+)`", prose) + re.findall(r"--[\w-]+", cmd)
    for used in named:
        assert used in flags, f"the ops section names {used}, send.py does not"


def test_the_ops_section_has_insertion_markers():
    """U8 re-inserts this block on upgrade; without delimiters it would append a
    second copy, and the instance's own CLAUDE.md edits could not be told from
    the managed text."""
    text = OPS_SECTION.read_text(encoding="utf-8")
    begin, end = "<!-- BEGIN telegram-agent-estate:ops -->", \
                 "<!-- END telegram-agent-estate:ops -->"
    assert text.count(begin) == 1 and text.count(end) == 1
    assert text.index(begin) < text.index(end)
    assert "## How you are running" in text[text.index(begin):text.index(end)]
