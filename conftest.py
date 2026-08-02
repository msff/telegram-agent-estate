"""Put `bin/` on sys.path for the test suite.

The plugin's executables live in `bin/` and are run directly (launchd invokes
them by absolute path), so there is no package to install and no `src` layout to
lean on. Tests import them as plain modules.

Deliberately NOT setting any state-directory environment variable here. Every
test that touches state must redirect it explicitly and visibly, because a
suite-wide default is exactly how a test run reaches production: on 2026-07-31
the reading suite pruned the LIVE handoff file, and the reason was a path that
looked redirected but resolved through a module constant instead. A redirect you
can see in the test body is one you can check.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "bin"))
