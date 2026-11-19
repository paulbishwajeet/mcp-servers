"""Runs the front end's player tests (tests/js, Node's built-in runner) so
pytest covers the in-app player's fallback logic too."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _node() -> str | None:
    for cand in (os.environ.get("NODE"), shutil.which("node"), str(Path.home() / ".local/bin/node")):
        if cand and Path(cand).is_file():
            return cand
    return None


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_player_fallback_logic():
    r = subprocess.run([_node(), "--test", str(ROOT / "tests/js/player.test.js")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
