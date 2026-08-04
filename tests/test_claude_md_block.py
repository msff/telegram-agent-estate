"""The marker editor, tested on bytes.

This is the one script in the flow that writes to a file the user already owned.
Everything else onboarding touches it also created — the instance directory, the
venv, the plists — and can therefore delete and rebuild. A `CLAUDE.md` may be
years of someone's accumulated instructions, and there is no undo.

So the assertions here are deliberately literal:

  1. **Byte-identical outside the markers.** Compared as `bytes`, prefix and
     suffix, not as normalized text. A test that strips whitespace before
     comparing would pass while the script quietly rewrote every line ending in
     the file.

  2. **Refusal beats a guess.** Every malformed marker shape has two plausible
     repairs and the wrong one eats prose, so each shape gets its own case, and
     each asserts the file is untouched afterwards — a refusal that already
     wrote half the change is not a refusal.

  3. **A block's own content is never a marker.** The shipped templates carry
     their own outer BEGIN/END (that is how `test_templates.py` recognises
     them), so unwrapping is a feature; anything else marker-shaped in the
     content would be indistinguishable from a real marker on the next run and
     is refused before the first byte is written.

The hazard tests at the end are the ones that come from the file not being ours:
a symlinked CLAUDE.md, a read-only directory, a concurrent save, an interrupted
write. Each pins a decision recorded in the script's own docstring.
"""
import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PLUGIN_ROOT / "libexec" / "claude-md-block.py"
TEMPLATES = PLUGIN_ROOT / "templates"
OPS_TEMPLATE = TEMPLATES / "CLAUDE-ops-section.md"
BRIEF_TEMPLATE = TEMPLATES / "CLAUDE-agent-brief.md"


