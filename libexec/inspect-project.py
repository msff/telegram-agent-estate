#!/usr/bin/env python3
"""Derive everything onboarding can discover about the project a bot is being
attached to, so the conversational flow only asks what it genuinely cannot know.

Run at step 3 of the setup skill — BEFORE the instance venv exists (step 9
builds it). It therefore runs under whatever interpreter the preflight found and
**imports stdlib only**: no `yaml`, no `requests`. That is the same constraint
`estate-bootstrap.zsh` solves with a one-line `sed` rather than a YAML parser,
and for the same reason. Nothing here needs YAML at all: every file it reads
(`.mcp.json`, `~/.claude.json`) is JSON, and `CLAUDE.md`/`README.md` are prose.
The one YAML-shaped thing in the neighbourhood — the instance config — does not
exist yet at this point in the flow, so it is deliberately not read.

    libexec/inspect-project.py [TARGET] [--user-config PATH] [--compact]

Exit 0 with a JSON report on stdout; exit 2 with `{"ok": false, "error": {...}}`
on stdout and a one-line human message on stderr when the target itself is
unusable. A traceback is a bug: the caller is a chat flow, not a shell.

WHAT IT WILL NOT PRINT
----------------------
`~/.claude.json` and a project `.mcp.json` routinely hold live credentials — in
an MCP server's `env` block, in an `Authorization` header, in an `args` list, in
a URL query string. This script reports **that a server exists, which scope it
came from, and which environment variable NAMES it needs**, and never a value.
That is not defensive decoration: the report is read by an agent, so anything
emitted here lands in a model's context, in the flow's transcript, and possibly
in a log. Later steps write `${VAR}` references into `mcp-config.json` and put
the values in a mode-600 env file (KTD10), which is what the names are for.
Command strings lifted out of `CLAUDE.md` get the same redaction pass, because a
project's own docs are not guaranteed to be clean either.

OUTPUT SHAPE — `schema` pins it; a consumer should check it
-----------------------------------------------------------
```
{
  "schema": "telegram-agent-estate/inspect-project@1",
  "ok": true,
  "workdir": "/abs/path",                  # the inspected directory, absolute
  "instance": {
    "slug": "my-project",                  # safe for launchd labels, lock file
                                           #   names, state paths, pgrep patterns
    "label": "My Project",                 # human-facing, unicode kept
    "derived_from": "<directory name>",
    "notes": ["..."]                       # say so when the slug was truncated,
  },                                       #   prefixed, or fell back
  "git": {"is_repo": true, "root": "/abs/path"},
  "synced_storage": {                      # venvs corrupt here and a lock file
    "synced": true,                        #   on a synced volume is not a lock
    "provider": "Dropbox",
    "matched": "/abs/path",
    "via": "path"                          # "path" | "realpath" (symlinked in)
  },
  "claude_md": {
    "exists": true, "path": "...", "bytes": 1234,
    "commands": [{"command": "...", "line": 42, "context": "fenced"}],
    "commands_truncated": false,
    "estate_markers": [{"line": 10, "text": "<!-- ... -->"}]
  },                                       # sightings only — the marker
                                           #   vocabulary belongs to
                                           #   claude-md-block.py
  "project_description": {                 # raw material for the agent brief,
    "source": "README.md",                 #   extracted rather than dumped
    "title": "...", "summary": "...", "truncated": true
  },
  "mcp_servers": [{
    "name": "whoop",
    "scope": "project" | "local" | "user", # .mcp.json | ~/.claude.json
                                           #   projects.<abspath>.mcpServers |
                                           #   ~/.claude.json mcpServers
    "scope_detail": "...", "source": "/abs/....json",
    "transport": "stdio", "command": "npx",# basename only
    "url_host": null,                      # host only — no userinfo, no query
    "env_var_names": ["WHOOP_CLIENT_ID"],  # NAMES; values never leave the file
    "config_keys": ["command", "args", "env"],
    "values_withheld": true,
    "disabled_in_project": false
  }],
  "scripts": [{"path": "scripts/x.py", "executable": true,
               "interpreter": "python3"}],
  "scripts_truncated": false,
  "allowlist_candidates": [{"rule": "Bash({python} scripts/x.py *)",
                            "origin": "scripts" | "claude_md",
                            "detail": "...", "line": null}],
  "opt_in_required": ["mcp_servers", "allowlist_candidates"],
  "warnings": ["..."]
}
```

`{python}` in a candidate rule is a PLACEHOLDER the flow substitutes with the
instance's venv interpreter once step 9 has built it. It is written that way
because it cannot be known yet and because the allowlist matcher denies any
command containing shell-variable expansion — `$PY` in a rule is not a
late-binding variable, it is a rule that never matches (verified 2026-07-28, see
`templates/permissions.json`). Substitute the literal path; do not emit `$VAR`.

`opt_in_required` is part of the contract, not commentary. Under `dontAsk` an
allow entry means the bot runs that thing with no prompt, so a repo containing
`scripts/deploy-prod.sh` would put production one chat message away. Everything
listed there is a CANDIDATE, defaulted off, and needs an affirmative per-item
confirmation before it reaches `permissions.json` or `mcp-config.json` (R10).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata

SCHEMA = "telegram-agent-estate/inspect-project@1"

# Read caps. A report that a model has to read must stay bounded, and an
# accidental 40 MB `CLAUDE.md` (generated logs get committed) should truncate
# rather than blow up the flow's context.
CLAUDE_MD_LIMIT = 256 * 1024
README_LIMIT = 128 * 1024
JSON_LIMIT = 2 * 1024 * 1024
SUMMARY_LIMIT = 600
MAX_COMMANDS = 40
MAX_COMMAND_CHARS = 400
MAX_SCRIPTS = 60
MAX_MARKERS = 20
MAX_SLUG = 32
SLUG_FALLBACK = "agent"

SCRIPT_DIRS = ("scripts", "bin")
SCRIPT_SUFFIXES = {".py", ".sh", ".zsh", ".bash", ".rb", ".js", ".mjs", ".ts",
                   ".pl", ".php", ".applescript", ".osa", ".expect"}
SCRIPT_WALK_DEPTH = 3
SKIP_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv", "env",
             ".env", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist",
             "build", ".tox", "site-packages", ".idea", ".vscode"}

README_NAMES = ("readme.md", "readme.markdown", "readme.rst", "readme.txt",
                "readme")

# Providers whose sync daemon rewrites files under the agent's feet. This is the
# single most common way the setup breaks: a venv inside one of these corrupts,
# and flock on a synced volume is advisory over a file the daemon may replace,
# so the drain lock stops being a lock. Detection is by path shape because
# there is no reliable API for "is this volume synced".
SYNC_PATTERNS = (
    ("Dropbox", re.compile(r"(?:^|/)Dropbox(?: \([^/]*\))?(?:/|$)")),
    ("iCloud Drive", re.compile(
        r"(?:^|/)Library/Mobile Documents(?:/|$)"
        r"|(?:^|/)com~apple~CloudDocs(?:/|$)"
        r"|(?:^|/)iCloud Drive(?:/|$)")),
    ("Google Drive", re.compile(
        r"(?:^|/)Library/CloudStorage/GoogleDrive-[^/]*(?:/|$)"
        r"|(?:^|/)Google ?Drive(?:/|$)"
        r"|(?:^|/)(?:My Drive|Shared drives)(?:/|$)")),
    ("OneDrive", re.compile(
        r"(?:^|/)Library/CloudStorage/OneDrive-[^/]*(?:/|$)"
        r"|(?:^|/)OneDrive(?: - [^/]*)?(?:/|$)")),
    ("Box", re.compile(r"(?:^|/)Library/CloudStorage/Box-[^/]*(?:/|$)"
                       r"|(?:^|/)Box Sync(?:/|$)")),
    # Anything else macOS mounts through the File Provider extension point.
    ("cloud storage", re.compile(r"(?:^|/)Library/CloudStorage/[^/]+(?:/|$)")),
)

REDACTED = "[redacted]"

# Value-shaped secrets, redacted out of anything lifted from a project's own
# files. Deliberately narrow: a broad pattern mangles legitimate command text,
# which is the raw material the allowlist candidates are built from.
_KEYWORD_SECRET = re.compile(
    r"(?i)\b(api[-_]?key|apikey|access[-_]?token|auth[-_]?token|token|secret|"
    r"password|passwd|client[-_]?secret)\b(\s*[:=]\s*|\s+)"
    r"(\"[^\"]*\"|'[^']*'|\S+)")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._~+/=-]{8,})")
_LITERAL_SECRETS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{16,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{8,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}"),          # telegram bot token
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
)
# A `${VAR}` reference is not a secret and is the thing we WANT to keep.
_VAR_ONLY = re.compile(r"^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?$")
_VAR_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}"
                      r"|\$([A-Za-z_][A-Za-z0-9_]*)")


class TargetError(Exception):
    """The inspected directory itself is unusable — the one fatal condition."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# --- redaction ----------------------------------------------------------------

