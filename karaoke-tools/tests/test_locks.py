"""The shared lock across processes: two processes adding songs at the same
time must not lose each other's rows (each add is a read-modify-write of the
whole songs.csv)."""

import os
import subprocess
import sys

import catalog
import feedback
from conftest import ROOT

WRITER = """
import sys
sys.path.insert(0, {root!r})
import catalog, feedback
tag = sys.argv[1]
for i in range(15):
    r = catalog.catalog_upsert({{"title": f"Song {{tag}} {{i}}", "year": 1990}})
    assert r["success"], r
    feedback._append_relation({{"logged_at": "2026-11-06 10:00:00", "reference_key": "ref",
        "reference_title": "Ref", "song_key": r["song_id"], "song_title": "x", "verdict": "like",
        "request_type": "like", "request_text": tag}})
"""


def test_two_processes_writing_at_once(wiki, tmp_path):
    script = tmp_path / "writer.py"
    script.write_text(WRITER.format(root=str(ROOT)))
    env = dict(os.environ)
    procs = [subprocess.Popen([sys.executable, str(script), tag], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) for tag in ("a", "b")]
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()[-2000:]
    rows = catalog.load_songs()
    assert len(rows) == 30 and len({r["song_id"] for r in rows}) == 30
    assert len(feedback.load_relations()) == 30
    assert [r["song_id"] for r in rows] == sorted(r["song_id"] for r in rows)


def test_lock_is_reentrant_in_one_process(wiki):
    with catalog._lock:
        with catalog._lock:  # e.g. catalog_upsert -> feedback hook -> catalog_upsert
            assert catalog.catalog_upsert({"title": "Inner", "year": 2000})["success"]
