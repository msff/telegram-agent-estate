#!/usr/bin/env python3
"""Own the read-modify-write of a target project's `CLAUDE.md` (R11).

Onboarding writes two marker-delimited blocks into a file it did not create,
does not own, and cannot afford to damage:

  * the **ops block** (`telegram-agent-estate:ops`) — how the runner reaches the
    user, what a turn owes, what failure means. `templates/CLAUDE-ops-section.md`
    is its source, and an upgrade refreshes it.
  * the **agent brief** (`telegram-agent-estate:brief`) — what this project is
    and what the bot is for, in what voice. `templates/CLAUDE-agent-brief.md` is
    its source, and it is prose the user is expected to rewrite.

Two names, two independent marker pairs: refreshing the ops block on upgrade
must not touch a brief the user has since rewritten, and rewriting the brief
must not fight the ops block.

    libexec/claude-md-block.py set    <file> <name>=<content> [<name>=<content>...]
    libexec/claude-md-block.py remove <file> <name> [<name> ...]
    libexec/claude-md-block.py status <file>

`<content>` is a file to read the block body from, or `-` for stdin. Exit 0 with
a JSON report on stdout; exit 2 with `{"ok": false, "error": {...}}` on stdout
and a one-line human message on stderr. A traceback is a bug: the caller is a
chat flow and an uninstaller, not a shell.

WHY THIS IS A SCRIPT AND NOT INSTRUCTIONS IN A SKILL
----------------------------------------------------
The guarantees below are byte-level, and prose in a `SKILL.md` cannot be tested.
An agent following instructions with the Edit tool makes "leaves surrounding
content byte-identical" a hope; here it is an assertion in
`tests/test_claude_md_block.py`, against the one file in this whole flow that
belongs to the user and predates us.

  1. **Re-running repairs, never duplicates.** A block that exists is replaced
     between its own markers; a block that does not is appended once.
  2. **Everything outside the markers is byte-identical afterwards.** The file
     is spliced as a list of lines with their endings preserved, so the prefix
     and suffix are the original bytes, not a re-rendered version of them. An
     append leaves the original bytes as an exact prefix of the result.
  3. **Malformed markers refuse rather than guess** — a BEGIN with no END, an
     END before its BEGIN, a second copy of the same block, a nested or
     interleaved pair. Every one of them has two plausible repairs, and the
     wrong one silently eats the user's prose.
  4. **Block content is never parsed as markers.** Content carrying a
     marker-shaped line is refused before anything is written, because once
     inside the file that line is indistinguishable from a real marker. The one
     exception is the shipped templates' own outer BEGIN/END wrapper, which is
     unwrapped and re-emitted canonically — that also drops the instructional
     comment those templates carry above the marker, which is guidance for the
     installer and not for the target file.

FILE HAZARDS — WHAT IS DEFENDED, AND WHAT IS NOT
------------------------------------------------
Defended:

  * **A partial write.** The new text goes to a temp file in the same directory,
    is flushed and fsynced, then `os.replace`d over the target. An interrupted
    run leaves either the old file or the new one, never half of either.
  * **Encoding.** Read and written as UTF-8, strictly. A file that is not UTF-8
    is refused by name rather than mangled, and a BOM survives untouched because
    it decodes to a character in the preserved prefix.
  * **Line endings.** The block is emitted with the file's own dominant ending,
    so inserting into a CRLF file does not produce a mixed one. Endings outside
    the block are never rewritten.
  * **Symlinks.** A `CLAUDE.md` that is a symlink is resolved and the real file
    is edited, so the link survives — an atomic replace on the link path would
    quietly turn it into a regular file. A dangling link is refused.
  * **Unwritable targets.** Checked before any work, including the *directory*:
    an atomic replace needs to create a sibling temp file, so a writable file in
    a read-only directory cannot be edited this way, and the message says so.
  * **A concurrent edit.** The file's size and mtime are re-checked immediately
    before the replace; if it moved under us the run refuses instead of
    clobbering whatever the other writer just saved. This narrows the window, it
    does not close it — a real lock is impossible against an editor that saves
    by rename, which is how most of them save.

Deliberately not defended:

  * **Hard links.** `os.replace` breaks them: a `CLAUDE.md` hard-linked to
    another path keeps the old content at the other name. Atomicity is worth
    more than the link, and hard-linked markdown is vanishingly rare.
  * **Extended attributes and resource forks.** Lost on replace, for the same
    trade. The POSIX mode is preserved.
  * **A marker-shaped line the user wrote inside a fenced code block.** Telling
    it from a real marker needs a markdown parser; a documentation file quoting
    `<!-- BEGIN telegram-agent-estate:ops -->` therefore reads as malformed
    markers and gets a refusal, which is the safe direction to be wrong in.
  * **Whitespace tidying on removal.** Removal takes out the block's own lines
    and nothing else, so a blank separator line inserted with the block stays
    behind. Eating an adjacent blank line would be touching content outside the
    markers on a guess about who put it there.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile

SCHEMA = "telegram-agent-estate/claude-md-block@1"

# The marker vocabulary. `templates/CLAUDE-ops-section.md` has shipped
# `<!-- BEGIN telegram-agent-estate:ops -->` since U2 and `inspect-project.py`
# reports sightings of it; this module owns the grammar around it.
NAMESPACE = "telegram-agent-estate"
OPS_BLOCK = "ops"
BRIEF_BLOCK = "brief"
KNOWN_BLOCKS = (OPS_BLOCK, BRIEF_BLOCK)

# Any well-formed name is accepted, not just the two above: an uninstaller
# shipped today has to be able to remove a block a later version invents.
BLOCK_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")

MARKER = re.compile(
    r"^[ \t]*<!--[ \t]*(BEGIN|END)[ \t]+" + re.escape(NAMESPACE) +
    r":([^\s>]+)[ \t]*-->[ \t]*$")

# A bracketed SHOUTING word is how both templates mark a substitution the flow
# has to make. Writing one into a CLAUDE.md verbatim produces an instruction the
# agent cannot follow, so it is worth saying out loud — but it is the caller's
# mistake to fix, not this script's to refuse.
PLACEHOLDER = re.compile(r"\[[A-Z][A-Z0-9_]{2,}\]")

EOL = re.compile(r"\r\n|\r|\n")
EOL_NAMES = {"\n": "lf", "\r\n": "crlf", "\r": "cr"}

# Enough to hold any plausible CLAUDE.md and stop an accidental multi-gigabyte
# one (generated logs get committed) from being read into memory whole.
FILE_LIMIT = 8 * 1024 * 1024


class BlockError(Exception):
    """A refusal with a code the caller can branch on and a message a human can
    act on. Every raise site names the file and, where there is one, the line."""

    def __init__(self, code, message, line=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line


# --- markers -----------------------------------------------------------------


def begin_marker(name):
    return "<!-- BEGIN {}:{} -->".format(NAMESPACE, name)


def end_marker(name):
    return "<!-- END {}:{} -->".format(NAMESPACE, name)


def split_lines(text):
    """Split into lines that keep their endings, so `"".join(...) == text`.

    Not `str.splitlines(keepends=True)`: that also splits on form feed, vertical
    tab and U+2028, which are legal characters in prose. Reassembly would still
    be lossless, but a reported line number would stop matching what the user
    sees in an editor, and this module's errors are read by a human who then has
    to go and find the marker.
    """
    lines, pos = [], 0
    for m in EOL.finditer(text):
        lines.append(text[pos:m.end()])
        pos = m.end()
    if pos < len(text):
        lines.append(text[pos:])
    return lines


def dominant_eol(text):
    """The line ending the file mostly uses. LF when it has none to go on."""
    counts = {}
    for m in EOL.finditer(text):
        counts[m.group(0)] = counts.get(m.group(0), 0) + 1
    if not counts:
        return "\n"
    return max(sorted(counts), key=lambda eol: counts[eol])


def parse_blocks(lines, path):
    """`{name: (begin_index, end_index)}` — or a refusal.

    A state machine rather than two independent scans, so the failure modes are
    named instead of averaged. Inside an open block the only marker that means
    anything is that block's own END; any other marker there is a nesting or an
    interleave, and both of them are ways to lose a block by replacing the one
    that appears to contain it.
    """
    blocks = {}
    open_name = None
    open_index = None

    for i, line in enumerate(lines):
        m = MARKER.match(line)
        if not m:
            continue
        kind, name = m.group(1), m.group(2)

        if open_name is None:
            if kind == "END":
                raise BlockError(
                    "malformed-markers",
                    "{}:{}: an END marker for {!r} with no BEGIN before it. "
                    "Refusing to edit: the block has no start, so there is no "
                    "way to tell managed text from yours. Add the BEGIN marker "
                    "or delete this END, then re-run.".format(path, i + 1, name),
                    i + 1)
            if name in blocks:
                first = blocks[name]
                raise BlockError(
                    "malformed-markers",
                    "{}:{}: a second BEGIN marker for {!r}; the first block is "
                    "at lines {}-{}. Refusing to edit: replacing one copy and "
                    "leaving the other would leave the agent two sets of "
                    "instructions. Delete the copy you do not want and re-run."
                    .format(path, i + 1, name, first[0] + 1, first[1] + 1),
                    i + 1)
            open_name, open_index = name, i
        elif kind == "END" and name == open_name:
            blocks[open_name] = (open_index, i)
            open_name = None
        else:
            raise BlockError(
                "malformed-markers",
                "{}:{}: a {} marker for {!r} inside the {!r} block opened at "
                "line {}. Refusing to edit: nested or interleaved markers make "
                "the inner block part of the outer one, so replacing the outer "
                "would delete the inner. Close each block before the next one "
                "begins.".format(path, i + 1, kind, name, open_name,
                                 open_index + 1),
                i + 1)

    if open_name is not None:
        raise BlockError(
            "malformed-markers",
            "{}:{}: the BEGIN marker for {!r} is never closed — no {!r} follows "
            "it. Refusing to edit: the block's extent is a guess, and guessing "
            "long deletes your prose while guessing short duplicates the block. "
            "Add the END marker (or delete the BEGIN) and re-run."
            .format(path, open_index + 1, open_name, end_marker(open_name)),
            open_index + 1)

    return blocks


# --- content -----------------------------------------------------------------


def body_lines(name, raw, source):
    """The block body, as lines, from a content file or from stdin.

    The shipped templates carry their own BEGIN/END pair — that is what makes
    them recognisable as the source of a block, and `test_templates.py` pins it
    — so a wrapper matching THIS block is unwrapped and everything outside it
    (the templates' instructional comment) is dropped. Anything else
    marker-shaped is refused: written into the file it would be a marker, and
    the next run would read it as one.
    """
    lines = split_lines(raw)
    found = [(i, m.group(1), m.group(2))
             for i, m in ((i, MARKER.match(l)) for i, l in enumerate(lines)) if m]

    if found:
        wrapped = (len(found) == 2
                   and found[0][1:] == ("BEGIN", name)
                   and found[1][1:] == ("END", name)
                   and found[0][0] < found[1][0])
        if not wrapped:
            i, kind, other = found[0]
            raise BlockError(
                "content-markers",
                "{}:{}: the {} block's content carries a {} marker for {!r}. "
                "Refusing to write it: inside a CLAUDE.md that line is "
                "indistinguishable from a real marker, and the next run would "
                "read it as one. Content may carry at most its own outer "
                "{} / {} pair.".format(source, i + 1, name, kind, other,
                                       begin_marker(name), end_marker(name)),
                i + 1)
        lines = lines[found[0][0] + 1:found[1][0]]

    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise BlockError(
            "empty-content",
            "{}: the {} block's content is empty. Refusing to write an empty "
            "block: it would look installed and instruct nothing."
            .format(source, name))
    return lines


def render_block(name, body, eol):
    """The block as canonical lines, all terminated with the file's own ending.

    A blank line after BEGIN and before END, always. CommonMark ends an HTML
    block at the line carrying `-->`, so it does not strictly need one — but
    plenty of renderers keep swallowing until a blank line, and a heading eaten
    by the marker above it is a silently ugly CLAUDE.md. It also makes a block
    this script wrote look exactly like the template it came from.
    """
    out = [begin_marker(name) + eol, eol]
    for line in body:
        out.append(EOL.sub("", line) + eol)
    out.extend([eol, end_marker(name) + eol])
    return out


def placeholder_warning(name, body, source):
    left = sorted({m.group(0) for line in body for m in PLACEHOLDER.finditer(line)})
    if not left:
        return None
    return ("the {} block still carries unsubstituted placeholders from {}: {} "
            "— an agent reading them literally cannot act on them"
            .format(name, source, ", ".join(left)))


# --- splicing ----------------------------------------------------------------


def append_block(lines, rendered, eol):
    """Append the block, separated from what is already there by one blank line.

    Only ever adds bytes: the original text stays an exact prefix of the result,
    which is the append half of "nothing outside the markers changes".
    """
    if lines:
        if not EOL.search(lines[-1]):
            lines[-1] = lines[-1] + eol
        if lines[-1].strip():
            lines.append(eol)
    lines.extend(rendered)


def apply_set(lines, specs, eol, path):
    actions = []
    for name, body, source in specs:
        blocks = parse_blocks(lines, path)
        rendered = render_block(name, body, eol)
        if name in blocks:
            begin, end = blocks[name]
            if lines[begin:end + 1] == rendered:
                actions.append((name, "unchanged"))
                continue
            lines[begin:end + 1] = rendered
            actions.append((name, "replaced"))
        else:
            append_block(lines, rendered, eol)
            actions.append((name, "inserted"))
    return actions


def apply_remove(lines, names, path):
    actions = []
    for name in names:
        blocks = parse_blocks(lines, path)
        if name not in blocks:
            actions.append((name, "absent"))
            continue
        begin, end = blocks[name]
        del lines[begin:end + 1]
        actions.append((name, "removed"))
    return actions


# --- the file ----------------------------------------------------------------


def resolve(path):
    """`(path_as_given, the_file_actually_edited, was_a_symlink)`.

    A symlinked `CLAUDE.md` is common — a repo pointing it at a shared doc, or
    at `AGENTS.md`. Replacing the link path atomically would silently turn the
    link into a regular file and orphan whatever it pointed at, so the real file
    is edited and the link is left alone.
    """
    given = os.path.abspath(os.path.expanduser(path))
    if not os.path.islink(given):
        return given, given, False
    real = os.path.realpath(given)
    if not os.path.exists(real):
        raise BlockError(
            "broken-symlink",
            "{} is a symlink to {}, which does not exist. Refusing to edit: "
            "writing would create that file somewhere you did not ask for."
            .format(given, real))
    return given, real, True


def read_file(path):
    """`(text, stat)` for an existing file, or `(None, None)` for a missing one."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise BlockError("unreadable",
                         "cannot stat {}: {}".format(path, exc.strerror))
    if not os.path.isfile(path):
        raise BlockError(
            "not-a-file",
            "{} is not a regular file — it cannot be a CLAUDE.md.".format(path))
    if st.st_size > FILE_LIMIT:
        raise BlockError(
            "too-large",
            "{} is {} bytes, over the {} byte limit. Refusing to rewrite a file "
            "this size in memory.".format(path, st.st_size, FILE_LIMIT))
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise BlockError("unreadable",
                         "cannot read {}: {}".format(path, exc.strerror))
    try:
        return raw.decode("utf-8"), st
    except UnicodeDecodeError as exc:
        raise BlockError(
            "not-utf8",
            "{} is not valid UTF-8 (byte {} is {:#04x}). Refusing to edit: "
            "rewriting it as UTF-8 would corrupt every character this script "
            "cannot read.".format(path, exc.start, raw[exc.start]))