def _load_module():
    """Imported by path: the filename has a hyphen, so it is not a module name.

    Same approach as `test_inspect_project.py` — everything in `libexec/` is
    executed rather than imported, and the CLI is exercised as a subprocess
    below; this handle is for the unit-level hazards a CLI cannot reach.
    """
    spec = importlib.util.spec_from_file_location("claude_md_block", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cmb = _load_module()

BEGIN_OPS = b"<!-- BEGIN telegram-agent-estate:ops -->"
END_OPS = b"<!-- END telegram-agent-estate:ops -->"
BEGIN_BRIEF = b"<!-- BEGIN telegram-agent-estate:brief -->"
END_BRIEF = b"<!-- END telegram-agent-estate:brief -->"

# The two trailing spaces are load-bearing: in markdown that is a hard line
# break, so a "tidy the file while we are in there" rstrip would silently change
# how someone's CLAUDE.md renders. A fixture with no whitespace to lose cannot
# catch that.
PROSE = (
    "# Widget Coach\n"
    "\n"
    "Watches the widget line and complains when it drifts.  \n"
    "It is not a dashboard.\n"
    "\n"
    "## House rules\n"
    "\n"
    "Never round a measurement.\tAsk before rebuilding.\n"
)

OPS_BODY = "## How you are running\n\nRun the send command to reply.\n"
OPS_BODY_V2 = "## How you are running\n\nRun the send command to reply.\n\nTurns are processes.\n"
BRIEF_BODY = "## What this project is\n\nA widget line watcher.\n"


# --- helpers ------------------------------------------------------------------


def content(tmp_path, name, text):
    path = tmp_path / ("content-%s.md" % name)
    path.write_text(text, encoding="utf-8")
    return path


def run(*argv, **kwargs):
    cmd = [sys.executable, str(SCRIPT)] + [str(a) for a in argv]
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def ok(*argv, **kwargs):
    """Run, assert it succeeded, return the parsed report."""
    result = run(*argv, **kwargs)
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    return json.loads(result.stdout)


def refused(*argv, **kwargs):
    """Run, assert it refused cleanly, return the error object."""
    result = run(*argv, **kwargs)
    assert result.returncode == 2, result.stdout
    assert "Traceback" not in result.stderr, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert result.stderr.strip() == payload["error"]["message"].strip()
    return payload["error"]


@pytest.fixture
def project(tmp_path):
    """A project whose CLAUDE.md predates us and has prose we must not touch."""
    root = tmp_path / "widget-coach"
    root.mkdir()
    (root / "CLAUDE.md").write_text(PROSE, encoding="utf-8")
    return root


@pytest.fixture
def ops(tmp_path):
    return content(tmp_path, "ops", OPS_BODY)


@pytest.fixture
def brief(tmp_path):
    return content(tmp_path, "brief", BRIEF_BODY)


# --- insertion ----------------------------------------------------------------


def test_insertion_into_a_file_with_no_markers_appends_one_block(project, ops):
    path = project / "CLAUDE.md"
    before = path.read_bytes()

    out = ok("set", path, "ops=%s" % ops)
    after = path.read_bytes()

    # 10 BEGIN / 11 blank / 12-14 body / 15 blank / 16 END — the block pads a
    # blank line either side of the body so the marker comments do not fuse with
    # the surrounding prose when the file is rendered as markdown.
    assert out["blocks"] == [{"name": "ops", "action": "inserted",
                              "begin_line": 10, "end_line": 16}]
    assert out["created"] is False and out["changed"] is True
    assert out["written"] is True
    assert after.count(BEGIN_OPS) == 1 and after.count(END_OPS) == 1
    assert after.startswith(before), (
        "an append may only add bytes; the original file must survive as an "
        "exact prefix")
    assert b"Run the send command to reply." in after


def test_the_agent_brief_appends_a_second_block_independently(project, ops, brief):
    """R11's separation, at its simplest: two blocks, two marker pairs, and the
    second insertion does not disturb the first."""
    path = project / "CLAUDE.md"
    ok("set", path, "ops=%s" % ops)
    with_ops = path.read_bytes()

    out = ok("set", path, "brief=%s" % brief)
    after = path.read_bytes()

    assert [b["action"] for b in out["blocks"]] == ["inserted"]
    assert after.startswith(with_ops)
    for marker in (BEGIN_OPS, END_OPS, BEGIN_BRIEF, END_BRIEF):
        assert after.count(marker) == 1
    assert after.index(END_OPS) < after.index(BEGIN_BRIEF)


def test_both_blocks_can_land_in_one_call(project, ops, brief):
    """The flow writes both or neither: a CLAUDE.md carrying the ops block and
    no brief is the exact failure this unit exists to prevent — a bot that can
    send messages and knows nothing about what it is attached to."""
    path = project / "CLAUDE.md"
    out = ok("set", path, "ops=%s" % ops, "brief=%s" % brief)

    assert [(b["name"], b["action"]) for b in out["blocks"]] == [
        ("ops", "inserted"), ("brief", "inserted")]
    after = path.read_bytes()
    assert after.count(BEGIN_OPS) == 1 and after.count(BEGIN_BRIEF) == 1


def test_a_block_is_separated_from_the_prose_it_follows(project, ops):
    """Cosmetic, but it is the difference between a readable CLAUDE.md and one
    where the marker is glued to the last sentence someone wrote."""
    path = project / "CLAUDE.md"
    ok("set", path, "ops=%s" % ops)
    text = path.read_text(encoding="utf-8")

    begin = text.index(BEGIN_OPS.decode())
    assert text[:begin].endswith("\n\n")


def test_a_file_without_a_trailing_newline_is_extended_not_rewritten(tmp_path, ops):
    path = tmp_path / "CLAUDE.md"
    path.write_bytes(b"# Widget Coach\n\nno trailing newline here")

    ok("set", path, "ops=%s" % ops)

    assert path.read_bytes().startswith(b"# Widget Coach\n\nno trailing newline here\n")


# --- replacement and idempotency ---------------------------------------------


def test_re_running_replaces_the_block_and_never_duplicates_it(project, tmp_path, ops):
    path = project / "CLAUDE.md"
    ok("set", path, "ops=%s" % ops)
    second = content(tmp_path, "ops-v2", OPS_BODY_V2)

    out = ok("set", path, "ops=%s" % second)
    after = path.read_bytes()

    assert [b["action"] for b in out["blocks"]] == ["replaced"]
    assert after.count(BEGIN_OPS) == 1 and after.count(END_OPS) == 1
    assert b"Turns are processes." in after


def test_replacement_leaves_the_surrounding_bytes_identical(project, tmp_path, ops):
    """THE assertion of this unit. Prose before the block and prose after it,
    compared as raw bytes on both sides of the markers."""
    path = project / "CLAUDE.md"
    ok("set", path, "ops=%s" % ops)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n## Written after the install\n\nStill mine.\n")

    before = path.read_bytes()
    head = before.index(BEGIN_OPS)
    tail = before.index(END_OPS) + len(END_OPS)

    ok("set", path, "ops=%s" % content(tmp_path, "ops-v2", OPS_BODY_V2))
    after = path.read_bytes()

    assert after[:head] == before[:head], "content before the block changed"
    assert after[after.index(END_OPS) + len(END_OPS):] == before[tail:], (
        "content after the block changed")


def test_refreshing_the_ops_block_does_not_touch_a_rewritten_brief(
        project, tmp_path, ops, brief):
    """The reason the two blocks have separate markers. A user rewrites the
    brief; a plugin upgrade re-inserts the ops block; the rewrite survives."""
    path = project / "CLAUDE.md"
    ok("set", path, "ops=%s" % ops, "brief=%s" % brief)

    text = path.read_text(encoding="utf-8")
    text = text.replace("A widget line watcher.", "MY OWN WORDS, hand-written.")
    path.write_text(text, encoding="utf-8")
    before = path.read_bytes()
    brief_at = before.index(BEGIN_BRIEF)

    ok("set", path, "ops=%s" % content(tmp_path, "ops-v2", OPS_BODY_V2))
    after = path.read_bytes()

    assert b"MY OWN WORDS, hand-written." in after
    assert after[after.index(BEGIN_BRIEF):] == before[brief_at:]


def test_an_identical_re_run_writes_nothing_at_all(project, ops):
    """Idempotent down to the inode: a repair that rewrites an unchanged file
    churns mtimes in a synced folder and makes `git status` lie."""
    path = project / "CLAUDE.md"
    ok("set", path, "ops=%s" % ops)
    stat_before = os.stat(path)

    out = ok("set", path, "ops=%s" % ops)

    assert [b["action"] for b in out["blocks"]] == ["unchanged"]
    assert out["changed"] is False and out["written"] is False
    stat_after = os.stat(path)
    assert (stat_after.st_mtime_ns, stat_after.st_ino) == (
        stat_before.st_mtime_ns, stat_before.st_ino)


def test_dry_run_reports_the_change_and_writes_none_of_it(project, ops):
    path = project / "CLAUDE.md"
    before = path.read_bytes()

    out = ok("set", path, "ops=%s" % ops, "--dry-run")

    assert out["changed"] is True and out["written"] is False
    assert out["blocks"][0]["action"] == "inserted"
    assert path.read_bytes() == before


# --- malformed markers refuse rather than guess -------------------------------


MALFORMED = {
    "begin-with-no-end":
        PROSE + "\n<!-- BEGIN telegram-agent-estate:ops -->\nstuff\n",
    "end-before-begin":
        PROSE + "\n<!-- END telegram-agent-estate:ops -->\nstuff\n",
    "the-same-block-twice":
        PROSE + "\n<!-- BEGIN telegram-agent-estate:ops -->\na\n"
                "<!-- END telegram-agent-estate:ops -->\n\n"
                "<!-- BEGIN telegram-agent-estate:ops -->\nb\n"
                "<!-- END telegram-agent-estate:ops -->\n",
    "nested":
        PROSE + "\n<!-- BEGIN telegram-agent-estate:ops -->\n"
                "<!-- BEGIN telegram-agent-estate:brief -->\nb\n"
                "<!-- END telegram-agent-estate:brief -->\n"
                "<!-- END telegram-agent-estate:ops -->\n",
    "interleaved":
        PROSE + "\n<!-- BEGIN telegram-agent-estate:ops -->\na\n"
                "<!-- BEGIN telegram-agent-estate:brief -->\nb\n"
                "<!-- END telegram-agent-estate:ops -->\n"
                "<!-- END telegram-agent-estate:brief -->\n",
    "mismatched-name":
        PROSE + "\n<!-- BEGIN telegram-agent-estate:ops -->\na\n"
                "<!-- END telegram-agent-estate:brief -->\n",
}


@pytest.mark.parametrize("shape", sorted(MALFORMED))
def test_malformed_markers_are_refused_and_nothing_is_written(
        tmp_path, ops, shape):
    path = tmp_path / "CLAUDE.md"
    path.write_text(MALFORMED[shape], encoding="utf-8")
    before = path.read_bytes()

    error = refused("set", path, "ops=%s" % ops)

    assert error["code"] == "malformed-markers"
    assert error["line"] >= 1
    assert path.read_bytes() == before, "a refusal that wrote is not a refusal"


@pytest.mark.parametrize("shape", sorted(MALFORMED))
def test_removal_refuses_on_the_same_shapes(tmp_path, shape):
    """The uninstaller runs on a file that has had a year to go wrong, and it
    runs unattended. Deleting the wrong span there is worse, not better."""
    path = tmp_path / "CLAUDE.md"
    path.write_text(MALFORMED[shape], encoding="utf-8")
    before = path.read_bytes()

    assert refused("remove", path, "ops")["code"] == "malformed-markers"
    assert path.read_bytes() == before


def test_a_malformed_block_refuses_even_when_a_different_block_is_the_target(
        tmp_path, ops):
    """Whole-file validation, not per-block. An unterminated brief means the
    file's structure is not understood, and appending an ops block into it would
    land inside the brief's open span."""
    path = tmp_path / "CLAUDE.md"
    path.write_text(PROSE + "\n<!-- BEGIN telegram-agent-estate:brief -->\nx\n",
                    encoding="utf-8")

    error = refused("set", path, "ops=%s" % ops)
    assert error["code"] == "malformed-markers"
    assert "brief" in error["message"]


def test_the_refusal_says_what_to_do_about_it(tmp_path, ops):
    """The reader is a human being told they cannot have what they asked for.
    A code and a line number are not enough to act on."""
    path = tmp_path / "CLAUDE.md"
    path.write_text(MALFORMED["begin-with-no-end"], encoding="utf-8")

    message = refused("set", path, "ops=%s" % ops)["message"]

    assert str(path) in message
    assert "END" in message and "re-run" in message


def test_status_refuses_on_a_malformed_file_too(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_text(MALFORMED["nested"], encoding="utf-8")

    assert refused("status", path)["code"] == "malformed-markers"


# --- block content is never parsed as markers ---------------------------------


def test_content_carrying_a_marker_line_is_refused_before_anything_is_written(
        project, tmp_path):
    """Written into the file, that line becomes a marker — the file would parse
    as a duplicate or a nest on the very next run."""
    path = project / "CLAUDE.md"
    before = path.read_bytes()
    bad = content(tmp_path, "bad",
                  "## Ops\n\n<!-- BEGIN telegram-agent-estate:brief -->\nx\n")

    error = refused("set", path, "ops=%s" % bad)

    assert error["code"] == "content-markers"
    assert path.read_bytes() == before


def test_a_block_may_talk_about_markers_without_being_one(project, tmp_path, ops):
    """The ops block's whole job is explaining the machinery, so prose naming
    the markers has to be allowed. Only a line that IS a marker is not."""
    path = project / "CLAUDE.md"
    body = ("## How you are running\n\n"
            "Keep the BEGIN telegram-agent-estate:ops marker where it is.\n"
            "An indented `<!-- BEGIN telegram-agent-estate:ops -->` inside a\n"
            "sentence is prose, not a delimiter.\n")
    ok("set", path, "ops=%s" % content(tmp_path, "talky", body))

    after = path.read_bytes()
    assert after.count(BEGIN_OPS) == 2, "the quoted marker is inside the block"
    # ...and the file still round-trips: the quoted one is inside the block, so
    # a re-insertion replaces it rather than tripping over it.
    out = ok("set", path, "ops=%s" % ops)
    assert [b["action"] for b in out["blocks"]] == ["replaced"]
    assert path.read_bytes().count(BEGIN_OPS) == 1


def test_a_template_s_own_wrapper_is_unwrapped_rather_than_nested(project):
    """The shipped templates carry their own BEGIN/END — `test_templates.py`
    pins that — so a caller handing one over must not end up with two pairs."""
    path = project / "CLAUDE.md"
    ok("set", path, "ops=%s" % OPS_TEMPLATE)

    after = path.read_bytes()
    assert after.count(BEGIN_OPS) == 1 and after.count(END_OPS) == 1
    assert b"## How you are running" in after
    assert b"Paste this section into the instance" not in after, (
        "the template's instructional comment is guidance for the installer, "
        "not text for the target file")


def test_the_wrong_template_under_the_wrong_name_is_refused(project):
    """`brief=<ops template>` would otherwise bury one block's markers inside
    the other's."""
    path = project / "CLAUDE.md"
    error = refused("set", path, "brief=%s" % OPS_TEMPLATE)

    assert error["code"] == "content-markers"
    assert "ops" in error["message"]


def test_empty_content_is_refused(project, tmp_path):
    path = project / "CLAUDE.md"
    error = refused("set", path, "ops=%s" % content(tmp_path, "empty", "\n\n"))
    assert error["code"] == "empty-content"


def test_unsubstituted_placeholders_are_reported_not_silently_installed(project):
    """A CLAUDE.md telling the agent to run `[ABSOLUTE_PYTHON_PATH] send.py` is
    a bot that cannot reply. Refusing would be wrong — the caller may be staging
    — but the flow has to be told."""
    path = project / "CLAUDE.md"
    out = ok("set", path, "ops=%s" % OPS_TEMPLATE)

    warned = " ".join(out["warnings"])
    assert "[ABSOLUTE_PYTHON_PATH]" in warned
    assert "[ESTATE_LIBEXEC]" in warned


# --- a missing CLAUDE.md ------------------------------------------------------


def test_a_missing_claude_md_is_created_carrying_both_blocks(tmp_path, ops, brief):
    """An agent with no instructions is not a bot."""
    root = tmp_path / "widget-coach"
    root.mkdir()
    path = root / "CLAUDE.md"

    out = ok("set", path, "ops=%s" % ops, "brief=%s" % brief)

    assert out["created"] is True and out["written"] is True
    after = path.read_bytes()
    assert after.startswith(b"# widget-coach\n")
    for marker in (BEGIN_OPS, END_OPS, BEGIN_BRIEF, END_BRIEF):
        assert after.count(marker) == 1
    assert b"outside the marker blocks below is yours" in after
    assert any("did not exist" in w for w in out["warnings"])


def test_the_created_file_takes_a_title_and_a_readable_mode(tmp_path, ops):
    root = tmp_path / "widget-coach"
    root.mkdir()
    path = root / "CLAUDE.md"

    ok("set", path, "ops=%s" % ops, "--title", "Widget Coach")

    assert path.read_text(encoding="utf-8").startswith("# Widget Coach\n")
    assert os.stat(path).st_mode & 0o077 != 0o077  # not the 0600 mkstemp default
    assert os.stat(path).st_mode & 0o400


def test_no_create_refuses_instead_of_writing_a_new_file(tmp_path, ops):
    path = tmp_path / "CLAUDE.md"

    error = refused("set", path, "ops=%s" % ops, "--no-create")

    assert error["code"] == "missing-file"
    assert not path.exists()


# --- removal ------------------------------------------------------------------


def test_removal_takes_out_one_block_and_leaves_the_other(project, ops, brief):
    path = project / "CLAUDE.md"
    ok("set", path, "ops=%s" % ops, "brief=%s" % brief)
    before = path.read_bytes()
    head = before.index(BEGIN_OPS)
    tail = before.index(END_OPS) + len(END_OPS)

    out = ok("remove", path, "ops")
    after = path.read_bytes()

    assert [(b["name"], b["action"]) for b in out["blocks"]] == [("ops", "removed")]
    assert BEGIN_OPS not in after and BEGIN_BRIEF in after
    assert after == before[:head] + before[tail + 1:], (
        "removal must take the block's own lines and nothing else")
    assert PROSE.encode("utf-8") in after


def test_removing_both_blocks_leaves_the_users_prose(project, ops, brief):
    path = project / "CLAUDE.md"
    ok("set", path, "ops=%s" % ops, "brief=%s" % brief)

    ok("remove", path, "ops", "brief")

    after = path.read_text(encoding="utf-8")
    assert "telegram-agent-estate" not in after
    assert after.startswith(PROSE)


def test_insert_then_remove_returns_the_file_to_its_bytes_plus_the_separator(
        project, ops):
    """The documented shape of "uninstall returns the machine to its prior
    state": exactly the blank line the insertion added is left behind.

    Eating it would mean deleting a line outside the markers on a guess about
    who put it there, which is the one thing this script must never do.
    """
    path = project / "CLAUDE.md"
    before = path.read_bytes()
    ok("set", path, "ops=%s" % ops)

    ok("remove", path, "ops")

    assert path.read_bytes() == before + b"\n"


def test_removing_a_block_that_is_not_there_is_a_no_op(project, ops):
    path = project / "CLAUDE.md"
    ok("set", path, "ops=%s" % ops)
    before = path.read_bytes()

    out = ok("remove", path, "brief")

    assert [(b["name"], b["action"]) for b in out["blocks"]] == [("brief", "absent")]
    assert out["changed"] is False and out["written"] is False
    assert path.read_bytes() == before


def test_removal_from_a_missing_file_needs_saying_so_out_loud(tmp_path):
    path = tmp_path / "CLAUDE.md"

    assert refused("remove", path, "ops")["code"] == "missing-file"

    out = ok("remove", path, "ops", "--missing-ok")
    assert out["blocks"][0]["action"] == "absent"
    assert out["changed"] is False


# --- encodings, line endings, and other things about a file we did not write ---


def test_a_crlf_file_stays_crlf_throughout(tmp_path, ops):
    path = tmp_path / "CLAUDE.md"
    path.write_bytes(PROSE.replace("\n", "\r\n").encode("utf-8"))

    out = ok("set", path, "ops=%s" % ops)
    after = path.read_bytes()

    assert out["line_ending"] == "crlf"
    assert BEGIN_OPS + b"\r\n" in after
    assert b"\n" not in after.replace(b"\r\n", b""), (
        "a lone LF anywhere means the file is now mixed")


def test_a_bom_and_non_ascii_prose_survive_byte_for_byte(tmp_path, ops):
    path = tmp_path / "CLAUDE.md"
    original = "﻿# Wörterbuch\n\nEmoji: 🚴 — and an em dash.\n".encode("utf-8")
    path.write_bytes(original)

    ok("set", path, "ops=%s" % ops)
    after = path.read_bytes()

    assert after.startswith(original)
    assert after.startswith(b"\xef\xbb\xbf")


def test_a_file_that_is_not_utf8_is_refused_rather_than_mangled(tmp_path, ops):
    path = tmp_path / "CLAUDE.md"
    original = b"# Widget Coach\n\nlatin-1 caf\xe9\n"
    path.write_bytes(original)

    error = refused("set", path, "ops=%s" % ops)

    assert error["code"] == "not-utf8"
    assert path.read_bytes() == original


def test_a_symlinked_claude_md_keeps_being_a_symlink(tmp_path, ops):
    """Repos point CLAUDE.md at a shared doc all the time. An atomic replace on
    the link path would turn the link into a regular file and orphan the
    target."""
    shared = tmp_path / "shared" / "instructions.md"
    shared.parent.mkdir()
    shared.write_text(PROSE, encoding="utf-8")
    root = tmp_path / "widget-coach"
    root.mkdir()
    link = root / "CLAUDE.md"
    link.symlink_to(shared)

    out = ok("set", link, "ops=%s" % ops)

    assert link.is_symlink()
    assert out["resolved_path"] == str(shared)
    assert BEGIN_OPS in shared.read_bytes()


def test_a_dangling_symlink_is_refused(tmp_path, ops):
    link = tmp_path / "CLAUDE.md"
    link.symlink_to(tmp_path / "gone.md")

    error = refused("set", link, "ops=%s" % ops)

    assert error["code"] == "broken-symlink"
    assert not (tmp_path / "gone.md").exists()


def test_a_directory_is_not_a_claude_md(tmp_path, ops):
    path = tmp_path / "CLAUDE.md"
    path.mkdir()

    assert refused("set", path, "ops=%s" % ops)["code"] == "not-a-file"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits")
def test_a_read_only_directory_is_refused_and_says_why(tmp_path, ops):
    """An atomic write needs a sibling temp file, so a writable CLAUDE.md in a
    read-only directory cannot be edited this way — which is a surprising thing
    to be told unless the message explains it."""
    root = tmp_path / "widget-coach"
    root.mkdir()
    path = root / "CLAUDE.md"
    path.write_text(PROSE, encoding="utf-8")
    os.chmod(root, 0o500)
    try:
        error = refused("set", path, "ops=%s" % ops)
    finally:
        os.chmod(root, 0o700)

    assert error["code"] == "not-writable"
    assert str(root) in error["message"]
    assert path.read_text(encoding="utf-8") == PROSE


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits")
def test_an_unwritable_file_is_refused(tmp_path, ops):
    path = tmp_path / "CLAUDE.md"
    path.write_text(PROSE, encoding="utf-8")
    os.chmod(path, 0o444)

    error = refused("set", path, "ops=%s" % ops)

    assert error["code"] == "not-writable"
    assert path.read_text(encoding="utf-8") == PROSE


def test_a_concurrent_save_is_detected_instead_of_clobbered(tmp_path):
    """The window between reading the file and replacing it is a conversational
    flow — seconds, sometimes minutes. An editor saving in that window would
    otherwise be silently overwritten by the replace."""
    path = tmp_path / "CLAUDE.md"
    path.write_text(PROSE, encoding="utf-8")
    text, stat = cmb.read_file(str(path))

    path.write_text(text + "saved by someone else while we were thinking\n",
                    encoding="utf-8")

    with pytest.raises(cmb.BlockError) as caught:
        cmb.atomic_write(str(path), "whatever we computed", 0o644, stat)

    assert caught.value.code == "file-changed"
    assert "saved by someone else" in path.read_text(encoding="utf-8")


def test_an_interrupted_write_leaves_the_original_and_no_debris(
        tmp_path, monkeypatch):
    """`os.replace` is the last step for exactly this reason: everything before
    it happens in a temp file, so a crash cannot produce a half-written
    CLAUDE.md."""
    path = tmp_path / "CLAUDE.md"
    path.write_text(PROSE, encoding="utf-8")
    _, stat = cmb.read_file(str(path))

    def boom(src, dst):
        raise KeyboardInterrupt("power cut")

    monkeypatch.setattr(cmb.os, "replace", boom)
    with pytest.raises(KeyboardInterrupt):
        cmb.atomic_write(str(path), "the new content", 0o644, stat)

    assert path.read_text(encoding="utf-8") == PROSE
    assert not list(tmp_path.glob(".claude-md-block.*")), "temp file left behind"


def test_a_refusal_leaves_no_temp_file_next_to_the_target(tmp_path, ops):
    path = tmp_path / "CLAUDE.md"
    path.write_text(MALFORMED["nested"], encoding="utf-8")

    refused("set", path, "ops=%s" % ops)

    assert not list(tmp_path.glob(".claude-md-block.*")), "temp file left behind"
    assert path.read_text(encoding="utf-8") == MALFORMED["nested"]


# --- the CLI's own edges ------------------------------------------------------


def test_status_reports_the_blocks_and_where_they_are(project, ops, brief):
    path = project / "CLAUDE.md"
    ok("set", path, "ops=%s" % ops, "brief=%s" % brief)

    out = ok("status", path)

    assert out["exists"] is True
    assert [b["name"] for b in out["blocks"]] == ["ops", "brief"], (
        "document order, not alphabetical — the caller is reading the file")
    for block in out["blocks"]:
        assert block["begin_line"] < block["end_line"]


def test_status_on_a_missing_file_answers_the_question(tmp_path):
    out = ok("status", tmp_path / "CLAUDE.md")
    assert out["exists"] is False and out["blocks"] == []


def test_the_body_can_come_from_stdin(project):
    path = project / "CLAUDE.md"
    ok("set", path, "ops=-", input="## Ops\n\nfrom a pipe\n")
    assert b"from a pipe" in path.read_bytes()


@pytest.mark.parametrize("spec,code", [
    ("ops", "bad-spec"),                       # no content file
    ("ops=", "bad-spec"),                      # empty content file
    ("Ops=/dev/null", "bad-name"),             # upper case
    ("../evil=/dev/null", "bad-name"),         # path-shaped
])
def test_a_junk_block_spec_is_refused(project, spec, code):
    assert refused("set", project / "CLAUDE.md", spec)["code"] == code


def test_naming_the_same_block_twice_in_one_call_is_refused(project, ops):
    error = refused("set", project / "CLAUDE.md",
                    "ops=%s" % ops, "ops=%s" % ops)
    assert error["code"] == "bad-spec"


def test_a_missing_content_file_is_refused(project, tmp_path):
    error = refused("set", project / "CLAUDE.md", "ops=%s" % (tmp_path / "gone.md"))
    assert error["code"] == "unreadable"


@pytest.mark.parametrize("where", ["before", "after"])
def test_the_report_is_json_on_stdout_and_compact_works_on_either_side(
        project, ops, where):
    """A flag argparse accepts only before the subcommand answers a hand-written
    command line with `unrecognized arguments`, which reads as a missing
    feature."""
    argv = ["set", project / "CLAUDE.md", "ops=%s" % ops]
    argv = ["--compact"] + argv if where == "before" else argv + ["--compact"]
    result = run(*argv)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "\n" not in result.stdout.strip()
    assert json.loads(result.stdout)["schema"] == cmb.SCHEMA


# --- the interpreter constraint ----------------------------------------------


def _interpreters():
    out = [pytest.param(sys.executable, id="suite-interpreter")]
    if os.path.exists("/usr/bin/python3"):
        out.append(pytest.param("/usr/bin/python3", id="system-python"))
    return out


@pytest.mark.parametrize("interpreter", _interpreters())
def test_it_runs_with_no_third_party_packages_available(
        interpreter, tmp_path, project, ops):
    """The uninstaller calls this after the venv may already be gone, and the
    skill calls it on a machine that has just met this plugin. Same constraint
    as `inspect-project.py`, asserted the same way: a subprocess with `site`
    disabled and a PATH that has no Homebrew in it.
    """
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")}
    path = project / "CLAUDE.md"
    result = subprocess.run(
        [interpreter, "-I", "-S", str(SCRIPT), "set", str(path),
         "ops=%s" % ops],
        capture_output=True, text=True, env=env)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout)["ok"] is True
    assert BEGIN_OPS in path.read_bytes()


@pytest.mark.skipif(not hasattr(sys, "stdlib_module_names"),
                    reason="needs python 3.10+ to know what the stdlib is")
def test_every_import_in_the_module_is_stdlib():
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
        "claude-md-block.py may import stdlib only; it imports %s" % third_party)


