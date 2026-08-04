"""Put `libexec/` on sys.path for the test suite, and declare the parity switches.

The plugin's executables live in `libexec/` and are run directly (launchd
invokes them by absolute path), so there is no package to install and no `src`
layout to lean on. Tests import them as plain modules. They live under
`libexec/` and not `bin/` because Claude Code puts an enabled plugin's `bin/` on
the Bash tool's PATH — `bin/` holds one namespaced dispatcher and nothing else
(KTD5).

Deliberately NOT setting any state-directory environment variable here. Every
test that touches state must redirect it explicitly and visibly, because a
suite-wide default is exactly how a test run reaches production: on 2026-07-31
the reading suite pruned the LIVE handoff file, and the reason was a path that
looked redirected but resolved through a module constant instead. A redirect you
can see in the test body is one you can check.

THE PARITY SUITE'S TWO MODES ARE DECLARED HERE. `--real` is the switch, and it
lives in this file because a pytest option that nothing declares is not an
option: pytest rejects the whole run with `unrecognized arguments: --real`
before a single test is collected. `libexec/parity.sh` passed that flag from the
day it was written, so real mode — the only mode that can prove a permission
allowlist complete — had never once run.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "libexec"))

# How real mode reaches the test module. The FLAG is canonical; this variable is
# how the decision travels to code that runs before any fixture (the parity
# module picks its skip marker at import time) and to a turn's subprocess.
REAL_ENV = "ESTATE_PARITY_REAL"

# The instance a --real drill borrows its workdir, allowlist and MCP config
# from. There is no default, for the same reason `instance_config` has none: a
# drill that cannot say which instance it is testing would run its turns
# against whatever happened to be nearby.
LIVE_CONFIG_ENV = "ESTATE_PARITY_LIVE_CONFIG"

# The chat id the suite acts as when no real instance is in play. It belongs to
# nobody, and specifically not to whoever wrote these tests: a real owner id
# baked into a test module reads as a neutral fixture value right up until a
# stranger runs the suite.
PLACEHOLDER_OWNER_CHAT_ID = 10_000_000_001


def pytest_addoption(parser):
    parser.addoption(
        "--real", action="store_true", default=False,
        help="parity suite: drive real `claude -p` turns against the instance "
             "named by $ESTATE_PARITY_LIVE_CONFIG. Spends tokens and SENDS "
             "real [PARITY] messages to that instance's owner chat.")


def _real_mode(config):
    """Real mode is on if the flag was passed or the variable is already set.

    Both are honoured so the invocation the parity module's own docstring
    documents keeps working, but they resolve to one answer.
    """
    return bool(config.getoption("--real")) or os.environ.get(REAL_ENV) == "1"


def pytest_configure(config):
    if not _real_mode(config):
        return
    os.environ[REAL_ENV] = "1"
    live = (os.environ.get(LIVE_CONFIG_ENV) or "").strip()
    if not live:
        raise pytest.UsageError(
            f"--real needs ${LIVE_CONFIG_ENV}. Real mode runs its turns against "
            f"a live instance's workdir, permission allowlist and MCP config, "
            f"and there is no default to fall back to. "
            f"`libexec/parity.sh <instance.yaml> --real` sets it from its own "
            f"argument; setting {REAL_ENV}=1 by hand means setting this too.")
    if not Path(live).expanduser().is_file():
        raise pytest.UsageError(
            f"${LIVE_CONFIG_ENV} names no readable instance config: {live}")


@pytest.fixture(scope="session")
def live_config():
    """The instance a --real drill borrows from, or None in fast mode.

    Resolved once per session and only in real mode — fast mode has no business
    reading an instance config it is not running against.
    """
    if os.environ.get(REAL_ENV) != "1":
        return None
    import instance_config
    return instance_config.load(os.environ[LIVE_CONFIG_ENV])


@pytest.fixture(scope="session")
def owner_chat_id(live_config):
    """The chat id under test.

    In real mode this must be the instance's own owner: it is the chat that
    actually receives the drill's messages, and the outbound guard refuses any
    other. Everywhere else it is the placeholder above.
    """
    return live_config.owner_chat_id if live_config else PLACEHOLDER_OWNER_CHAT_ID
