#!/usr/bin/env python3
"""The Handover: gameplay breakdown from the spawner's events table.

    python3 scripts/results.py ~/handover-leaderboard.db > results.md

Run it on the host, or on a copy from the nightly backup (docs/ops-notes.md).
Prints Markdown: players, how far they got, time per level (wall clock and
active), wrong passwords and the commands used on each level. Only totals and
averages: no player ids, no full command lines, so the output is safe to
publish. Standard library only.

Times per level come from each player's first arrival at a level and at the
next one, across all their boxes (resumes included). "Active" time leaves out
gaps of more than 5 minutes between that player's commands, so a lunch break
doesn't count as being stuck.
"""
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict

GAP = 300          # seconds without a command that count as a break
WIN = 21           # dave's index: reaching it is the win
TOP = 5            # commands listed per level


def fmt(sec):
    if sec is None:
        return "-"
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


def name(line):
    word = line.split(maxsplit=1)[0] if line and line.strip() else ""
    return word.rsplit("/", 1)[-1] or "?"


def stats(values):
    if not values:
        return dict(n=0, median=None, mean=None, fastest=None, slowest=None)
    return dict(n=len(values), median=statistics.median(values), mean=statistics.mean(values),
                fastest=min(values), slowest=max(values))


def main(path):
    c = sqlite3.connect(path)
    rows = c.execute("SELECT ts, kind, level, detail, status, pid FROM events "
                     "WHERE pid IS NOT NULL AND pid != '' ORDER BY ts").fetchall()
    if not rows:
        print("No events with a player id yet.")
        return

    reach = defaultdict(dict)                 # pid -> level -> first ts
    activity = defaultdict(list)              # pid -> timestamps of commands and arrivals
    cmds = defaultdict(Counter)               # level -> command name -> times typed
    failed = defaultdict(Counter)             # level -> command name -> non-zero exits
    users_by_level = defaultdict(set)         # level -> pids that typed something there
    badpass = Counter()                       # level -> wrong passwords
    badpass_players = defaultdict(set)
    for ts, kind, level, detail, status, pid in rows:
        if kind in ("reach", "finish"):
            reach[pid].setdefault(level, ts)
            activity[pid].append(ts)
        elif kind == "cmd":
            activity[pid].append(ts)
            cmds[level][name(detail)] += 1
            users_by_level[level].add(pid)
            if status:
                failed[level][name(detail)] += 1
        elif kind == "badpass":
            badpass[level] += 1
            badpass_players[level].add(pid)

    def active(pid, t0, t1):
        """Seconds between t0 and t1 not spent in gaps longer than GAP."""
        ts = [t for t in activity[pid] if t0 <= t <= t1]
        ts = sorted({t0, *ts, t1})
        return sum(min(b - a, GAP) for a, b in zip(ts, ts[1:]))

    wall, act = defaultdict(list), defaultdict(list)
    total_act = []
    for pid, lv in reach.items():
        for level in range(WIN):
            if level in lv and level + 1 in lv and lv[level + 1] >= lv[level]:
                wall[level].append(lv[level + 1] - lv[level])
                act[level].append(active(pid, lv[level], lv[level + 1]))
        if 0 in lv and WIN in lv:
            total_act.append(active(pid, lv[0], lv[WIN]))

    players = len(reach)
    won = sum(1 for lv in reach.values() if WIN in lv)
    print("# The Handover: results\n")
    print(f"- Players: {players}")
    print(f"- Won (reached Dave): {won} ({100 * won / players:.0f}%)" if players else "- Won: 0")
    print(f"- Commands typed: {sum(sum(x.values()) for x in cmds.values())}")
    print(f"- Wrong passwords: {sum(badpass.values())}")
    t = stats(total_act)
    print(f"- Whole game, active time (players who started at level 0 and won): "
          f"median {fmt(t['median'])}, fastest {fmt(t['fastest'])}, slowest {fmt(t['slowest'])}\n")

    print("## How far players got\n")
    print("| Level | Players reached | Lost since previous |")
    print("| ---: | ---: | ---: |")
    prev = None
    for level in range(WIN + 1):
        n = sum(1 for lv in reach.values() if level in lv)
        lost = "" if prev is None else str(max(prev - n, 0))
        label = "Dave (won)" if level == WIN else str(level)
        print(f"| {label} | {n} | {lost} |")
        prev = n

    print("\n## Time to solve each level\n")
    print("Active time leaves out breaks of more than 5 minutes.\n")
    print("| Level | Solved by | Median active | Mean active | Fastest | Slowest active | Median wall clock |")
    print("| ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for level in range(WIN):
        a, w = stats(act[level]), stats(wall[level])
        print(f"| {level} | {a['n']} | {fmt(a['median'])} | {fmt(a['mean'])} | {fmt(a['fastest'])} "
              f"| {fmt(a['slowest'])} | {fmt(w['median'])} |")

    print("\n## Where players struggled\n")
    print("| Level | Wrong passwords | Players who got one wrong | Most failed command |")
    print("| ---: | ---: | ---: | --- |")
    for level in range(WIN + 1):
        top_fail = failed[level].most_common(1)
        tf = f"`{top_fail[0][0]}` ({top_fail[0][1]}x)" if top_fail else "-"
        if badpass[level] or top_fail:
            print(f"| {level} | {badpass[level]} | {len(badpass_players[level])} | {tf} |")

    print(f"\n## Commands used on each level (top {TOP})\n")
    print("| Level | Players typing | Commands |")
    print("| ---: | ---: | --- |")
    for level in range(WIN + 1):
        if cmds[level]:
            top = ", ".join(f"`{k}` {v}" for k, v in cmds[level].most_common(TOP))
            print(f"| {level} | {len(users_by_level[level])} | {top} |")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
