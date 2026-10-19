"""The results script on a tiny made-up game."""
import os
import sqlite3
import subprocess
import sys

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "results.py")


def test_results_breakdown(tmp_path):
    db = tmp_path / "lb.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE events (ts INTEGER, box TEXT, kind TEXT, level INTEGER, detail TEXT,"
              " pid TEXT, status INTEGER)")
    A, B = "a" * 32, "b" * 32
    c.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?)", [
        (0, "p-1", "reach", 0, "", A, None), (30, "p-1", "cmd", 0, "cat readme", A, 0),
        (60, "p-1", "reach", 1, "", A, None),                            # A: level 0 in 60 s
        (0, "p-2", "reach", 0, "", B, None), (10, "p-2", "cmd", 0, "ls -la", B, 0),
        (20, "p-2", "cmd", 0, "cat nope", B, 1), (25, "p-2", "badpass", 0, "level1", B, None),
        (4000, "p-3", "cmd", 0, "cat readme", B, 0),                     # B came back after a break
        (4020, "p-3", "reach", 1, "", B, None),                          # B: 4020 s wall, active less
        (4100, "p-3", "cmd", 1, "ls", B, 0),
        (9, "p-9", "cmd", 0, "ls", None, 0),                             # no player id: ignored
    ])
    c.commit()
    out = subprocess.run([sys.executable, SCRIPT, str(db)], capture_output=True, text=True, check=True).stdout
    assert "- Players: 2" in out
    assert "| 1 | 2 | 0 |" in out                       # both reached level 1, none lost
    times = out.split("## Time to solve")[1].split("##")[0]
    row0 = next(ln for ln in times.splitlines() if ln.startswith("| 0 | 2 |"))
    assert "1m00s" in row0                               # fastest: A's 60 s
    assert "34m00s" in row0                              # wall-clock median of 60 s and 4020 s
    assert "| 0 | 1 | 1 | `cat` (1x) |" in out          # B's wrong password and failed cat
    assert "`cat` 3" in out and "`ls` 1" in out
    assert "a" * 32 not in out and "nope" not in out     # no ids, no command lines in the output
