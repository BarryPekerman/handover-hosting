"""Gameplay numbers from the spawner's events table, shared by the spawner's
/data/* endpoints (Grafana, while the game runs) and scripts/results.py (the
published breakdown when it closes). Standard library only.

Times per level come from each player's first arrival at a level and at the
next one, across all their boxes (resumes included). "Active" time leaves out
gaps of more than GAP seconds between that player's commands and arrivals, so
a lunch break doesn't count as being stuck.
"""
import statistics
from collections import Counter, defaultdict

GAP = 300          # seconds without activity that count as a break
WIN = 21           # dave's index: reaching it is the win


def load(conn) -> list[tuple]:
    """(ts, box, kind, level, detail, status, pid) for every event with a player id."""
    return conn.execute("SELECT ts, box, kind, level, detail, status, pid FROM events "
                        "WHERE pid IS NOT NULL AND pid != '' ORDER BY ts, rowid").fetchall()


def cmd_word(line) -> str:
    """The command a line runs: its first word, path stripped."""
    word = line.split(maxsplit=1)[0] if line and line.strip() else ""
    return word.rsplit("/", 1)[-1] or "?"


def stats(values) -> dict:
    if not values:
        return {"n": 0, "median": None, "mean": None, "fastest": None, "slowest": None}
    return {"n": len(values), "median": statistics.median(values), "mean": statistics.mean(values),
            "fastest": min(values), "slowest": max(values)}


def active_between(times: list[int], t0: int, t1: int) -> int:
    """Seconds between t0 and t1, leaving out gaps longer than GAP."""
    ts = sorted({t0, t1, *(t for t in times if t0 <= t <= t1)})
    return sum(min(b - a, GAP) for a, b in zip(ts, ts[1:]))


class Game:
    """Everything indexed once, per player and per level."""

    def __init__(self, rows):
        self.reach = defaultdict(dict)          # pid -> level -> first arrival
        self.activity = defaultdict(list)       # pid -> timestamps of commands and arrivals
        self.first = {}                         # pid -> first event
        self.last = {}                          # pid -> last event
        self.boxes = defaultdict(set)
        self.n_cmds = Counter()                 # pid -> commands
        self.n_failed = Counter()               # pid -> commands with a non-zero exit
        self.n_badpass = Counter()              # pid -> wrong passwords
        self.level_cmds = defaultdict(Counter)  # level -> command word -> times
        self.level_failed = defaultdict(Counter)
        self.level_typists = defaultdict(set)
        self.level_badpass = Counter()
        self.level_badpass_players = defaultdict(set)
        for ts, box, kind, level, detail, status, pid in rows:
            self.first.setdefault(pid, ts)
            self.last[pid] = ts
            self.boxes[pid].add(box)
            if kind in ("reach", "finish"):
                self.reach[pid].setdefault(level, ts)
                self.activity[pid].append(ts)
            elif kind == "cmd":
                self.activity[pid].append(ts)
                self.n_cmds[pid] += 1
                self.level_cmds[level][cmd_word(detail)] += 1
                self.level_typists[level].add(pid)
                if status:
                    self.n_failed[pid] += 1
                    self.level_failed[level][cmd_word(detail)] += 1
            elif kind == "badpass":
                self.n_badpass[pid] += 1
                self.level_badpass[level] += 1
                self.level_badpass_players[level].add(pid)

    def level_times(self):
        """level -> ([active seconds], [wall seconds]) for players who solved it."""
        out = defaultdict(lambda: ([], []))
        for pid, lv in self.reach.items():
            for level in range(WIN):
                if level in lv and level + 1 in lv and lv[level + 1] >= lv[level]:
                    out[level][0].append(active_between(self.activity[pid], lv[level], lv[level + 1]))
                    out[level][1].append(lv[level + 1] - lv[level])
        return out

    def players(self) -> list[dict]:
        """One row per player, most recently active first."""
        rows = []
        for pid in self.first:
            lv = self.reach.get(pid, {})
            acts = sorted(self.activity[pid])
            rows.append({
                "pid": pid, "player": pid[:8],
                "level": max(lv, default=None),
                "won": WIN in lv,
                "active_s": active_between(acts, acts[0], acts[-1]) if acts else 0,
                "commands": self.n_cmds[pid],
                "failed": self.n_failed[pid],
                "wrong_passwords": self.n_badpass[pid],
                "boxes": len(self.boxes[pid]),
                "first_seen": self.first[pid], "last_seen": self.last[pid],
            })
        return sorted(rows, key=lambda r: r["last_seen"], reverse=True)

    def levels(self, top: int = 5) -> list[dict]:
        """One row per level: reached, solve times, where players struggled."""
        times = self.level_times()
        rows, prev = [], None
        for level in range(WIN + 1):
            reached = sum(1 for lv in self.reach.values() if level in lv)
            act, wall = stats(times[level][0]), stats(times[level][1])
            fail = self.level_failed[level].most_common(1)
            rows.append({
                "level": level, "label": "Dave (won)" if level == WIN else str(level),
                "reached": reached, "lost": None if prev is None else max(prev - reached, 0),
                "solved": act["n"],
                "median_active_s": act["median"], "mean_active_s": act["mean"],
                "fastest_s": act["fastest"], "slowest_active_s": act["slowest"],
                "median_wall_s": wall["median"],
                "wrong_passwords": self.level_badpass[level],
                "players_wrong_password": len(self.level_badpass_players[level]),
                "most_failed": f"{fail[0][0]} ({fail[0][1]}x)" if fail else "",
                "typists": len(self.level_typists[level]),
                "top_commands": ", ".join(f"{k} {v}" for k, v in self.level_cmds[level].most_common(top)),
            })
            prev = reached
        return rows

    def whole_game_active(self) -> list[int]:
        """Active seconds from level 0 to the win, for players who did both."""
        return [active_between(self.activity[pid], lv[0], lv[WIN])
                for pid, lv in self.reach.items() if 0 in lv and WIN in lv]


def timeline(conn, pid: str) -> list[dict]:
    """Everything one player did, oldest first."""
    return [{"time": ts, "box": box, "kind": kind, "level": level, "detail": detail or "", "status": status}
            for ts, box, kind, level, detail, status in conn.execute(
                "SELECT ts, box, kind, level, detail, status FROM events WHERE pid=? ORDER BY ts, rowid",
                (pid,))]
