"""Unit tests for metrics.py and the shared database helpers in common.py.

    pip install pytest aiohttp && pytest tests/
"""
import asyncio
import json
import os
import sqlite3
import sys

import pytest
from aiohttp.test_utils import make_mocked_request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "spawner"))
import common  # noqa: E402
import metrics  # noqa: E402

WIN = common.WIN_INDEX


@pytest.fixture(autouse=True)
def reset(tmp_path):
    common.DB_PATH = str(tmp_path / "lb.db")
    common.live_image = ""
    for d in (common.sessions, common.states, common.box_pid, metrics.counters, metrics.solve_hist):
        d.clear()


def counter(name, **labels):
    return metrics.counters.get((name, tuple(sorted((k, str(v)) for k, v in labels.items()))), 0)


def all_levels(start=0):
    return {i: 1000 + 60 * i for i in range(start, WIN + 1)}


def test_a_full_run_from_level_0_finishes_ranked():
    s = {"ct": "p-1", "start": 0}
    metrics.count_progress(s, all_levels())
    assert counter("handover_finishes_total", ranked="true") == 1
    rows = sqlite3.connect(common.DB_PATH).execute(
        "SELECT kind, level, detail FROM events WHERE kind='finish'").fetchall()
    assert rows == [("finish", WIN, "ranked")]


def test_a_resumed_run_finishes_unranked():
    s = {"ct": "p-2", "start": 5}
    metrics.count_progress(s, all_levels(start=5))
    assert counter("handover_finishes_total", ranked="false") == 1
    assert counter("handover_finishes_total", ranked="true") == 0
    assert 3 not in metrics.solve_hist           # levels before the resume aren't timed


def test_finishing_counts_once_however_often_it_is_polled():
    s = {"ct": "p-3", "start": 0}
    for _ in range(3):
        metrics.count_progress(s, all_levels())
    assert counter("handover_finishes_total", ranked="true") == 1


def test_metrics_endpoint_serves_prometheus_text():
    common.live_image = "abcdef0123456789"
    common.sessions["s1"] = {"start": 4}
    common.states["s2"] = "pending"
    metrics.init_counters()
    r = asyncio.run(metrics.h_metrics(None))
    assert r.content_type == "text/plain"
    assert "handover_players 1\n" in r.text and "handover_starting 1\n" in r.text
    assert 'handover_info{image="abcdef012345"} 1' in r.text
    assert 'handover_player_level{level="4"} 1' in r.text
    assert 'handover_finishes_total{ranked="true"} 0' in r.text


def test_a_broken_database_never_breaks_play():
    common.DB_PATH = "/nonexistent/dir/lb.db"
    metrics.init_counters()
    common.events([(1, "p-1", "reach", 0, "")])          # logged, not raised
    assert counter("handover_errors_total") == 1


def test_events_carry_the_boxs_player_id_and_status():
    common.box_pid["p-1"] = "f" * 32
    common.events([(1, "p-1", "cmd", 2, "ls", 0), (2, "p-9", "reach", 3, "")])
    rows = sqlite3.connect(common.DB_PATH).execute(
        "SELECT box, kind, status, pid FROM events ORDER BY ts").fetchall()
    assert rows == [("p-1", "cmd", 0, "f" * 32), ("p-9", "reach", None, None)]


def test_player_endpoint_serves_only_well_formed_ids():
    c = common.db()
    c.execute("INSERT INTO events(ts, box, kind, level, detail, pid) VALUES(1, 'p-1', 'reach', 0, '', 'x')")
    c.execute("INSERT INTO events(ts, box, kind, level, detail, pid) VALUES(2, 'p-2', 'reach', 0, '', ?)",
              ("c" * 32,))
    c.commit()

    def get(pid):
        r = asyncio.run(metrics.h_data_player(make_mocked_request("GET", f"/data/player?pid={pid}")))
        return json.loads(r.text)
    assert get("x") == []
    assert [e["box"] for e in get("c" * 32)] == ["p-2"]
