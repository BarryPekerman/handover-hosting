"""Standby boxes (WARM_SPARES): boxes kept started so a player gets one at once.
Incus is faked; `make e2e` hands out a standby box against the game VM.

    pip install pytest aiohttp && pytest tests/
"""
import asyncio
import os
import sys
from contextlib import closing

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "spawner"))
import common  # noqa: E402
import spawner as sp  # noqa: E402

PW = "A" * 32
SID = "s" * 32


@pytest.fixture(autouse=True)
def reset(tmp_path, monkeypatch):
    common.DB_PATH = str(tmp_path / "lb.db")
    common.live_image = "img1"
    for d in (common.sessions, common.states, common.spares, common.in_flight, common.resume_req,
              common.box_pid, common.pids, common.locks, common.background):
        d.clear()
    monkeypatch.setattr(sp, "WARM_SPARES", 1)
    monkeypatch.setattr(sp, "start_ttyd", nothing)
    monkeypatch.setattr(sp, "save_state", lambda: None)


async def nothing(*a, **k):
    return None


@pytest.fixture
def incus(monkeypatch):
    """A fake incus: every box boots, images come from `images` (default img1),
    boxes in `dead` don't answer, `listed` is what `incus list` shows."""
    fake = {"calls": [], "images": {}, "dead": set(), "listed": ""}

    async def run(*args):
        fake["calls"].append(args)
        if "is-system-running" in args:
            return 0, "running"
        if args[:3] == ("incus", "config", "get"):
            return 0, fake["images"].get(args[3], "img1")
        if args[:2] == ("incus", "exec") and args[-1] == "true":
            return (1 if args[2] in fake["dead"] else 0), ""
        if args[:2] == ("incus", "list"):
            return 0, fake["listed"]
        return 0, ""
    monkeypatch.setattr(sp, "run", run)
    fake["created"] = lambda: [a[a.index("launch") + 2] for a in fake["calls"] if "launch" in a]
    fake["deleted"] = lambda: [a[-1] for a in fake["calls"] if a[:2] == ("incus", "delete")]
    return fake


def play(sid=SID):
    """Provision like /play does, then let background work (refills) finish."""
    async def go():
        await sp.provision_bg(sid)
        while common.background:
            await asyncio.gather(*list(common.background))
    asyncio.run(go())
    return common.sessions.get(sid)


def events():
    with closing(common.db()) as c:                     # db() creates the tables if no event came yet
        return c.execute("SELECT box, kind FROM events ORDER BY rowid").fetchall()


def test_a_player_gets_the_ready_standby_box_and_a_replacement_starts(incus):
    common.spares["w-ready"] = "img1"
    s = play()
    assert s["ct"] == "w-ready"                         # no create to wait for
    new = incus["created"]()
    assert len(new) == 1 and new[0].startswith("w-")    # only the replacement was created
    assert set(common.spares) == {new[0]} and not common.in_flight


def test_without_a_standby_box_the_player_gets_a_new_one(incus):
    s = play()
    assert s["ct"] == "p-" + SID[:12]
    assert incus["created"]()[0] == "p-" + SID[:12]


def test_a_resume_is_checked_against_the_standby_box(incus, monkeypatch):
    common.spares["w-ready"] = "img1"
    common.resume_req[SID] = ({7: PW, 9: "forged"}, 0, "")
    asked = []

    async def read_passes(ct, levels):
        asked.append(ct)
        return {7: PW, 9: "B" * 32}
    monkeypatch.setattr(sp, "read_passes", read_passes)
    s = play()
    assert asked == ["w-ready"]
    assert (s["start"], s["drop"]) == (7, [9])


def test_a_standby_box_on_the_old_image_is_never_handed_out(incus):
    common.spares["w-old"] = "img0"                     # built before an image deploy
    s = play()
    assert s["ct"] == "p-" + SID[:12]
    assert "w-old" in incus["deleted"]()


def test_a_standby_box_that_died_is_skipped(incus):
    common.spares["w-dead"] = "img1"
    incus["dead"].add("w-dead")
    assert play()["ct"] == "p-" + SID[:12]
    assert "w-dead" in incus["deleted"]()


def test_an_image_deploy_deletes_old_standby_boxes(incus):
    common.spares.update({"w-old": "img0", "w-new": "img1"})
    asyncio.run(sp.drop_outdated_spares())
    assert set(common.spares) == {"w-new"} and incus["deleted"]() == ["w-old"]


@pytest.mark.parametrize("players,ready,want", [
    (0, 0, 3),      # empty server: build all three
    (0, 2, 1),      # two ready: one more
    (8, 0, 2),      # 8 players + 2 standby = the 10 boxes allowed
    (10, 0, 0),     # full: no standby boxes at all
])
def test_standby_boxes_never_take_the_box_count_past_max_players(monkeypatch, players, ready, want):
    monkeypatch.setattr(sp, "WARM_SPARES", 3)
    monkeypatch.setattr(sp, "MAX_PLAYERS", 10)
    for i in range(players):
        common.sessions[f"s{i}"] = {"ct": f"p-{i}"}
    for i in range(ready):
        common.spares[f"w-r{i}"] = "img1"
    built = []
    monkeypatch.setattr(sp, "build_spare", lambda ct: built.append(ct) or nothing())

    async def go():
        sp.fill_spares()
        await asyncio.gather(*list(common.background))
    asyncio.run(go())
    assert len(built) == want


def test_the_sweep_keeps_standby_boxes_and_deletes_strays(incus):
    common.sessions["s1"] = {"ct": "w-handed"}           # a standby box now in use
    common.spares["w-ready"] = "img1"
    common.in_flight.add("w-building")
    incus["listed"] = "w-handed\nw-ready\nw-building\nw-stray\np-stray\nsomething-else\n"
    asyncio.run(sp.sweep_orphans())
    assert sorted(incus["deleted"]()) == ["p-stray", "w-stray"]


def test_a_standby_box_is_not_a_player_until_handed_out(incus):
    asyncio.run(sp.build_spare("w-new"))
    assert common.spares == {"w-new": "img1"} and not common.in_flight
    assert events() == [] and "w-new" not in common.box_pid
    common.pids[SID] = "c" * 32
    play()
    assert events()[0] == ("w-new", "start")             # its first event is the player's start
    assert common.box_pid["w-new"] == "c" * 32
