"""The onboarding flow, held to the parts of it that are not style.

`SKILL.md` is prose, and most prose does not deserve a test. These properties
do, because each one is a thing the flow can quietly stop doing while still
reading perfectly well:

  1. **It names commands that exist.** A skill is executed by an agent, so a
     stale path is not a typo — it is a step that fails halfway through an
     install, after the token file has already been written.

  2. **The disclosure comes before the first write.** A user who learns at the
     verification step that a timer now runs unattended as them was told too
     late, and the ordering is the whole of that guarantee.

  3. **The token is never echoed.** The one secret in this flow passes through a
     terminal the agent cannot see, and every convenient way to check on it —
     `cat`, `echo $TOKEN`, `curl -v` — writes it into a transcript on disk.

  4. **Chat-id discovery is private-only and drains.** A group chat id in
     `owner_chat_id` hands the agent to everyone in the group.

  5. **The step numbers `libexec/README.md` cites are the real ones.** That file
     documents two scripts by their position in this flow; renumbering here
     silently makes it wrong.

What is deliberately NOT asserted: wording, length, or the presence of any
particular sentence. Those change with every honest edit, and a test that
fights them trains people to edit the test.
"""
import re
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SKILL = PLUGIN_ROOT / "skills" / "setup" / "SKILL.md"
LIBEXEC_README = PLUGIN_ROOT / "libexec" / "README.md"
DISPATCHER = PLUGIN_ROOT / "bin" / "telegram-agent-estate"

BODY = SKILL.read_text(encoding="utf-8")


def frontmatter():
    assert BODY.startswith("---\n"), "no YAML frontmatter — the skill will not load"
    end = BODY.index("\n---\n", 3)
    out = {}
    for line in BODY[4:end].splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            out[k.strip()] = v.strip()
    return out


def steps():
    """`{step number: (heading, character offset)}` for the numbered install steps."""
    out = {}
    for m in re.finditer(r"^### (\d+)\. (.+)$", BODY, re.M):
        out[int(m.group(1))] = (m.group(2), m.start())
    return out


def code():
    """Only the fenced blocks — the lines an agent following this actually runs.

    The distinction matters for the token rules below: prose that says "never
    run `curl -v`" contains the string it forbids, and a check that cannot tell
    the warning from the command would push the warning out of the document.
    """
    return "\n".join(m.group(1) for m in
                     re.finditer(r"^```\w*\n(.*?)^```", BODY, re.S | re.M))


# --- 1. it names things that exist --------------------------------------------

def test_frontmatter_declares_the_skill_and_what_it_covers():
    fm = frontmatter()
    assert fm.get("name") == "setup"
    desc = fm.get("description", "")
    assert desc, "no description — nothing would route to this skill"
    # The five verbs the flow implements. A verb missing here is a verb no one
    # reaches by asking for it in their own words.
    for verb in ("set up", "migrate", "upgrade", "uninstall"):
        assert verb in desc.lower(), f"{verb!r} is implemented but not discoverable"


@pytest.mark.parametrize("script", ["inspect-project.py", "claude-md-block.py"])
def test_every_libexec_script_it_calls_exists(script):
    assert script in BODY, f"{script} is no longer part of the flow — deliberate?"
    assert (PLUGIN_ROOT / "libexec" / script).exists()


def test_every_dispatcher_verb_it_uses_is_a_real_subcommand():
    used = set(re.findall(r"telegram-agent-estate (\w+)", code()))
    routed = re.search(r"typeset -A TARGETS=\((.*?)\n\)",
                       DISPATCHER.read_text(encoding="utf-8"), re.S)
    declared = set(re.findall(r"^\s*(\w+)\s+\S+", routed.group(1), re.M))
    unknown = used - declared
    assert not unknown, f"the skill calls subcommands that do not exist: {unknown}"


def test_every_template_it_tells_the_user_to_copy_exists():
    for rel in re.findall(r"templates/[\w./-]+", BODY):
        path = PLUGIN_ROOT / rel
        assert path.exists(), f"{rel} is referenced by the skill but not shipped"


def test_the_step_numbers_libexec_readme_cites_are_the_real_ones():
    """`libexec/README.md` documents two scripts by their position in this flow.
    Renumbering the steps without touching that file makes it quietly wrong."""
    numbered = steps()
    assert numbered, "the install flow has no numbered steps"
    for script, cited in re.findall(
            r"`([\w.-]+\.py)` \| \*new\* \| onboarding step (\d+)", LIBEXEC_README.read_text(encoding="utf-8")):
        n = int(cited)
        assert n in numbered, f"{script} is cited as step {n}, which does not exist"
        _, offset = numbered[n]
        nxt = min((o for k, (_, o) in numbered.items() if o > offset), default=len(BODY))
        assert script in BODY[offset:nxt], (
            f"{script} is cited as onboarding step {n}, but step {n} does not call it")


# --- 2. the disclosure precedes the first write --------------------------------

