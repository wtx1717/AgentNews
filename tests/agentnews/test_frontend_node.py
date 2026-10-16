from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND_TEST_ROOT = Path(__file__).resolve().parent
FRONTEND_TESTS = ("test_frontend_markdown.mjs", "test_frontend_app.mjs")
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is required for the frontend tests")
@pytest.mark.parametrize("test_file", FRONTEND_TESTS)
def test_frontend_scripts_pass_under_node(test_file: str) -> None:
    """Run the browser scripts against the Node DOM stub.

    The static frontend has no bundler, so these files double as the frontend
    test suite; running them from pytest keeps `uv run pytest` a single gate.
    """

    completed = subprocess.run(
        [NODE, str(FRONTEND_TEST_ROOT / test_file)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=FRONTEND_TEST_ROOT.parent.parent,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
