"""Prometheus metrics (counters, the solve-time histogram) and the per-player
JSON for Grafana, served on the localhost-only metrics port.
"""
import asyncio
import logging
import os

from aiohttp import web

import common
import gamedata
from contextlib import closing
from common import MAX_PLAYERS, PID_RE, WIN_INDEX, db, events, log, sessions, states

# ---------- metrics: Prometheus text format on a localhost-only port ----------
# Scraped by the OKE Prometheus through the tunnel route handover-metrics (Access
# service token). Counters reset when the spawner restarts; Prometheus handles that.
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9101"))
SOLVE_BUCKETS = (60, 180, 300, 600, 1200, 1800, 3600, 7200, 14400)  # seconds
SOLVE_CAP = 14400        # longer is a player who walked away: kept in events, not in the histogram
# Commands counted by name in the metrics; anything else is "other" (the
# events table keeps every line). Bounded so a player can't explode the series;
# shell aliases like ll count by their own name.
TRACKED_CMDS = frozenset("""
    ls cd cat less more head tail grep find file strings xxd od hexdump base64
    openssl gpg tar gzip zcat unzip bzip2 xz diff sort uniq wc cut tr awk sed
    vim vi nano man mail su ssh go sudo id whoami groups getfacl setfacl chmod
    stat ps top kill pkill pgrep systemctl journalctl crontab at env export
    echo printf history clear exit git python3 curl wget nc ss lsof sqlite3 jq
    tmux tree pwd cp mv rm mkdir touch ln readlink which type alias help
    ll la l dir egrep fgrep nl rev tac xargs tee watch sleep date hostname
    du df free uptime w who last lastlog getent locate apropos whatis info
    netstat ip ping nmap telnet scp sftp openssl sha256sum md5sum stat
    chown chgrp umask unset set source exec fg bg jobs nohup timeout
    systemd-run sudoedit visudo journalctl dmesg lsblk mount findmnt
""".split())
counters: dict[tuple, float] = {}        # (name, sorted label items) -> value
solve_hist: dict[int, list] = {}         # level -> per-bucket counts + [+Inf count, sum]


def inc(name: str, n: float = 1, **labels):
    key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
    counters[key] = counters.get(key, 0) + n


def init_counters():
    """Every series with a known label set starts at 0, so a quiet game reads 0
    on the dashboard instead of "No data" (and increase() sees the first event)."""
    zero = [("handover_level_reached_total", {"level": i}) for i in range(WIN_INDEX + 1)]
    zero += [("handover_level_solve_capped_total", {"level": i}) for i in range(WIN_INDEX)]
    zero += [("handover_finishes_total", {"ranked": r}) for r in ("true", "false")]
    zero += [("handover_resumes_total", {"outcome": o}) for o in ("resumed", "no_match", "nothing_saved")]
    zero += [("handover_box_starts_total", {"result": r}) for r in ("ready", "full", "error")]
    zero += [("handover_teardowns_total", {"reason": r})
             for r in ("idle", "max-lifetime", "outdated-idle", "upgrade")]
    zero += [("handover_errors_total", {})]
    zero += [("handover_wrong_passwords_total", {"level": i}) for i in range(WIN_INDEX)]
    for name, labels in zero:
        inc(name, 0, **labels)


class CountErrors(logging.Handler):
    """Every ERROR the spawner logs (incl. log.exception) counts in
    handover_errors_total; the journal has the detail."""

    def __init__(self):
        super().__init__(logging.ERROR)

    def emit(self, record):
        inc("handover_errors_total")


log.addHandler(CountErrors())


def observe_solve(level: int, secs: float):
    h = solve_hist.setdefault(level, [0] * len(SOLVE_BUCKETS) + [0, 0.0])
    for i, b in enumerate(SOLVE_BUCKETS):
        if secs <= b:
            h[i] += 1
    h[-2] += 1
    h[-1] += secs


def count_progress(s: dict, reaches: dict[int, int]):
    """Count each level a box reaches once (the funnel), and the time spent on
    the level before it, when that level was played in this box. Every reach
    is also an event row with its real time."""
    counted = s.setdefault("counted", [])
    rows = []
    for idx in sorted(reaches):
        if idx in counted:
            continue
        counted.append(idx)
        rows.append((reaches[idx], s.get("ct", ""), "reach", idx, ""))
        inc("handover_level_reached_total", level=idx)
        if idx - 1 in reaches and idx - 1 >= s.get("start", 0):
            secs = max(0, reaches[idx] - reaches[idx - 1])
            if secs <= SOLVE_CAP:
                observe_solve(idx - 1, secs)
            else:
                inc("handover_level_solve_capped_total", level=idx - 1)
        if idx == WIN_INDEX:
            ranked = s.get("start", 0) == 0 and all(i in reaches for i in range(WIN_INDEX + 1))
            inc("handover_finishes_total", ranked=str(ranked).lower())
            rows.append((reaches[idx], s.get("ct", ""), "finish", idx, "ranked" if ranked else "unranked"))
    events(rows)