def test_the_not_a_sandbox_disclosure_comes_before_anything_is_written():
    """R15. The steps that create a venv, a token file, a config, launchd jobs
    or a CLAUDE.md edit all have to sit after it — being honest afterwards is
    not the same promise."""
    numbered = steps()
    at = BODY.lower().index("not a sandbox")
    # Which numbered step the disclosure lives in. Everything that writes must
    # be a LATER step — the disclosure's own step is allowed to write nothing,
    # and does not.
    disclosure_step = max(n for n, (_, o) in numbered.items() if o < at)

    writing = [n for n, (heading, _) in numbered.items()
               if re.search(r"venv|token|config|install|allowlist|blocks", heading, re.I)]
    assert writing, "no step looks like it writes anything — did the flow change shape?"
    for n in writing:
        assert n > disclosure_step, (
            f"step {n} ({numbered[n][0]!r}) writes at or before the disclosure "
            f"in step {disclosure_step}")


def test_the_disclosure_enumerates_what_will_be_created():
    """"It is not a sandbox" without a list of what lands on disk is a
    reassurance, not a disclosure."""
    para = BODY[BODY.lower().index("not a sandbox"):][:1600].lower()
    for thing in ("venv", "token", "launchd", "claude.md"):
        assert thing in para, f"the disclosure does not mention {thing}"


def test_it_asks_for_a_yes_rather_than_announcing():
    para = BODY[BODY.lower().index("not a sandbox"):][:2000].lower()
    assert "yes" in para, "the disclosure never asks the user to agree"


# --- 3. the token is never echoed ----------------------------------------------

@pytest.mark.parametrize("forbidden,why", [
    (r"cat\s+~?/?[\w./<>-]*env\b", "cat of the env file prints the token"),
    (r"echo\s+.*TELEGRAM_BOT_TOKEN", "echoing the token writes it to the transcript"),
    (r"curl\s+(-\w*v|--verbose)", "curl -v prints the URL, and the URL carries the token"),
])
def test_the_flow_contains_no_token_disclosing_command(forbidden, why):
    hits = [l for l in code().splitlines() if re.search(forbidden, l)]
    assert not hits, f"{why}: {hits}"


def test_the_token_is_captured_with_hidden_input_and_locked_down():
    assert "read -rs" in BODY, "the token is read with visible input"
    assert "umask 077" in BODY or "chmod 600" in BODY
    assert "chmod 600" in BODY, "the token file's mode is never set"


def test_the_verification_step_checks_shape_not_content():
    """`grep -c` prints a count; `grep` alone prints the line, which is the
    token. The difference is the whole of this step."""
    assert re.search(r"grep -c\b", BODY), "no count-only check of the token file"
    assert "stat -f" in BODY, "the file's mode is never verified"


def test_it_says_where_the_token_may_not_live():
    """The auto-loading channel plugin polls any token it finds, so a token in
    a plist or a turn's environment 409-wars this instance's own poller."""
    section = BODY[BODY.index("read -rs"):][:2000]
    assert "409" in section
    for place in ("plist", "repo"):
        assert place in section.lower(), f"the flow does not rule out {place}"


# --- 4. chat-id discovery ------------------------------------------------------

def test_owner_discovery_requires_a_private_chat():
    section = BODY[BODY.index("getUpdates"):][:2500]
    assert '"private"' in section or "== \"private\"" in section, (
        "nothing constrains the chat type; a group id here hands the agent to "
        "every member of that group")
    assert "chat" in section and "type" in section


def test_owner_discovery_uses_a_nonce_and_drains_the_backlog():
    section = BODY[BODY.index("getUpdates"):][:2500]
    assert "nonce" in BODY.lower()
    assert "offset=" in section, (
        "the backlog is never drained, so the poller answers the nonce message "
        "as its first turn")


def test_repair_does_not_poll_a_live_bot():
    """`getUpdates` against a running poller is the 409 that repair may be
    trying to diagnose — self-inflicted, and indistinguishable from the real
    one."""
    repair = BODY[BODY.index("## Repair"):BODY.index("## Upgrade")]
    assert "getUpdates" in repair, "repair never mentions the 409 hazard"
    assert re.search(r"do not call `getUpdates`", repair, re.I), (
        "repair does not forbid polling a live bot")


# --- 5. opt-in stays opt-in ----------------------------------------------------

def test_the_allowlist_step_defaults_to_off():
    step = next(BODY[o:] for n, (h, o) in steps().items() if "allowlist" in h.lower())
    step = step[:step.index("\n### ")] if "\n### " in step else step
    assert re.search(r"one by one|one at a time|per item", step, re.I)
    assert re.search(r"unless the user says|takes? silence as no|default off",
                     step, re.I), "nothing states that an unanswered item is not added"


def test_mcp_servers_are_offered_the_same_way_and_not_gated_by_the_allowlist():
    assert re.search(r"not gated by this file|are not gated", BODY), (
        "the flow implies MCP tools are covered by the permission file; they "
        "are not, and a server left out is invisible to the turn")


# --- 6. teardown tells the truth about the token -------------------------------

def test_uninstall_says_the_token_is_not_revoked():
    section = BODY[BODY.index("## Uninstall"):]
    assert "BotFather" in section, (
        "deleting the token file does not revoke the token, and a teardown that "
        "implies otherwise leaves a live bot the user believes is gone")
    assert "--purge-state" in section and "--purge-secrets" in section


def test_uninstall_names_what_survives_every_flag():
    section = BODY[BODY.index("## Uninstall"):].lower()
    for kept in ("workdir", "venv", "handoff"):
        assert kept in section, f"the teardown never says the {kept} survives"