def check_writable(path, exists):
    parent = os.path.dirname(path) or "."
    if exists and not os.access(path, os.W_OK):
        raise BlockError("not-writable",
                         "{} is not writable by this user.".format(path))
    if not os.path.isdir(parent):
        raise BlockError(
            "missing-directory",
            "{} does not exist, so {} cannot be created there."
            .format(parent, os.path.basename(path)))
    if not os.access(parent, os.W_OK | os.X_OK):
        raise BlockError(
            "not-writable",
            "{} is not writable. The edit is written to a temp file in that "
            "directory and moved into place — which is what makes an "
            "interrupted run leave the old file intact — so a writable "
            "CLAUDE.md in a read-only directory is not enough."
            .format(parent))


def default_mode():
    """What `open(path, "w")` would have produced, umask included."""
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def atomic_write(path, text, mode, before):
    """Replace `path` with `text`, or leave it exactly as it was.

    `before` is the stat taken when the file was read; it is re-checked here
    because everything between that read and this write is a window in which an
    editor could have saved. `os.replace` would clobber that save without a
    word.
    """
    if before is None:
        if os.path.lexists(path):
            raise BlockError(
                "file-changed",
                "{} appeared while it was being created. Nothing was written — "
                "re-run, so the new content is merged rather than replaced."
                .format(path))
    else:
        try:
            now = os.stat(path)
        except OSError as exc:
            raise BlockError(
                "file-changed",
                "{} disappeared while it was being edited ({}). Nothing was "
                "written; re-run.".format(path, exc.strerror))
        if (now.st_size, now.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise BlockError(
                "file-changed",
                "{} changed on disk while it was being edited. Nothing was "
                "written — re-run rather than let this overwrite whatever was "
                "just saved.".format(path))

    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".claude-md-block.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Best effort: the rename is durable only once the directory entry is.
    try:
        dfd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


# --- commands ----------------------------------------------------------------


SCAFFOLD = """\
# {title}

Standing instructions for the agent working in this project, re-read at the
start of every turn.

Everything outside the marker blocks below is yours: write what the agent should
know about this project here, and nothing this plugin does will touch it. What
is between a BEGIN and its END marker is managed by `{namespace}`
and is replaced whole whenever the plugin refreshes it.
"""


def parse_spec(spec):
    """`name=path` — split on the FIRST `=`, so a path may contain one."""
    if "=" not in spec:
        raise BlockError(
            "bad-spec",
            "{!r} is not a block spec. Expected <name>=<content-file>, for "
            "example {}=templates/CLAUDE-ops-section.md.".format(spec, OPS_BLOCK))
    name, _, source = spec.partition("=")
    check_name(name)
    if not source:
        raise BlockError(
            "bad-spec",
            "{!r} names no content file. Use <name>=<path>, or <name>=- to read "
            "the block body from stdin.".format(spec))
    return name, source


def check_name(name):
    if not BLOCK_NAME.match(name):
        raise BlockError(
            "bad-name",
            "{!r} is not a usable block name. Lower-case letters, digits, dot, "
            "dash and underscore, starting with a letter or digit; the blocks "
            "this plugin writes are {}."
            .format(name, " and ".join(KNOWN_BLOCKS)))


def read_content(source):
    if source == "-":
        return sys.stdin.read()
    path = os.path.expanduser(source)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise BlockError("unreadable",
                         "cannot read block content from {}: {}"
                         .format(path, exc.strerror))
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BlockError("not-utf8",
                         "{} is not valid UTF-8.".format(path))


def report(given, real, text, actions, created, changed, written, before_bytes,
           warnings):
    """The JSON a caller branches on.

    Re-parsing the text this run produced is a self-check, not decoration: if
    the result does not parse as one BEGIN/END pair per block, the next run
    would refuse, and the place to find that out is here.
    """
    lines = split_lines(text)
    blocks = parse_blocks(lines, given)
    entries = []
    for name, action in actions:
        entry = {"name": name, "action": action,
                 "begin_line": None, "end_line": None}
        if name in blocks:
            entry["begin_line"] = blocks[name][0] + 1
            entry["end_line"] = blocks[name][1] + 1
        entries.append(entry)
    return {
        "schema": SCHEMA,
        "ok": True,
        "path": given,
        "resolved_path": real if real != given else None,
        "created": created,
        "changed": changed,
        "written": written,
        "bytes_before": before_bytes,
        "bytes_after": len(text.encode("utf-8")),
        "line_ending": EOL_NAMES[dominant_eol(text)],
        "blocks": entries,
        "warnings": warnings,
    }


def cmd_set(args):
    given, real, _ = resolve(args.file)
    specs = []
    seen = set()
    stdin_used = False
    for spec in args.blocks:
        name, source = parse_spec(spec)
        if name in seen:
            raise BlockError(
                "bad-spec",
                "block {!r} is named twice in one call; the second would "
                "immediately replace the first.".format(name))
        seen.add(name)
        if source == "-":
            if stdin_used:
                raise BlockError("bad-spec",
                                 "only one block can be read from stdin.")
            stdin_used = True
        specs.append((name, source))

    text, before = read_file(real)
    created = text is None
    if created and args.no_create:
        raise BlockError(
            "missing-file",
            "{} does not exist and --no-create was given.".format(given))
    check_writable(real, not created)

    warnings = []
    prepared = []
    for name, source in specs:
        body = body_lines(name, read_content(source), source)
        warning = placeholder_warning(name, body, source)
        if warning:
            warnings.append(warning)
        prepared.append((name, body, source))

    before_bytes = len(text.encode("utf-8")) if text is not None else 0
    if created:
        title = args.title or os.path.basename(os.path.dirname(real)) or "Project"
        text = SCAFFOLD.format(title=title, namespace=NAMESPACE)
        warnings.append(
            "{} did not exist and was created — an agent with no instructions "
            "is not a bot. Everything outside the marker blocks is yours to "
            "extend.".format(given))

    eol = dominant_eol(text)
    lines = split_lines(text)
    actions = apply_set(lines, prepared, eol, given)
    new_text = "".join(lines)

    changed = created or new_text != text
    written = False
    if changed and not args.dry_run:
        mode = before.st_mode & 0o777 if before is not None else default_mode()
        atomic_write(real, new_text, mode, before)
        written = True

    return report(given, real, new_text, actions, created, changed, written,
                  before_bytes, warnings)


def cmd_remove(args):
    given, real, _ = resolve(args.file)
    for name in args.blocks:
        check_name(name)

    text, before = read_file(real)
    if text is None:
        if args.missing_ok:
            return {
                "schema": SCHEMA, "ok": True, "path": given,
                "resolved_path": real if real != given else None,
                "created": False, "changed": False, "written": False,
                "bytes_before": 0, "bytes_after": 0, "line_ending": "lf",
                "blocks": [{"name": n, "action": "absent", "begin_line": None,
                            "end_line": None} for n in args.blocks],
                "warnings": ["{} does not exist; nothing to remove."
                             .format(given)],
            }
        raise BlockError(
            "missing-file",
            "{} does not exist. Pass --missing-ok if a missing file is an "
            "acceptable outcome for the caller.".format(given))
    check_writable(real, True)

    before_bytes = len(text.encode("utf-8"))
    lines = split_lines(text)
    actions = apply_remove(lines, args.blocks, given)
    new_text = "".join(lines)

    changed = new_text != text
    written = False
    if changed and not args.dry_run:
        atomic_write(real, new_text, before.st_mode & 0o777, before)
        written = True

    return report(given, real, new_text, actions, False, changed, written,
                  before_bytes, [])


def cmd_status(args):
    given, real, _ = resolve(args.file)
    text, _ = read_file(real)
    if text is None:
        return {"schema": SCHEMA, "ok": True, "path": given,
                "resolved_path": real if real != given else None,
                "exists": False, "bytes": 0, "line_ending": "lf", "blocks": [],
                "warnings": []}
    lines = split_lines(text)
    blocks = parse_blocks(lines, given)
    return {
        "schema": SCHEMA,
        "ok": True,
        "path": given,
        "resolved_path": real if real != given else None,
        "exists": True,
        "bytes": len(text.encode("utf-8")),
        "line_ending": EOL_NAMES[dominant_eol(text)],
        # Document order, not alphabetical: the caller is reading a file, and a
        # list that does not match what is on the screen is a list they have to
        # re-derive.
        "blocks": [{"name": name, "begin_line": span[0] + 1,
                    "end_line": span[1] + 1}
                   for name, span in sorted(blocks.items(),
                                            key=lambda kv: kv[1][0])],
        "warnings": [],
    }


def build_parser():
    parser = argparse.ArgumentParser(
        prog="claude-md-block.py",
        description="Insert, replace or remove telegram-agent-estate's "
                    "marker-delimited blocks in a project's CLAUDE.md.")
    parser.add_argument("--compact", action="store_true",
                        help="one-line JSON instead of indented")
    sub = parser.add_subparsers(dest="command", required=True)

    def compact_too(p):
        """`--compact` on both sides of the subcommand.

        A caller writing the command by hand puts its flags at the end, and an
        argparse that accepts a flag only before the subcommand answers that
        with `unrecognized arguments` — which reads as "this tool cannot do
        that". SUPPRESS is what keeps the subparser's default from overwriting
        a value the parent already parsed.
        """
        p.add_argument("--compact", action="store_true",
                       default=argparse.SUPPRESS,
                       help="one-line JSON instead of indented")

    s = sub.add_parser("set", help="insert or replace one or more blocks")
    s.add_argument("file", help="the CLAUDE.md to edit")
    s.add_argument("blocks", nargs="+", metavar="NAME=CONTENT",
                   help="block name and the file to read its body from "
                        "(`-` for stdin); known names: "
                        + ", ".join(KNOWN_BLOCKS))
    s.add_argument("--title", default=None,
                   help="heading for a CLAUDE.md that has to be created "
                        "(default: the project directory's name)")
    s.add_argument("--no-create", action="store_true",
                   help="fail instead of creating a missing CLAUDE.md")
    s.add_argument("--dry-run", action="store_true",
                   help="report what would change; write nothing")
    compact_too(s)
    s.set_defaults(func=cmd_set)

    r = sub.add_parser("remove", help="remove one or more blocks")
    r.add_argument("file", help="the CLAUDE.md to edit")
    r.add_argument("blocks", nargs="+", metavar="NAME",
                   help="block name; known names: " + ", ".join(KNOWN_BLOCKS))
    r.add_argument("--missing-ok", action="store_true",
                   help="a missing CLAUDE.md is success, not an error")
    r.add_argument("--dry-run", action="store_true",
                   help="report what would change; write nothing")
    compact_too(r)
    r.set_defaults(func=cmd_remove)

    t = sub.add_parser("status", help="report the blocks a CLAUDE.md carries")
    t.add_argument("file", help="the CLAUDE.md to inspect")
    compact_too(t)
    t.set_defaults(func=cmd_status)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        payload = args.func(args)
    except BlockError as exc:
        error = {"code": exc.code, "message": exc.message}
        if exc.line is not None:
            error["line"] = exc.line
        print(json.dumps({"schema": SCHEMA, "ok": False, "error": error},
                         indent=None if args.compact else 2,
                         ensure_ascii=False))
        print(exc.message, file=sys.stderr)
        return 2
    except OSError as exc:
        # Insurance. Every expected OSError is converted at its source; this
        # keeps an unexpected one from reaching a chat flow as a traceback.
        message = "{}: {}".format(args.file, exc.strerror or exc)
        print(json.dumps({"schema": SCHEMA, "ok": False,
                          "error": {"code": "io-error", "message": message}},
                         indent=None if args.compact else 2, ensure_ascii=False))
        print(message, file=sys.stderr)
        return 2

    print(json.dumps(payload, indent=None if args.compact else 2,
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
