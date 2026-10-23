#!/usr/bin/env python3
"""The Handover: gameplay breakdown from the spawner's events table.

    sudo python3 scripts/results.py /var/lib/handover/handover-leaderboard.db > results.md

Run it on the host (next to spawner/gamedata.py, which deploy.yml copies to /opt/handover),
or in a checkout on a copy from the nightly backup (docs/ops-notes.md). Prints
Markdown: players, how far they got, time per level (wall clock and active),
wrong passwords and the commands used on each level. Only totals and averages:
no player ids, no full command lines, so the output is safe to publish. The
numbers come from spawner/gamedata.py, the same code behind the live Grafana
players dashboard.
"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "spawner"), "/opt/handover"]
import gamedata  # noqa: E402


def fmt(sec):
    if sec is None:
        return "-"
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


def main(path):
    rows = gamedata.load(sqlite3.connect(path))
    if not rows:
        print("No events with a player id yet.")
        return
    g = gamedata.Game(rows)
    players, levels = g.players(), g.levels()
    won = sum(1 for p in players if p["won"])
    print("# The Handover: results\n")
    print(f"- Players: {len(players)}")
    print(f"- Won (reached Dave): {won} ({100 * won / len(players):.0f}%)")
    print(f"- Commands typed: {sum(p['commands'] for p in players)}")
    print(f"- Wrong passwords: {sum(p['wrong_passwords'] for p in players)}")
    t = gamedata.stats(g.whole_game_active())
    print(f"- Whole game, active time (players who started at level 0 and won): "
          f"median {fmt(t['median'])}, fastest {fmt(t['fastest'])}, slowest {fmt(t['slowest'])}\n")

    print("## How far players got\n")
    print("| Level | Players reached | Lost since previous |")
    print("| ---: | ---: | ---: |")
    for r in levels:
        print(f"| {r['label']} | {r['reached']} | {'' if r['lost'] is None else r['lost']} |")

    print("\n## Time to solve each level\n")
    print(f"Active time leaves out breaks of more than {gamedata.GAP // 60} minutes.\n")
    print("| Level | Solved by | Median active | Mean active | Fastest | Slowest active | Median wall clock |")
    print("| ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in levels[:-1]:
        print(f"| {r['level']} | {r['solved']} | {fmt(r['median_active_s'])} | {fmt(r['mean_active_s'])} "
              f"| {fmt(r['fastest_s'])} | {fmt(r['slowest_active_s'])} | {fmt(r['median_wall_s'])} |")

    print("\n## Where players struggled\n")
    print("| Level | Wrong passwords | Players who got one wrong | Most failed command |")
    print("| ---: | ---: | ---: | --- |")
    for r in levels:
        if r["wrong_passwords"] or r["most_failed"]:
            mf = r["most_failed"]
            mf = f"`{mf.split(' (')[0]}` ({mf.split(' (')[1]}" if mf else "-"
            print(f"| {r['level']} | {r['wrong_passwords']} | {r['players_wrong_password']} | {mf} |")

    print("\n## Commands used on each level (top 5)\n")
    print("| Level | Players typing | Commands |")
    print("| ---: | ---: | --- |")
    for r in levels:
        if r["top_commands"]:
            top = ", ".join(f"`{c.rsplit(' ', 1)[0]}` {c.rsplit(' ', 1)[1]}" for c in r["top_commands"].split(", "))
            print(f"| {r['level']} | {r['typists']} | {top} |")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
