"""Unit tests for the spawner's pure logic: saves, scoring, box ownership.

    pip install pytest aiohttp && pytest tests/

End-to-end behaviour (containers, ttyd, tmux) needs a game VM; see
tests/README.md.
"""
import asyncio
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "spawner"))
import spawner as sp  # noqa: E402

PW = "A" * 32


class Req:
    """Just enough of an aiohttp request for identity()/resolve_sid()/owns()."""

    def __init__(self, hid=None, uid=None):
        self.cookies = {}
        if hid:
            self.cookies[sp.COOKIE] = hid
        if uid:
            self.cookies["ghid"] = sp._sign({"uid": uid, "login": "u" + str(uid)})


@pytest.fixture(autouse=True)
def tmp_db(tmp_path):
    sp.DB_PATH = str(tmp_path / "lb.db")


def setup_function():
    sp.SESSION_SECRET = "test-secret"
    sp.sessions.clear()
    sp.who.clear()
    sp.states.clear()
    sp.live_image = ""
    sp.counters.clear()
    sp.solve_hist.clear()


def box(sid, uid=None, **kw):
    sp.sessions[sid] = {"start": 0, "passes": {}, "image": "img1", **kw}
    sp.who[sid] = {"uid": uid, "display": "x"}


# ---- browser saves -------------------------------------------------------

def test_parse_save_keeps_only_valid_entries():
    raw = json.dumps({"levels": {"3": PW, "20": "b" * 10, "21": PW, "0": PW,
                                 "x": PW, "5": "bad pass!", "7": 42}})
    assert sp.parse_save(raw) == {3: PW, 20: "b" * 10}


def test_parse_save_survives_garbage():
    for raw in ["", "not json {", "null", "[1,2]", '{"levels": []}',
                '{"levels": "x"}', "{" + "x" * 9000 + "}"]:
        assert sp.parse_save(raw) == {}


def test_choose_start_uses_highest_matching_password(monkeypatch):
    async def real(ct, levels):
        return {7: PW, 9: "B" * 32}
    monkeypatch.setattr(sp, "read_passes", real)
    saved = {7: PW, 9: "wrong", 3: PW}           # 9 forged, 3 stale
    start, drop, note = asyncio.run(sp.choose_start("ct", (saved, 0, "")))
    assert start == 7
    assert drop == [3, 9]
    assert note == "resumed at level 7"


def test_choose_start_nothing_matches(monkeypatch):
    async def real(ct, levels):
        return {}
    monkeypatch.setattr(sp, "read_passes", real)
    start, drop, note = asyncio.run(sp.choose_start("ct", ({4: PW}, 0, "")))
    assert (start, drop) == (0, [4])
    assert "doesn't match" in note


def test_choose_start_server_level_and_override(monkeypatch):
    async def real(ct, levels):
        return {}
    monkeypatch.setattr(sp, "read_passes", real)
    assert asyncio.run(sp.choose_start("ct", ({}, 5, "updated"))) == (5, [], "updated")
    assert asyncio.run(sp.choose_start("ct", None)) == (0, [], "")


# ---- scoring -------------------------------------------------------------

def test_solve_time_counts_consecutive_levels_once():
    reaches = {0: 100, 1: 160, 2: 400, 3: 420}
    assert sp.solve_time(reaches, 0, 3) == 60 + 240 + 20
    assert sp.solve_time(reaches, 2, 3) == 20          # only the new level


def test_solve_time_ignores_gaps_and_clock_skew():
    assert sp.solve_time({0: 100, 2: 300}, 0, 2) == 0
    assert sp.solve_time({0: 100, 1: 50}, 0, 1) == 0


def test_skipped_levels():
    assert not sp.skipped_levels({0: 1, 1: 2, 2: 3}, 0, 2)
    assert sp.skipped_levels({0: 1, sp.WIN_INDEX: 2}, 0, sp.WIN_INDEX)   # straight to dave
    assert not sp.skipped_levels({5: 1, 6: 2}, 5, 6)                    # returning player
    assert sp.skipped_levels({6: 2}, 5, 6)


# ---- whose box -----------------------------------------------------------

def test_account_box_follows_the_account():
    box("s1", uid="42")
    assert sp.resolve_sid(Req(uid="42")) == "s1"                 # new device, no cookie
    assert sp.owns(Req(uid="42"), "s1")


def test_other_account_never_gets_the_box():
    box("s1", uid="42")
    assert sp.resolve_sid(Req(hid="s1", uid="99")) is None       # stale cookie, other account
    assert not sp.owns(Req(hid="s1", uid="99"), "s1")


