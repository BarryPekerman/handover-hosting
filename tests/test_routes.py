"""The leaderboard's scoring and the play / ownership routes, through real
HTTP requests to the spawner's app. Everything that would touch Incus
(starting a box, reading its login records) is faked; tests/vm covers that.

    pip install pytest aiohttp && pytest tests/
"""
import asyncio
import os
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest
from aiohttp.test_utils import TestClient, TestServer

SPAWNER_DIR = os.path.join(os.path.dirname(__file__), "..", "spawner")
sys.path.insert(0, SPAWNER_DIR)
import auth  # noqa: E402
import common  # noqa: E402
import metrics  # noqa: E402
import spawner as sp  # noqa: E402

PW = "A" * 32
WIN = common.WIN_INDEX


def query(sql):
    """Rows from the test database, connection closed."""
    with closing(sqlite3.connect(common.DB_PATH)) as c:
        return c.execute(sql).fetchall()


@pytest.fixture(autouse=True)
def reset(tmp_path, monkeypatch):
    common.DB_PATH = str(tmp_path / "lb.db")
    common.live_image = ""
    monkeypatch.setattr(auth, "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(sp, "TS_SECRET", "")
    for d in (common.sessions, common.states, common.who, common.pids, common.box_pid,
              common.ip_hits, common.resume_req, metrics.counters, metrics.solve_hist):
        d.clear()


@pytest.fixture
def boxes(monkeypatch):
    """Record box starts and teardowns instead of touching Incus."""
    started, torn = [], []

    def start(sid):
        common.states[sid] = "pending"
        started.append(sid)

    async def teardown(sid, reason=""):
        common.sessions.pop(sid, None)
        torn.append((sid, reason))
    monkeypatch.setattr(sp, "start_provision", start)
    monkeypatch.setattr(sp, "teardown", teardown)
    return started, torn


def live(sid, uid=None, **kw):
    common.sessions[sid] = {"ct": "p-" + sid[:12], "start": 0, "passes": {}, "drop": [],
                            "note": "", "image": "img1", "last": 0, **kw}
    common.who[sid] = {"uid": uid, "display": "@u" + str(uid) if uid else "anonymous"}


def ghid(uid):
    return auth._sign({"uid": uid, "login": "u" + str(uid)})


def call(method, path, cookies=None, data=None, ip="1.1.1.1", headers=None):
    """One request to the spawner's app: (status, headers, body, cookies set)."""
    async def go():
        app = sp.make_app()
        app.on_startup.clear(); app.on_shutdown.clear(); app.on_cleanup.clear()   # no Incus
        async with TestClient(TestServer(app)) as c:
            h = {"CF-Connecting-IP": ip, **(headers or {})}
            if cookies:
                h["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
            r = await c.request(method, path, headers=h, data=data, allow_redirects=False)
            return r.status, r.headers, await r.text(), {k: m.value for k, m in r.cookies.items()}
    return asyncio.run(go())


# ---- scoring: update_progress -> players table -> /leaderboard -------------

@pytest.fixture
def box_reads(monkeypatch):
    """The box's login records and pushed totals, faked."""
    state = {"reaches": {}, "pushed": []}

    async def reaches(ct):
        return dict(state["reaches"])

    async def passes(ct, levels):
        return {n: PW for n in levels}

    async def commands(s):
        return None

    async def push(ct, secs):
        state["pushed"].append(secs)
    monkeypatch.setattr(sp, "read_reaches", reaches)
    monkeypatch.setattr(sp, "read_passes", passes)
    monkeypatch.setattr(sp, "read_commands", commands)
    monkeypatch.setattr(sp, "push_playtime", push)
    return state


def poll(sid, reaches, box_reads):
    box_reads["reaches"] = reaches
    asyncio.run(sp.update_progress(sid, common.sessions[sid]["ct"]))


def players():
    return query("SELECT key, max_level, accumulated, finished, unranked FROM players ORDER BY key")


def climb(n, t0=1000, step=60):
    """Arrivals at levels 0..n, `step` seconds apart."""
    return {i: t0 + step * i for i in range(n + 1)}


def test_each_new_level_adds_its_time_once(box_reads):
    live("s1")
    poll("s1", climb(2), box_reads)
    assert players() == [("anon:s1", 2, 120, 0, 0)]
    poll("s1", climb(2), box_reads)                       # nothing new: nothing added
    assert players() == [("anon:s1", 2, 120, 0, 0)]
    poll("s1", {**climb(2), 3: 1000 + 120 + 500}, box_reads)
    assert players() == [("anon:s1", 3, 620, 0, 0)]
    assert box_reads["pushed"] == [120, 120, 620]         # the victory screen's total, every poll


def test_reaching_dave_finishes_ranked_under_the_account(box_reads):
    live("s1", uid="42")
    poll("s1", climb(WIN), box_reads)
    assert players() == [("42", WIN, 60 * WIN, 1, 0)]


def test_passwords_of_new_levels_go_to_the_browser_save(box_reads):
    live("s1")
    poll("s1", climb(3), box_reads)
    assert set(common.sessions["s1"]["passes"]) == {0, 1, 2, 3}


def test_a_resume_past_what_the_account_played_is_unranked(box_reads):
    live("s1", uid="42", start=5)                         # resumed at 5, never played 0-4 here
    poll("s1", climb(WIN), box_reads)
    assert players()[0][3:] == (1, 1)                     # finished, but unranked


def test_jumping_levels_is_unranked(box_reads):
    live("s1")
    poll("s1", {0: 1000, 1: 1060, WIN: 1100}, box_reads)  # straight to dave with a shared password
    assert players()[0][4] == 1


def test_unranked_is_never_cleared(box_reads):
    live("s1", uid="42", start=5)
    poll("s1", climb(6), box_reads)                       # unranked: resumed past level 0
    common.sessions["s1"]["start"] = 0
    poll("s1", climb(WIN), box_reads)                     # a clean later poll
    assert players()[0][4] == 1


def finished(key, display, secs, unranked=0, done=1):
    sp.save_progress(key, display, WIN if done else 5, secs, done, unranked)


def test_leaderboard_lists_ranked_finishers_fastest_first():
    finished("1", "@slow", 7200)
    finished("2", "@fast", 3725)
    finished("3", "@resumed", 60, unranked=1)
    finished("4", "@playing", 10, done=0)
    status, _, body, _ = call("GET", "/leaderboard")
    assert status == 200
    assert body.index("@fast") < body.index("@slow")
    assert "@resumed" not in body and "@playing" not in body
    assert "1:02:05" in body                              # 3725 s


def test_leaderboard_escapes_names():
    finished("1", "@<script>x</script>", 100)
    assert "<script>x" not in call("GET", "/leaderboard")[2]


def test_empty_leaderboard_says_so():
    assert "nobody's finished yet" in call("GET", "/leaderboard")[2]


# ---- play: who gets a box ----------------------------------------------------

def test_a_get_never_starts_a_box(boxes):
    started, _ = boxes
    status, h, _, _ = call("GET", "/play")
    assert (status, h["Location"], started) == (302, "/", [])


def test_a_get_with_a_live_box_goes_to_its_terminal(boxes):
    live("s1")
    status, h, _, _ = call("GET", "/play", cookies={common.COOKIE: "s1"})
    assert (status, h["Location"]) == (302, "/app/s1")


def test_play_starts_a_box_and_hands_out_its_cookies(boxes):
    started, _ = boxes
    status, _, body, cookies = call("POST", "/play", data={"mode": "fresh"})
    assert status == 200 and started == [cookies[common.COOKIE]]
    assert cookies[common.COOKIE] in body                  # the spinner polls for this box
    assert common.PID_RE.fullmatch(cookies["pid"])


def test_play_with_a_live_box_reuses_it(boxes):
    started, _ = boxes
    live("s1")
    status, h, _, _ = call("POST", "/play", cookies={common.COOKIE: "s1"}, data={"mode": "fresh"})
    assert (status, h["Location"], started) == (302, "/app/s1", [])


def test_a_signed_in_player_gets_their_box_on_any_device(boxes):
    started, _ = boxes
    live("s1", uid="42")
    status, h, _, _ = call("POST", "/play", cookies={"ghid": ghid("42")}, data={"mode": "fresh"})
    assert (status, h["Location"], started) == (302, "/app/s1", [])


def test_play_is_throttled_per_ip(boxes):
    started, _ = boxes
    for _ in range(common.RATE_MAX):
        assert call("POST", "/play", data={"mode": "fresh"}, ip="9.9.9.9")[0] == 200
    assert call("POST", "/play", data={"mode": "fresh"}, ip="9.9.9.9")[0] == 429
    assert len(started) == common.RATE_MAX
    assert call("POST", "/play", data={"mode": "fresh"}, ip="8.8.8.8")[0] == 200


def test_play_fails_the_bot_check_before_spending_a_box(boxes, monkeypatch):
    started, _ = boxes
    monkeypatch.setattr(sp, "TS_SECRET", "ts-secret")

    async def no(token, ip):
        return False
    monkeypatch.setattr(sp, "verify_turnstile", no)
    assert call("POST", "/play", data={"mode": "fresh"})[0] == 403
    assert started == [] and common.ip_hits == {}         # a failed check doesn't use up the IP's quota


def test_play_resume_hands_the_save_to_the_new_box(boxes):
    status, _, _, cookies = call("POST", "/play", data={"mode": "resume", "save": '{"levels":{"7":"' + PW + '"}}'})
    saved, server_level, _ = common.resume_req[cookies[common.COOKIE]]
    assert status == 200 and saved == {7: PW} and server_level == 0


def test_play_on_an_outdated_box_upgrades_it_at_the_level_reached(boxes):
    started, torn = boxes
    live("s1", passes={4: PW, 6: PW})
    common.live_image = "img2"                             # an image deploy happened
    status, _, _, _ = call("POST", "/play", cookies={common.COOKIE: "s1"}, data={"mode": "fresh"})
    assert status == 200 and torn == [("s1", "upgrade")] and started == ["s1"]
    assert common.resume_req["s1"][1] == 6


# ---- whose terminal and save -------------------------------------------------

def test_terminal_opens_only_for_its_owner():
    live("s1")
    assert call("GET", "/app/s1", cookies={common.COOKIE: "s1"})[0] == 200
    assert call("GET", "/app/s1")[1]["Location"] == "/"                          # no cookie
    assert call("GET", "/app/s1", cookies={common.COOKIE: "s2"})[1]["Location"] == "/"
    assert call("GET", "/app/nope", cookies={common.COOKIE: "nope"})[1]["Location"] == "/"


def test_another_account_cannot_open_the_terminal_even_with_the_cookie():
    live("s1", uid="42")
    status, h, _, _ = call("GET", "/app/s1", cookies={common.COOKIE: "s1", "ghid": ghid("99")})
    assert (status, h["Location"]) == (302, "/")


def test_a_terminal_socket_opened_from_another_sites_page_is_refused():
    live("s1", port=1)          # no ttyd: a refused socket never gets that far
    ws = {"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
          "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", "Origin": "https://evil.barrypekerman.com"}
    status, _, body, _ = call("GET", "/t/s1/ws", cookies={common.COOKIE: "s1"}, headers=ws)
    assert (status, body) == (403, "cross-site terminal")


def test_the_spawner_will_not_run_without_the_bot_check():
    env = {k: v for k, v in os.environ.items() if k not in ("TURNSTILE_SECRET", "ALLOW_NO_TURNSTILE")}
    r = subprocess.run([sys.executable, os.path.join(SPAWNER_DIR, "spawner.py")], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "TURNSTILE_SECRET is not set" in r.stderr


def test_an_outdated_terminal_goes_to_the_landing_page():
    live("s1")
    common.live_image = "img2"
    assert call("GET", "/app/s1", cookies={common.COOKIE: "s1"})[1]["Location"] == "/"


def test_save_is_served_only_to_the_owner_and_never_cached():
    live("s1", passes={3: PW})
    status, h, body, _ = call("GET", "/save/s1", cookies={common.COOKIE: "s1"})
    assert status == 200 and PW in body and h["Cache-Control"] == "no-store"
    assert call("GET", "/save/s1")[0] == 404
    assert call("GET", "/save/s1", cookies={common.COOKIE: "s9"})[0] == 404


@pytest.mark.parametrize("setup,want", [
    (lambda: live("s1"), "ready"),
    (lambda: common.states.update(s1="full"), "full"),
    (lambda: None, "pending"),
])
def test_ready_reports_the_box_state(setup, want):
    setup()
    assert f'"state": "{want}"' in call("GET", "/ready?sid=s1")[2]
