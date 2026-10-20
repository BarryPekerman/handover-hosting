"""Unit tests for the spawner's pure logic: saves, scoring, box ownership.

    pip install pytest aiohttp && pytest tests/

End-to-end behaviour (containers, ttyd, tmux) needs a game VM; see
tests/README.md.
"""
import asyncio
import json
import os
import re
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
    sp.pids.clear()
    sp.box_pid.clear()


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
        json.dumps({"__CURSOR": "c1", "_UID": "1001", "SYSLOG_IDENTIFIER": "handover-cmd",
                    "MESSAGE": "0 find / -perm -4000 2>/dev/null", "__REALTIME_TIMESTAMP": "5000000"}),
        json.dumps({"__CURSOR": "c2", "_UID": "1001", "SYSLOG_IDENTIFIER": "handover-cmd",
                    "MESSAGE": "find", "__REALTIME_TIMESTAMP": "6000000"}),          # old image: name only
        json.dumps({"__CURSOR": "c3", "_UID": "0", "SYSLOG_IDENTIFIER": "handover-cmd",
                    "MESSAGE": "0 ls"}),                                             # not a player
        json.dumps({"__CURSOR": "c4", "_UID": "1022", "SYSLOG_IDENTIFIER": "handover-cmd",
                    "MESSAGE": "127 frobnicate --x", "__REALTIME_TIMESTAMP": "7000000"}),
        json.dumps({"__CURSOR": "c5", "_UID": "1001", "SYSLOG_IDENTIFIER": "su", "__REALTIME_TIMESTAMP": "8000000",
                    "MESSAGE": "pam_unix(su:auth): authentication failure; logname= uid=1001 euid=0 "
                               "tty=/dev/pts/1 ruser=level3 rhost=  user=level4"}),
        json.dumps({"__CURSOR": "c6", "_UID": "0", "SYSLOG_IDENTIFIER": "sshd", "__REALTIME_TIMESTAMP": "9000000",
                    "MESSAGE": "Failed password for level4 from ::1 port 1 ssh2"}),  # same attempt: pam line counts
        "garbage",
    ])
    found, cursor = sp.parse_commands(out)
    assert found == [(5, 3, "cmd", "find / -perm -4000 2>/dev/null", 0), (6, 3, "cmd", "find", None),
                     (7, sp.WIN_INDEX, "cmd", "frobnicate --x", 127), (8, 3, "badpass", "level4", None)]
    assert cursor == "c6"
    box("s1", ct="p-1")
    sp.box_pid["p-1"] = "a" * 32
    sp.count_commands(sp.sessions["s1"], found)
    text = sp.render_metrics()
    assert 'handover_commands_total{cmd="find",level="3"} 2' in text
    assert 'handover_commands_total{cmd="other",level="21"} 1' in text
    assert 'handover_wrong_passwords_total{level="3"} 1' in text
    rows = sqlite3.connect(sp.DB_PATH).execute("SELECT kind, detail, status, pid FROM events").fetchall()
    assert rows[0] == ("cmd", "find / -perm -4000 2>/dev/null", 0, "a" * 32)
    assert rows[-1] == ("badpass", "level4", None, "a" * 32)


def test_player_id_follows_cookie_then_account():
    r = Req()
    r.cookies["pid"] = "b" * 32
    assert sp.player_id(r, None) == "b" * 32                    # same browser, same player
    r.cookies["pid"] = "not-a-pid"
    assert sp.PID_RE.fullmatch(sp.player_id(r, None))          # junk cookie: a fresh id
    r.cookies["pid"] = "c" * 32
    assert sp.player_id(r, "42") == "c" * 32                    # first sign-in adopts this browser's id
    r2 = Req()
    r2.cookies["pid"] = "d" * 32
    assert sp.player_id(r2, "42") == "c" * 32                   # another device gets the account's id


def test_cmd_name_for_metrics():
    assert sp.cmd_name("find / -name x") == "find"
    assert sp.cmd_name("/usr/bin/grep -r x") == "grep"
    assert sp.cmd_name("./run.sh") == "other"
    assert sp.cmd_name("") == "other"