def test_anonymous_box_by_cookie_and_claimed_on_sign_in():
    box("s2")
    assert sp.owns(Req(hid="s2"), "s2")
    assert not sp.owns(Req(), "s2")
    assert sp.resolve_sid(Req(hid="s2", uid="7")) == "s2"        # sign-in claims it
    sp.remember("s2", "7", "@u7")
    assert sp.who["s2"]["uid"] == "7"
    sp.remember("s2", None, "anonymous")                        # signing out never downgrades
    assert sp.who["s2"]["uid"] == "7"


def test_forged_identity_cookie_is_ignored():
    box("s1", uid="42")
    r = Req()
    r.cookies["ghid"] = sp._sign({"uid": "42", "login": "x"})[:-4] + "0000"
    assert sp.identity(r) == (None, "anonymous")
    assert not sp.owns(r, "s1")


# ---- image updates -------------------------------------------------------

def test_outdated_and_reached_level():
    box("s1", start=3, passes={4: PW, 5: PW})
    assert not sp.outdated(sp.sessions["s1"])                  # live image unknown yet
    sp.live_image = "img1"
    assert not sp.outdated(sp.sessions["s1"])
    sp.live_image = "img2"
    assert sp.outdated(sp.sessions["s1"])
    assert sp.reached_level(sp.sessions["s1"]) == 5


# ---- metrics -------------------------------------------------------------

def test_progress_counted_once_per_box():
    box("s1")
    s = sp.sessions["s1"]
    sp.count_progress(s, {0: 100, 1: 160})
    sp.count_progress(s, {0: 100, 1: 160, 2: 400})          # next poll: only level 2 is new
    reached = {dict(k[1])["level"]: v for k, v in sp.counters.items()
               if k[0] == "handover_level_reached_total"}
    assert reached == {"0": 1, "1": 1, "2": 1}
    assert sp.solve_hist[0][-2:] == [1, 60] and sp.solve_hist[1][-2:] == [1, 240]
    text = sp.render_metrics()
    assert 'handover_level_solve_seconds_bucket{level="1",le="300"} 1' in text
    assert 'handover_player_level{level="2"} 1' in text


def test_resumed_box_does_not_time_levels_before_its_start():
    box("s1", start=5)
    sp.count_progress(sp.sessions["s1"], {4: 10, 5: 20, 6: 80})
    assert list(sp.solve_hist) == [5]


def test_long_breaks_stay_out_of_the_histogram_but_in_events():
    box("s1", ct="p-1")
    sp.count_progress(sp.sessions["s1"], {0: 0, 1: sp.SOLVE_CAP + 1, 2: sp.SOLVE_CAP + 61})
    assert list(sp.solve_hist) == [1]                                   # level 0 was capped
    assert sp.counters[("handover_level_solve_capped_total", (("level", "0"),))] == 1
    rows = sqlite3.connect(sp.DB_PATH).execute("SELECT ts, box, kind, level FROM events").fetchall()
    assert rows == [(0, "p-1", "reach", 0), (sp.SOLVE_CAP + 1, "p-1", "reach", 1),
                    (sp.SOLVE_CAP + 61, "p-1", "reach", 2)]


def test_commands_from_the_journal():
    out = "\n".join([
        "U 1001 level3", "U 1022 dave",
        json.dumps({"__CURSOR": "c1", "_UID": "1001", "MESSAGE": "find", "__REALTIME_TIMESTAMP": "5000000"}),
        json.dumps({"__CURSOR": "c2", "_UID": "1001", "MESSAGE": "rm -rf /"}),     # not a name
        json.dumps({"__CURSOR": "c3", "_UID": "0", "MESSAGE": "ls"}),              # not a player
        json.dumps({"__CURSOR": "c4", "_UID": "1022", "MESSAGE": "frobnicate", "__REALTIME_TIMESTAMP": "7000000"}),
        "garbage",
    ])
    found, cursor = sp.parse_commands(out)
    assert found == [(5, 3, "find"), (7, sp.WIN_INDEX, "frobnicate")]
    assert cursor == "c4"
    box("s1", ct="p-1")
    sp.count_commands(sp.sessions["s1"], found)
    assert 'handover_commands_total{cmd="find",level="3"} 1' in sp.render_metrics()
    assert 'handover_commands_total{cmd="other",level="21"} 1' in sp.render_metrics()
    kinds = sqlite3.connect(sp.DB_PATH).execute("SELECT kind, detail FROM events").fetchall()
    assert kinds == [("cmd", "find"), ("cmd", "frobnicate")]