# --- the two templates it is the editor for -----------------------------------


def test_the_agent_brief_template_has_its_own_markers(project):
    """Separate from the ops block's, or a refresh of one would fight the
    other."""
    text = BRIEF_TEMPLATE.read_text(encoding="utf-8")
    begin, end = BEGIN_BRIEF.decode(), END_BRIEF.decode()

    assert text.count(begin) == 1 and text.count(end) == 1
    assert text.index(begin) < text.index(end)
    assert "telegram-agent-estate:ops" not in text


def test_the_agent_brief_template_says_what_the_bot_is_for(project):
    """The ops block alone produces a bot that can send messages and knows
    nothing about what it is attached to. This template is the other half, so
    the questions it leaves for the flow to fill in are its contract."""
    text = BRIEF_TEMPLATE.read_text(encoding="utf-8")
    body = text[text.index(BEGIN_BRIEF.decode()):]

    for placeholder in ("[WHAT_IT_IS]", "[WHAT_YOU_ARE_FOR]", "[LANGUAGE]",
                        "[VOICE]", "[FACT_SOURCES]", "[OUT_OF_SCOPE]"):
        assert placeholder in body, "%s is not asked for" % placeholder


def test_both_shipped_templates_install_into_a_fresh_claude_md(tmp_path):
    """End to end on the real templates: the file this produces is what a
    stranger's project ends up with."""
    root = tmp_path / "widget-coach"
    root.mkdir()
    path = root / "CLAUDE.md"

    out = ok("set", path, "ops=%s" % OPS_TEMPLATE, "brief=%s" % BRIEF_TEMPLATE)

    assert out["created"] is True
    assert [b["action"] for b in out["blocks"]] == ["inserted", "inserted"]
    after = path.read_bytes()
    for marker in (BEGIN_OPS, END_OPS, BEGIN_BRIEF, END_BRIEF):
        assert after.count(marker) == 1
    assert b"## How you are running" in after
    assert b"## What you are for" in after

    # ...and the result is a file this script can still parse, which is what
    # makes the second run a repair rather than a refusal.
    status = ok("status", path)
    assert [b["name"] for b in status["blocks"]] == ["ops", "brief"]
    assert ok("set", path, "ops=%s" % OPS_TEMPLATE)["blocks"][0]["action"] == \
        "unchanged"