def redact(text):
    """Mask credential-shaped values while leaving `${VAR}` references intact."""
    if not text:
        return text

    def keyword(m):
        value = m.group(3)
        if _VAR_ONLY.match(value.strip("\"'")):
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}{REDACTED}"

    out = _KEYWORD_SECRET.sub(keyword, text)
    out = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", out)
    for pattern in _LITERAL_SECRETS:
        out = pattern.sub(REDACTED, out)
    return out


def _var_names(value, into):
    """Environment-variable NAMES referenced anywhere in a config value."""
    if isinstance(value, str):
        for m in _VAR_REF.finditer(value):
            into.add(m.group(1) or m.group(2))
    elif isinstance(value, dict):
        for v in value.values():
            _var_names(v, into)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _var_names(v, into)


# --- identity -----------------------------------------------------------------

def slugify(raw, notes=None):
    """A slug safe for a launchd label, a lock filename, a state path and a
    `pgrep -f` pattern — all four at once, because `name` becomes all four.

    So: ASCII, lowercase, `[a-z0-9-]` only, no leading digit, bounded length.
    Dots go because the launchd label is `<prefix>.<name>` and a dot would add a
    segment that `install-instance.sh`'s clobber scan (`<prefix>.<name>*`) reads
    as a different instance. Regex metacharacters go because the name is
    interpolated into `poller_match()`.
    """
    notes = notes if notes is not None else []
    text = unicodedata.normalize("NFKD", str(raw))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    if not text:
        notes.append(
            "the directory name yielded no ASCII slug characters; the slug is a "
            "placeholder and should be confirmed with the user")
        return SLUG_FALLBACK
    if len(text) > MAX_SLUG:
        cut = text[:MAX_SLUG]
        if "-" in cut[1:]:
            cut = cut[:cut.rindex("-")]
        text = cut.strip("-") or text[:MAX_SLUG].strip("-")
        notes.append(f"slug truncated to {len(text)} characters")
    if text[0].isdigit():
        text = f"{SLUG_FALLBACK}-{text}"
        notes.append(
            "slug prefixed because it started with a digit — a leading digit "
            "reads badly in a reverse-DNS launchd label")
    return text


