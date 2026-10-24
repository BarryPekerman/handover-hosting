"""The static HTML the spawner serves.

Each page is read from pages/ once at import and cached as a module constant,
with __SHARE__, __ICON__ and __PUBLIC_URL__ filled in. The request handlers that
send these live in spawner.py; this module is just their content.
"""
import os

from common import PUBLIC_URL

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.environ.get("STATIC_DIR", os.path.join(HERE, "static"))
PAGES_DIR = os.environ.get("PAGES_DIR", os.path.join(HERE, "pages"))


def read_page(name: str) -> str:
    with open(os.path.join(PAGES_DIR, name), encoding="utf-8") as f:
        return f.read().removesuffix("\n")


ICON = '<link rel=icon type=image/png href=/favicon.ico>'
SHARE = ICON + "\n" + read_page("share.html").replace("__PUBLIC_URL__", PUBLIC_URL)


def page(name: str) -> str:
    return read_page(name).replace("__SHARE__", SHARE).replace("__ICON__", ICON)


LANDING = page("landing.html")
STORY = page("story.html")
LEADERBOARD = page("leaderboard.html")
SPAWNING = page("spawning.html")
SAVE_JS = page("save.js")
APP = page("app.html")


def message_page(title: str, heading: str, body: str, color: str) -> str:
    """A one-message page (slow down, failed bot check)."""
    return (f"""<!doctype html><html><head><meta charset=utf-8><title>{title}</title>
<style>body{{background:#000;color:{color};font-family:monospace;display:flex;height:100vh;
align-items:center;justify-content:center;text-align:center}}a{{color:{color}}}</style></head>
<body><div><h1>{heading}</h1><p>{body}</p></div></body></html>""")


SLOWDOWN = message_page("Slow down", "easy there",
                        "You've started a few servers in a short time. Give it a couple of "
                        'minutes, then <a href="/">head back</a> and press Play.', "#ffcc33")
TS_FAIL = message_page("Are you human?", "couldn't verify you",
                       'The human check didn\'t pass. <a href="/">go back</a> and press Play again.',
                       "#ff5555")
