"""The README as a publishing artifact, not as prose.

Most of what a README should say cannot be asserted — whether the framing is
honest, whether a stranger comes away with the right model. What CAN be
asserted is that the load-bearing disclosures are still present and that the
factual claims still match the repo, and both of those are exactly the things
that rot silently: a dependency gets added, a diagram keeps pointing at a script
that was renamed, someone tightens a paragraph and the sentence saying "this is
not a sandbox" goes with it.

So read these as regression guards on presence, not as proof of quality. A
reviewer still has to read the section; this only makes sure there is one.
"""
import re
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
README = PLUGIN_ROOT / "README.md"
REQUIREMENTS = PLUGIN_ROOT / "requirements.txt"

REQ_LINE = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*(?P<spec>[<>=!~].*)$")


def readme():
    return README.read_text(encoding="utf-8")


def declared_requirements():
    """(name, full spec) for every requirement line in requirements.txt."""
    out = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = REQ_LINE.match(line)
        assert m, f"unparsed requirement line: {line!r}"
        out.append((m.group("name"), line))
    return out


def test_requirements_parse_at_all():
    """Guards the guard: a parser that silently yields nothing would make the
    dependency comparison below pass on any README at all."""
    reqs = declared_requirements()
    assert len(reqs) >= 3
    assert "PyYAML" in dict(reqs)


def test_the_readme_lists_exactly_the_requirements_file_s_dependencies():
    """A stranger sizes the install from the README and audits it from the
    requirements file. When the two disagree, the README is the one they
    believed."""
    text = readme()
    for name, spec in declared_requirements():
        assert f"`{spec}`" in text, (
            f"requirements.txt pins {spec!r}; the README does not say so")
    # ...and nothing the README claims is a dependency has quietly been dropped
    listed = set(re.findall(r"`([A-Za-z0-9._-]+)[<>=!~][^`]*`", text))
    declared = {name for name, _ in declared_requirements()}
    assert listed <= declared, (
        f"the README names packages requirements.txt does not: "
        f"{sorted(listed - declared)}")


def test_the_readme_states_the_python_floor_and_the_homebrew_prerequisite():
    """macOS ships 3.9 in /usr/bin, so "any python3" is a wrong default that
    fails after the install rather than before it."""
    text = readme()
    assert "3.11" in text
    assert re.search(r"homebrew", text, re.IGNORECASE)


def test_the_readme_declares_macos_and_launchd_only():
    """KTD7. A Linux user should learn this from the README, not from
    `launchctl: command not found` three steps into a setup flow."""
    text = readme()
    assert re.search(r"\*\*macOS only\.\*\*", text), "no plain macOS-only claim"
    assert "launchd" in text


def test_the_readme_states_a_support_posture():
    """R16. Which posture is the author's call; having none is not an option —
    a public repo with launchd daemons in it sets an expectation either way."""
    text = readme().lower()
    assert re.search(r"support posture:\s*\*{0,2}(maintained|best-effort"
                     r"|unsupported)", text), "no stated support posture"


def test_the_readme_says_the_allowlist_is_not_a_sandbox():
    """R15's README half, and the single most important sentence here.

    A stranger reading `permissions.json` as a security boundary and pointing a
    bot at something sensitive is the worst outcome of publishing this. The
    allowlist stops accidents; inline python is allowed, so it contains nothing
    determined."""
    text = readme()
    assert "not a sandbox" in text.lower()
    assert re.search(r"inline python", text, re.IGNORECASE), (
        "the not-a-sandbox claim must say WHY, or it reads as boilerplate")


def test_the_readme_names_the_boundary_that_does_exist():
    """Saying only "not a sandbox" overshoots into "nothing protects you". The
    owner-gated transport and the owner-allowlisted send path are real, and
    they are the whole of it."""
    text = readme().lower()
    assert "owner-gated" in text or "owner-only" in text
    assert "send.py" in text


@pytest.mark.parametrize("token", ["send_transaction.py"])
def test_the_readme_does_not_name_scripts_this_repo_does_not_ship(token):
    """The architecture diagram's output node named another project's script
    for as long as this README existed."""
    assert token not in readme()


def test_every_script_the_readme_names_exists():
    """Repo-relative paths only — the lookbehind keeps a venv's `bin/python3`
    out of the sweep, which is a path on the reader's machine, not here."""
    text = readme()
    named = set(re.findall(
        r"(?<![\w/~.-])(?:libexec|bin|templates|tests|skills)/[A-Za-z0-9._/-]+",
        text))
    assert named, "the README names no repo paths at all — regex broken?"
    missing = sorted(n for n in named
                     if not (PLUGIN_ROOT / n.rstrip("/.")).exists()
                     and not (PLUGIN_ROOT / n).exists())
    assert not missing, f"the README points at paths that do not exist: {missing}"


def test_the_readme_no_longer_describes_itself_as_a_scaffold():
    """It said "Status: Scaffold (U12) … transplanted in U13" while the stack
    was complete and had already moved to libexec/."""
    text = readme().lower()
    assert "scaffold" not in text
    assert not re.search(r"\bu1[0-9]\b", text), "internal unit ids leaked"