def humanize(raw):
    """Human label. Unicode is kept: this one is only ever shown to a person."""
    text = re.sub(r"[_.\-]+", " ", str(raw))
    text = re.sub(r"\s+", " ", text).strip()
    words = [w if any(c.isupper() for c in w) else w.capitalize()
             for w in text.split(" ") if w]
    return (" ".join(words))[:60] or "Agent"


# --- filesystem facts ---------------------------------------------------------

def detect_git(workdir):
    """Walk up looking for `.git`, without shelling out to git.

    A file rather than a directory is the worktree/submodule form and counts.
    `git` may not be on the sanitized PATH this runs under, and "is this a repo"
    is not worth a subprocess.
    """
    cur = os.path.abspath(workdir)
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return {"is_repo": True, "root": cur}
        parent = os.path.dirname(cur)
        if parent == cur:
            return {"is_repo": False, "root": None}
        cur = parent


def detect_synced_storage(workdir):
    """Dropbox / iCloud / Google Drive et al, by path and by resolved path.

    The realpath pass matters: a symlink from `~/projects/foo` into Dropbox is a
    normal way to organise things and looks entirely local from the path alone.
    """
    candidates = [("path", os.path.abspath(workdir))]
    real = os.path.realpath(workdir)
    if real != candidates[0][1]:
        candidates.append(("realpath", real))
    for via, path in candidates:
        for provider, pattern in SYNC_PATTERNS:
            if pattern.search(path):
                return {"synced": True, "provider": provider,
                        "matched": path, "via": via}
    return {"synced": False, "provider": None, "matched": None, "via": None}