def test_quiet_game_reads_zero_not_missing():
    sp.init_counters()
    text = sp.render_metrics()
    assert 'handover_level_reached_total{level="0"} 0' in text
    assert f'handover_level_reached_total{{level="{sp.WIN_INDEX}"}} 0' in text
    assert 'handover_box_starts_total{result="error"} 0' in text
    assert "handover_errors_total 0" in text
    sp.count_progress({"ct": "p-1"}, {0: 100})                 # init never resets real counts
    sp.init_counters()
    assert 'handover_level_reached_total{level="0"} 1' in sp.render_metrics()


def test_logged_errors_are_counted():
    sp.init_counters()
    sp.log.error("boom")
    try:
        raise RuntimeError("x")
    except RuntimeError:
        sp.log.exception("provision failed")
    sp.log.warning("not an error")
    assert "handover_errors_total 2" in sp.render_metrics()


def test_share_preview_and_icon_on_pages():
    for page in (sp.LANDING, sp.STORY, sp.LEADERBOARD):
        assert 'property=og:image content="https://play.barrypekerman.com/og.png"' in page
        assert "twitter:card" in page
    for page in (sp.SPAWNING, sp.APP):
        assert "rel=icon" in page and "og:image" not in page


def test_pages_load_from_files_with_placeholders_filled():
    for page in (sp.LANDING, sp.STORY, sp.LEADERBOARD, sp.SPAWNING, sp.APP):
        assert page.startswith("<!doctype html>") and page.endswith("</html>")
        assert not re.search(r"__(ICON|SHARE|PUBLIC_URL)__", page)
    assert "<h2 id=record>What we record</h2>" in sp.STORY           # the data notice
    assert "function loadSave" in sp.SAVE_JS


def test_static_images_are_served():
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        async with TestClient(TestServer(sp.make_app())) as c:
            r = await c.get("/og.png")
            assert r.status == 200 and r.headers["Content-Type"] == "image/png"
            assert (await r.read())[:8] == b"\x89PNG\r\n\x1a\n"
            assert (await c.get("/favicon.ico")).status == 200
    # make_app's startup hooks touch Incus; serve the routes without them
    orig = sp.make_app

    def bare():
        app = orig()
        app.on_startup.clear(); app.on_shutdown.clear(); app.on_cleanup.clear()
        return app
    sp.make_app = bare
    try:
        asyncio.run(go())
    finally:
        sp.make_app = orig


def test_player_data_endpoints():
    from aiohttp.test_utils import TestClient, TestServer
    A = "a" * 32
    sp.db().close()
    c = sqlite3.connect(sp.DB_PATH)
    c.executemany("INSERT INTO events(ts,box,kind,level,detail,status,pid) VALUES(?,?,?,?,?,?,?)", [
        (100, "p-1", "reach", 0, "", None, A), (130, "p-1", "cmd", 0, "cat readme", 0, A),
        (140, "p-1", "badpass", 0, "level1", None, A), (160, "p-1", "reach", 1, "", None, A),
        (170, "p-2", "cmd", 0, "ls", 0, None)])                       # no player id: left out
    c.commit()

    async def go():
        async with TestClient(TestServer(sp.make_metrics_app())) as cl:
            players = await (await cl.get("/data/players")).json()
            assert len(players) == 1 and players[0]["player"] == "aaaaaaaa"
            assert (players[0]["level"], players[0]["commands"], players[0]["wrong_passwords"]) == (1, 1, 1)
            levels = await (await cl.get("/data/levels")).json()
            assert levels[0]["solved"] == 1 and levels[0]["median_active_s"] == 60
            assert levels[0]["top_commands"] == "cat 1"
            tl = await (await cl.get("/data/player", params={"pid": A})).json()
            assert [r["kind"] for r in tl] == ["reach", "cmd", "badpass", "reach"]
            assert await (await cl.get("/data/player", params={"pid": "x' OR 1"})).json() == []
            assert (await cl.get("/data/players")).status == 200
    asyncio.run(go())
