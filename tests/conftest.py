from __future__ import annotations

import atexit
import sys

import pytest

from ktxgo import runlog


@pytest.fixture(autouse=True)
def _isolated_run_log(tmp_path, monkeypatch):
    """Send run logs to a temp dir and undo the stdout/stderr swap afterwards."""
    monkeypatch.setattr(runlog, "LOG_DIR", tmp_path / "logs")
    stdout, stderr = sys.stdout, sys.stderr
    yield
    sys.stdout, sys.stderr = stdout, stderr
    if runlog._writer is not None:
        runlog._writer.close()
    runlog._writer = None
    runlog._tees.clear()
    atexit.unregister(runlog._finish)