def _read_text(path, limit, warnings):
    try:
        with open(path, "rb") as fh:
            raw = fh.read(limit + 1)
    except OSError as exc:
        warnings.append(f"could not read {path}: {exc.strerror or exc}")
        return None, False
    truncated = len(raw) > limit
    return raw[:limit].decode("utf-8", "replace"), truncated


def _load_json(path, warnings):
    """None when absent; None plus a warning when unreadable or malformed.

    A malformed `.mcp.json` must not end the inspection — the flow can still
    offer everything else, and "your .mcp.json does not parse" is itself a
    useful thing to tell the user.
    """
    if not os.path.isfile(path):
        return None
    text, truncated = _read_text(path, JSON_LIMIT, warnings)
    if text is None:
        return None
    if truncated:
        warnings.append(f"{path} is larger than {JSON_LIMIT} bytes; not parsed")
        return None
    try:
        data = json.loads(text)
    except ValueError as exc:
        warnings.append(f"{path} is not valid JSON ({exc}); skipped")
        return None
    if not isinstance(data, dict):
        warnings.append(f"{path} is not a JSON object; skipped")
        return None
    return data


# --- CLAUDE.md ----------------------------------------------------------------

_FENCE = re.compile(r"^\s*(?:```|~~~)\s*([A-Za-z0-9_+-]*)\s*$")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_PROMPT = re.compile(r"^\s*[$%>]\s+")
_TOKEN_OK = re.compile(r"^[\w./~@+-]+$")
_MARKER_SIGHTING = re.compile(r"^\s*<!--.*telegram-agent-estate.*-->\s*$",
                              re.IGNORECASE)

SHELL_LANGS = {"", "sh", "bash", "zsh", "shell", "console", "shell-session",
               "shellsession", "sh-session", "text"}

COMMON_COMMANDS = {
    "python", "python3", "node", "npm", "npx", "pnpm", "yarn", "bun", "deno",
    "pytest", "git", "make", "cargo", "go", "ruby", "bundle", "rake", "docker",
    "uv", "poetry", "pip", "pip3", "java", "dotnet", "swift", "php", "composer",
    "psql", "sqlite3", "jq", "curl", "wget", "rsync", "brew", "open",
    "osascript", "launchctl", "tmux", "screen", "zsh", "bash", "sh", "ffmpeg",
    "pandoc", "sips", "say", "terraform", "kubectl", "gh",
}
INTERPRETERS = {"python", "python3", "node", "ruby", "php", "perl", "bun",
                "deno", "zsh", "bash", "sh"}


