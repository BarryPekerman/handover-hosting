"""Settings, shared state and the database, used by spawner.py, metrics.py
and auth.py. Everything here is one copy: code that changes a value at run
time (live_image; tests also set DB_PATH) writes `common.<name>`.
"""
import asyncio
import logging
import os
import re
import sqlite3

LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
BASE_IMAGE  = os.environ.get("BASE_IMAGE", "handover")
PROFILE     = os.environ.get("PROFILE", "isolated")
MAX_PLAYERS = int(os.environ.get("MAX_PLAYERS", "20"))
IDLE_SEC    = int(os.environ.get("IDLE_MIN", "60")) * 60
OUTDATED_IDLE = int(os.environ.get("OUTDATED_IDLE_MIN", "5")) * 60
MAX_LIFE    = int(os.environ.get("MAX_LIFETIME_MIN", "1440")) * 60
PORT_BASE   = int(os.environ.get("TTYD_PORT_BASE", "8100"))
LOGIN_USER  = os.environ.get("LOGIN_USER", "level0")
COOKIE      = "hid"
MAX_RESUME  = 20          # highest level a save can resume at
WIN_INDEX   = 21          # dave's index in the login records: reaching it is the win
PASS_RE     = re.compile(r"[A-Za-z0-9]{1,64}")
STATE_FILE  = os.environ.get("STATE_FILE", os.path.expanduser("~/.handover-sessions.json"))
DB_PATH     = os.environ.get("LEADERBOARD_DB", os.path.expanduser("~/handover-leaderboard.db"))
RATE_MAX    = int(os.environ.get("RATE_MAX", "5"))       # new containers per IP ...
RATE_WINDOW = int(os.environ.get("RATE_WINDOW", "600"))  # ... per this many seconds
PUBLIC_URL       = os.environ.get("PUBLIC_URL", "https://play.barrypekerman.com")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("spawner")

# sid -> {ct, port, proc, created, last}
sessions: dict[str, dict] = {}
locks: dict[str, asyncio.Lock] = {}      # per-sid provisioning lock
states: dict[str, str] = {}              # sid -> "pending"|"full"|"error" (ready == in sessions)
ip_hits: dict[str, list] = {}            # client IP -> recent provision timestamps (throttle)
who: dict[str, dict] = {}                # sid -> {"uid": <github id>|None, "display": str}
                                         # (a signed-in player's box follows the account)
live_ws: set = set()                     # open terminal WebSockets, closed on shutdown
resume_req: dict[str, tuple] = {}        # sid -> (saved {level: pass}, server-known level, note)
live_image = ""                          # fingerprint BASE_IMAGE points at now
background: set = set()                  # running provision tasks (asyncio keeps only weak refs)
pids: dict[str, str] = {}                # sid -> player id (random; ties one player's boxes together)
box_pid: dict[str, str] = {}             # container -> player id, for event rows (kept after teardown)
PID_RE = re.compile(r"[0-9a-f]{32}")


def db():
    c = sqlite3.connect(DB_PATH)
    # key = GitHub uid for logged-in players (persists across sessions), or
    # "anon:<sid>" for anonymous players (single-session). accumulated = sum of
    # each level's solve time, counted once.
    c.execute("CREATE TABLE IF NOT EXISTS players "
              "(key TEXT PRIMARY KEY, display TEXT, max_level INTEGER, "
              " accumulated INTEGER, finished INTEGER, updated REAL)")
    # unranked = resumed past the levels this key actually played, so its total
    # is missing time and it stays off the leaderboard.
    if "unranked" not in {r[1] for r in c.execute("PRAGMA table_info(players)")}:
        c.execute("ALTER TABLE players ADD COLUMN unranked INTEGER DEFAULT 0")
    # Raw game events for analytics (later: Postgres). box = container name;
    # no player identity. kind: start, resume, reach, finish, end, cmd.
    c.execute("CREATE TABLE IF NOT EXISTS events "
              "(ts INTEGER, box TEXT, kind TEXT, level INTEGER, detail TEXT)")
    # pid = random player id (cookie; the same across resumes and, signed in,
    # across devices). status = a command's exit code. Announced on the landing
    # and story pages; published only as totals after the game closes.
    have = {r[1] for r in c.execute("PRAGMA table_info(events)")}
    for col, typ in (("pid", "TEXT"), ("status", "INTEGER")):
        if col not in have:
            c.execute(f"ALTER TABLE events ADD COLUMN {col} {typ}")
    c.execute("CREATE TABLE IF NOT EXISTS player_ids (uid TEXT PRIMARY KEY, pid TEXT)")
    return c


def events(rows: list[tuple]):
    """Append (ts, box, kind, level, detail[, status]) rows, tagged with the
    box's player id; analytics never break play."""
    if not rows:
        return
    rows = [(*r[:5], r[5] if len(r) > 5 else None, box_pid.get(r[1])) for r in rows]
    try:
        c = db()
        c.executemany("INSERT INTO events(ts,box,kind,level,detail,status,pid) VALUES(?,?,?,?,?,?,?)", rows)
        c.commit()
        c.close()
    except Exception:
        log.exception("events write failed")


def player_row(key):
    c = db()
    row = c.execute("SELECT max_level, accumulated, finished FROM players WHERE key=?",
                    (key,)).fetchone()
    c.close()
    return row  # (max_level, accumulated, finished) or None
