"""Test suite for local-agent-manager.

Manager state (journals, locks, plans, receipts) normally lives in the
checkout's ``.agent-manager-state``. Tests must never write there, so the
package points every test, and every child process it starts, at one
temporary state directory. Run the suite as a package
(``python -m unittest discover -s tests -t .``) so this module loads first.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_INHERITED = os.environ.get("LAM_TEST_STATE_DIR")
if _INHERITED:
    # A child process started by a test shares its parent's test state.
    _STATE = Path(_INHERITED)
else:
    _STATE = Path(tempfile.mkdtemp(prefix="lam-test-state-")).resolve()
    os.environ["LAM_TEST_STATE_DIR"] = str(_STATE)
    atexit.register(shutil.rmtree, _STATE, ignore_errors=True)
os.environ["LAM_STATE_DIR"] = str(_STATE)


def _isolated_state_base() -> Path:
    # Tests that replace the whole environment still stay in the test state.
    configured = os.environ.get("LAM_STATE_DIR")
    return Path(configured).absolute() if configured else _STATE


from local_agent_manager import state_paths as _state_paths  # noqa: E402

_state_paths.real_manager_state_base = _state_paths.manager_state_base
_state_paths.manager_state_base = _isolated_state_base