def _looks_like_command(line, context="fenced"):
    """Is this line an instruction to RUN something, or is it prose?

    The first token decides. A known binary, a script suffix, or an
    extensionless path is a command; `state/session-handoff.md` and `data/whoop/`
    — files and directories a `CLAUDE.md` mentions constantly — are not, and
    neither is a sentence.

    Inline code spans are held to a stricter bar than fenced blocks: prose says
    "the exporter lives in `scripts/export.py`" far more often than it issues a
    one-word instruction, so a single-token span only counts when the token is a
    known binary. A fenced shell block is already a claim that its lines are
    commands.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return False
    line = _PROMPT.sub("", line).strip()
    if not line:
        return False
    tokens = line.split()
    first = tokens[0]
    if not _TOKEN_OK.match(first) or first.endswith("/"):
        return False
    base = first.rsplit("/", 1)[-1]
    ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    known = (base in COMMON_COMMANDS
             or base.rstrip("0123456789.") in COMMON_COMMANDS)
    if context == "inline" and len(tokens) == 1 and not known:
        return False
    if known:
        return True
    if f".{ext}" in SCRIPT_SUFFIXES:
        return True
    if "/" in first and not ext:
        return True
    return False


def _clean_command(line):
    line = _PROMPT.sub("", line.strip()).strip()
    line = redact(line)
    if len(line) > MAX_COMMAND_CHARS:
        line = line[:MAX_COMMAND_CHARS] + " …"
    return line


def extract_commands(text):
    """Commands a `CLAUDE.md` tells the agent to run, with where they were seen.

    Fenced shell blocks and inline code spans, deduped, first sighting wins.
    Returns (commands, truncated).
    """
    commands = []
    seen = set()
    truncated = False
    in_fence = False
    fence_is_shell = False
    pending = ""
    pending_line = 0

    for lineno, raw in enumerate(text.splitlines(), 1):
        fence = _FENCE.match(raw)
        if fence:
            if in_fence:
                in_fence = False
                pending = ""
            else:
                in_fence = True
                fence_is_shell = fence.group(1).lower() in SHELL_LANGS
            continue

        candidates = []
        if in_fence:
            if not fence_is_shell:
                continue
            line = raw.rstrip()
            if pending:
                line = pending + " " + line.strip()
            if line.rstrip().endswith("\\"):
                # A continuation: hold it, emit the joined command.
                pending = line.rstrip()[:-1].rstrip()
                pending_line = pending_line or lineno
                continue
            candidates = [(line, lineno if not pending_line else pending_line,
                           "fenced")]
            pending, pending_line = "", 0
        else:
            candidates = [(m.group(1), lineno, "inline")
                          for m in _INLINE_CODE.finditer(raw)]

        for candidate, at, context in candidates:
            if not _looks_like_command(candidate, context):
                continue
            command = _clean_command(candidate)
            if not command or command in seen:
                continue
            if len(commands) >= MAX_COMMANDS:
                truncated = True
                break
            seen.add(command)
            commands.append({"command": command, "line": at,
                             "context": context})
        if truncated:
            break
    return commands, truncated


def read_claude_md(workdir, warnings):
    path = os.path.join(workdir, "CLAUDE.md")
    if not os.path.isfile(path):
        return {"exists": False, "path": None, "bytes": 0, "commands": [],
                "commands_truncated": False, "estate_markers": []}, None
    text, truncated = _read_text(path, CLAUDE_MD_LIMIT, warnings)
    if text is None:
        return {"exists": True, "path": path, "bytes": 0, "commands": [],
                "commands_truncated": False, "estate_markers": []}, None
    if truncated:
        warnings.append(
            f"{path} is larger than {CLAUDE_MD_LIMIT} bytes; only the first "
            f"{CLAUDE_MD_LIMIT} were inspected")
    commands, commands_truncated = extract_commands(text)
    markers = [{"line": n, "text": line.strip()}
               for n, line in enumerate(text.splitlines(), 1)
               if _MARKER_SIGHTING.match(line)][:MAX_MARKERS]
    try:
        size = os.path.getsize(path)
    except OSError:
        size = len(text.encode("utf-8"))
    return {
        "exists": True,
        "path": path,
        "bytes": size,
        "commands": commands,
        "commands_truncated": commands_truncated or truncated,
        "estate_markers": markers,
    }, text


# --- what the project IS ------------------------------------------------------

_BADGE = re.compile(r"^\s*\[?!\[")
_HTML = re.compile(r"^\s*<[^>]+>")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_RST_UNDERLINE = re.compile(r"^[=\-~`^\"'*+#]{3,}\s*$")


def summarize(text):
    """Title plus the first few real paragraphs — the raw material the agent
    brief is written from.

    Extraction, not a dump: the brief goes into someone's `CLAUDE.md` and gets
    re-read on every turn, so an entire README pasted in is a permanent context
    tax and reads as noise. Badges, HTML, fences and headings are dropped.
    """
    title = None
    paragraphs = []
    current = []
    in_fence = False
    for raw in text.splitlines():
        if _FENCE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = raw.rstrip()
        stripped = line.strip()
        heading = _HEADING.match(line)
        if heading:
            if title is None:
                title = heading.group(1).strip()
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        if not stripped or _BADGE.match(line) or _HTML.match(line) \
                or _RST_UNDERLINE.match(stripped):
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        if title is None and not paragraphs and not current:
            # No `# heading` yet: an RST/plain README's first line is the title.
            title = stripped
            continue
        current.append(re.sub(r"\s+", " ", stripped.lstrip("> ")))
    if current:
        paragraphs.append(" ".join(current))

    summary = ""
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        summary = para if not summary else f"{summary}\n\n{para}"
        if len(summary) >= SUMMARY_LIMIT:
            break
    truncated = len(summary) > SUMMARY_LIMIT
    if truncated:
        summary = summary[:SUMMARY_LIMIT].rstrip() + " …"
    return title, (redact(summary) or None), truncated


def find_description(workdir, claude_md_text, warnings):
    """A README if there is one; the `CLAUDE.md` prose if there is not."""
    source_path = None
    try:
        entries = sorted(os.listdir(workdir))
    except OSError as exc:
        warnings.append(f"could not list {workdir}: {exc.strerror or exc}")
        entries = []
    by_lower = {name.lower(): name for name in entries}
    for want in README_NAMES:
        if want in by_lower:
            source_path = os.path.join(workdir, by_lower[want])
            break
    if source_path is None:
        for sub in ("docs", "doc"):
            candidate = os.path.join(workdir, sub, "README.md")
            if os.path.isfile(candidate):
                source_path = candidate
                break

    if source_path and os.path.isfile(source_path):
        text, _ = _read_text(source_path, README_LIMIT, warnings)
        if text:
            title, summary, truncated = summarize(text)
            return {"source": os.path.relpath(source_path, workdir),
                    "title": title, "summary": summary, "truncated": truncated}
    if claude_md_text:
        title, summary, truncated = summarize(claude_md_text)
        if summary or title:
            return {"source": "CLAUDE.md", "title": title, "summary": summary,
                    "truncated": truncated}
    return {"source": None, "title": None, "summary": None, "truncated": False}


# --- MCP servers --------------------------------------------------------------

def _server_summary(name, cfg, scope, scope_detail, source, disabled):
    """One server, described well enough to opt in to and never well enough to
    authenticate with."""
    cfg = cfg if isinstance(cfg, dict) else {}
    env_names = set()
    env_block = cfg.get("env")
    if isinstance(env_block, dict):
        env_names.update(str(k) for k in env_block)
    _var_names(cfg, env_names)

    command = cfg.get("command")
    command_base = os.path.basename(str(command)) if command else None

    url_host = None
    url = cfg.get("url") or cfg.get("serverUrl") or cfg.get("httpUrl")
    if isinstance(url, str) and url:
        try:
            from urllib.parse import urlsplit
            # `.hostname` and nothing else: netloc can carry `user:token@`, and
            # a query string is where hosted MCP servers put the API key.
            url_host = urlsplit(url).hostname
        except (ValueError, ImportError):
            url_host = None

    transport = cfg.get("type") if cfg.get("type") in (
        "stdio", "http", "sse", "ws", "websocket") else None
    if transport is None:
        transport = "stdio" if command else ("http" if url else "unknown")

    return {
        "name": name,
        "scope": scope,
        "scope_detail": scope_detail,
        "source": source,
        "transport": transport,
        "command": command_base,
        "url_host": url_host,
        "env_var_names": sorted(env_names),
        "config_keys": sorted(str(k) for k in cfg),
        "values_withheld": True,
        "disabled_in_project": bool(disabled),
    }


def _servers_of(data):
    """`{"mcpServers": {...}}` is the documented shape; a bare map of servers
    turns up in hand-written files often enough to accept."""
    if not isinstance(data, dict):
        return {}
    block = data.get("mcpServers")
    if isinstance(block, dict):
        return block
    if block is None and data and all(isinstance(v, dict) for v in data.values()):
        return data
    return {}


def collect_mcp_servers(workdir, user_config_path, warnings):
    """All three places a server in scope for this project can be declared.

    Reporting the scope is not cosmetic: the user is deciding per item whether a
    server the bot may call is one they meant to expose, and "this comes from
    your user-scope config, not from this repo" is most of that judgement.
    """
    servers = []

    project_path = os.path.join(workdir, ".mcp.json")
    project_data = _load_json(project_path, warnings)

    user_path = os.path.abspath(os.path.expanduser(user_config_path))
    user_data = _load_json(user_path, warnings) if user_config_path else None

    project_entry = {}
    matched_key = None
    if isinstance(user_data, dict):
        projects = user_data.get("projects")
        if isinstance(projects, dict):
            for key in (os.path.abspath(workdir), os.path.realpath(workdir),
                        os.path.abspath(workdir).rstrip("/")):
                if key in projects and isinstance(projects[key], dict):
                    project_entry = projects[key]
                    matched_key = key
                    break

    def _disabled(field, name):
        names = project_entry.get(field)
        return isinstance(names, list) and name in names

    for name, cfg in sorted(_servers_of(project_data).items()):
        servers.append(_server_summary(
            name, cfg, "project", ".mcp.json in the project", project_path,
            _disabled("disabledMcpjsonServers", name)))

    local_block = project_entry.get("mcpServers")
    if isinstance(local_block, dict):
        for name, cfg in sorted(local_block.items()):
            servers.append(_server_summary(
                name, cfg, "local",
                f'projects["{matched_key}"].mcpServers', user_path,
                _disabled("disabledMcpServers", name)))

    for name, cfg in sorted(_servers_of(
            {"mcpServers": (user_data or {}).get("mcpServers")}).items()):
        servers.append(_server_summary(
            name, cfg, "user", "top-level mcpServers", user_path,
            _disabled("disabledMcpServers", name)))

    counts = {}
    for entry in servers:
        counts.setdefault(entry["name"], []).append(entry["scope"])
    for name, scopes in sorted(counts.items()):
        if len(scopes) > 1:
            warnings.append(
                f"MCP server {name!r} is declared in more than one scope "
                f"({', '.join(scopes)}) — confirm which one you mean")
    return servers


# --- scripts ------------------------------------------------------------------

def _shebang_interpreter(path):
    try:
        with open(path, "rb") as fh:
            first = fh.readline(256)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    line = first[2:].decode("utf-8", "replace").strip()
    parts = line.split()
    if not parts:
        return None
    interp = os.path.basename(parts[0])
    if interp == "env" and len(parts) > 1:
        interp = os.path.basename(parts[1])
    return interp or None


def collect_scripts(workdir, warnings):
    """Executable-or-script-shaped files under `scripts/` and `bin/`.

    A missing exec bit is reported rather than used as a filter: a fresh clone
    on a filesystem that dropped the mode, or a `.py` that is always run through
    an interpreter, is still a script the user may want allowlisted. The flag is
    there so the flow can say which is which.
    """
    found = []
    truncated = False
    for top in SCRIPT_DIRS:
        root = os.path.join(workdir, top)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            rel_depth = len(os.path.relpath(dirpath, root).split(os.sep)) \
                if dirpath != root else 0
            if rel_depth >= SCRIPT_WALK_DEPTH:
                dirnames[:] = []
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in SKIP_DIRS and not d.startswith("."))
            for filename in sorted(filenames):
                if filename.startswith("."):
                    continue
                full = os.path.join(dirpath, filename)
                if os.path.islink(full) and not os.path.exists(full):
                    warnings.append(f"broken symlink skipped: {full}")
                    continue
                if not os.path.isfile(full):
                    continue
                ext = os.path.splitext(filename)[1].lower()
                executable = os.access(full, os.X_OK)
                if not executable and ext not in SCRIPT_SUFFIXES:
                    continue
                if len(found) >= MAX_SCRIPTS:
                    truncated = True
                    break
                found.append({
                    "path": os.path.relpath(full, workdir).replace(os.sep, "/"),
                    "executable": executable,
                    "interpreter": _shebang_interpreter(full),
                })
            if truncated:
                break
        if truncated:
            break
    found.sort(key=lambda s: s["path"])
    return found, truncated


# --- allowlist candidates -----------------------------------------------------

PYTHON_INTERPRETERS = {"python", "python3"}


def build_allowlist_candidates(scripts, commands):
    """Proposed `Bash(...)` rules — CANDIDATES, defaulted off (see module doc).

    One rule per script, never a wildcard over a directory: a directory-wide
    rule allows whatever lands there next, which is the difference between "the
    bot can run the two scripts I showed it" and "the bot can run anything a
    future commit adds".
    """
    out = []
    seen = set()

    def add(rule, origin, detail, line=None):
        if not rule or rule in seen:
            return
        seen.add(rule)
        out.append({"rule": rule, "origin": origin, "detail": detail,
                    "line": line})

    for script in scripts:
        path = script["path"]
        ext = os.path.splitext(path)[1].lower()
        interp = (script.get("interpreter") or "").rstrip("0123456789.")
        if ext == ".py" or interp == "python":
            add(f"Bash({{python}} {path} *)", "scripts", path)
        elif script["executable"]:
            add(f"Bash({path} *)", "scripts", path)
        elif ext in SCRIPT_SUFFIXES:
            add(f"Bash({path} *)", "scripts", path)

    for entry in commands:
        tokens = entry["command"].split()
        if not tokens:
            continue
        head = tokens[0]
        base = os.path.basename(head).rstrip("0123456789.")
        if base in PYTHON_INTERPRETERS and len(tokens) > 1 \
                and not tokens[1].startswith("-"):
            add(f"Bash({{python}} {tokens[1]} *)", "claude_md",
                entry["command"], entry["line"])
        elif base in INTERPRETERS and len(tokens) > 1 \
                and not tokens[1].startswith("-"):
            add(f"Bash({head} {tokens[1]} *)", "claude_md",
                entry["command"], entry["line"])
        else:
            add(f"Bash({head} *)", "claude_md", entry["command"], entry["line"])
    return out


# --- the report ---------------------------------------------------------------

def inspect(target, user_config="~/.claude.json"):
    workdir = os.path.abspath(os.path.expanduser(str(target)))
    if not os.path.exists(workdir):
        raise TargetError("not-found", f"no such directory: {workdir}")
    if not os.path.isdir(workdir):
        raise TargetError("not-a-directory", f"not a directory: {workdir}")
    if not os.access(workdir, os.R_OK | os.X_OK):
        raise TargetError(
            "unreadable",
            f"cannot read {workdir} (permission denied) — inspection needs read "
            f"and execute on the project directory")

    warnings = []
    notes = []
    dirname = os.path.basename(workdir.rstrip(os.sep)) or workdir

    claude_md, claude_md_text = read_claude_md(workdir, warnings)
    description = find_description(workdir, claude_md_text, warnings)
    servers = collect_mcp_servers(workdir, user_config, warnings)
    scripts, scripts_truncated = collect_scripts(workdir, warnings)

    synced = detect_synced_storage(workdir)
    if synced["synced"]:
        warnings.append(
            f"the project directory is inside {synced['provider']} — put the "
            f"venv and the runtime state dir OUTSIDE it: a synced venv corrupts "
            f"and a lock file on a synced volume is not a lock")

    return {
        "schema": SCHEMA,
        "ok": True,
        "workdir": workdir,
        "instance": {
            "slug": slugify(dirname, notes),
            "label": humanize(dirname),
            "derived_from": dirname,
            "notes": notes,
        },
        "git": detect_git(workdir),
        "synced_storage": synced,
        "claude_md": claude_md,
        "project_description": description,
        "mcp_servers": servers,
        "scripts": scripts,
        "scripts_truncated": scripts_truncated,
        "allowlist_candidates": build_allowlist_candidates(
            scripts, claude_md["commands"]),
        "opt_in_required": ["mcp_servers", "allowlist_candidates"],
        "warnings": warnings,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="inspect-project.py",
        description="Report what onboarding can derive about a project.")
    parser.add_argument("target", nargs="?", default=".",
                        help="project directory (default: the current one)")
    parser.add_argument("--user-config", default="~/.claude.json",
                        help="path to the user's claude.json "
                             "(default: ~/.claude.json)")
    parser.add_argument("--compact", action="store_true",
                        help="one-line JSON instead of indented")
    args = parser.parse_args(argv)

    try:
        report = inspect(args.target, args.user_config)
    except TargetError as exc:
        payload = {"schema": SCHEMA, "ok": False,
                   "error": {"code": exc.code, "message": exc.message}}
        print(json.dumps(payload, indent=None if args.compact else 2))
        print(exc.message, file=sys.stderr)
        return 2

    print(json.dumps(report, indent=None if args.compact else 2,
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