def cmd_name(line: str) -> str:
    """The command a line runs, for the bounded metrics label: first word, path stripped."""
    word = line.split(maxsplit=1)[0] if line.strip() else ""
    word = word.rsplit("/", 1)[-1]
    return word if word in TRACKED_CMDS else "other"


def count_commands(s: dict, found: list[tuple]):
    """found: (epoch, level, kind, detail, status) read from the box's journal.
    kind "cmd": detail is the command line as typed, status its exit code (None
    from images that logged names only). kind "badpass": a wrong password for
    the account in detail, counted against the level the player is on."""
    for _, lvl, kind, detail, _ in found:
        if kind == "cmd":
            inc("handover_commands_total", level=lvl, cmd=cmd_name(detail))
        else:
            inc("handover_wrong_passwords_total", level=lvl)
    events([(t, s.get("ct", ""), kind, lvl, detail, status) for t, lvl, kind, detail, status in found])


def _labels(items) -> str:
    return "{" + ",".join(f'{k}="{v}"' for k, v in items) + "}" if items else ""


def render_metrics() -> str:
    out = [
        "# TYPE handover_players gauge", f"handover_players {len(sessions)}",
        "# TYPE handover_seats gauge", f"handover_seats {MAX_PLAYERS}",
        "# TYPE handover_spares gauge", f"handover_spares {len(common.spares)}",
        "# TYPE handover_starting gauge",
        f"handover_starting {sum(1 for v in states.values() if v == 'pending')}",
        "# TYPE handover_info gauge", f'handover_info{{image="{common.live_image[:12]}"}} 1',
        "# TYPE handover_player_level gauge",
    ]
    at = {}
    for x in sessions.values():
        lvl = max(x.get("counted") or [x.get("start", 0)])
        at[lvl] = at.get(lvl, 0) + 1
    out += [f'handover_player_level{{level="{lvl}"}} {n}' for lvl, n in sorted(at.items())]
    for name in sorted({k[0] for k in counters}):
        out.append(f"# TYPE {name} counter")
        out += [f"{name}{_labels(items)} {v:g}" for (n, items), v in sorted(counters.items()) if n == name]
    out.append("# TYPE handover_level_solve_seconds histogram")
    for lvl, h in sorted(solve_hist.items()):
        for b, c in zip(SOLVE_BUCKETS, h, strict=False):
            out.append(f'handover_level_solve_seconds_bucket{{level="{lvl}",le="{b}"}} {c}')
        out.append(f'handover_level_solve_seconds_bucket{{level="{lvl}",le="+Inf"}} {h[-2]}')
        out.append(f'handover_level_solve_seconds_count{{level="{lvl}"}} {h[-2]}')
        out.append(f'handover_level_solve_seconds_sum{{level="{lvl}"}} {h[-1]:g}')
    return "\n".join(out) + "\n"


async def h_metrics(request):
    return web.Response(text=render_metrics(), content_type="text/plain")


# ---------- player data for Grafana (metrics port only) ----------
# The raw per-player view for the operator while the game runs: Grafana's
# Infinity datasource reads these through the handover-metrics tunnel route,
# which only the oke-prometheus Access token passes. Never on the play port.

def _data(fn):
    with closing(db()) as c:
        return fn(c)


async def h_data_players(request):
    rows = await asyncio.to_thread(_data, lambda c: gamedata.Game(gamedata.load(c)).players())
    return web.json_response(rows)


async def h_data_levels(request):
    rows = await asyncio.to_thread(_data, lambda c: gamedata.Game(gamedata.load(c)).levels())
    return web.json_response(rows)


async def h_data_player(request):
    pid = request.query.get("pid", "")
    if not PID_RE.fullmatch(pid):
        return web.json_response([])
    return web.json_response(await asyncio.to_thread(_data, lambda c: gamedata.timeline(c, pid)))


def make_metrics_app():
    mapp = web.Application()
    mapp.router.add_get("/metrics", h_metrics)
    mapp.router.add_get("/data/players", h_data_players)
    mapp.router.add_get("/data/levels", h_data_levels)
    mapp.router.add_get("/data/player", h_data_player)
    return mapp

