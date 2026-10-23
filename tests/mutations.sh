#!/bin/bash
# Tests for the tests: each line plants one realistic bug (a security check
# skipped, an off-by-one, a ranking rule broken), runs the unit tests, and
# puts the code back. Every bug must make a test fail ("caught"); "SURVIVED"
# means no test notices that bug, so a test is missing or too weak.
#   make mutate        (or PY=<python with pytest+aiohttp> tests/mutations.sh)
# Coverage says which lines ran; this says whether the tests would notice if
# those lines were wrong.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
PY=${PY:-.venv/bin/python}
survived=0
m() {  # file, code as written, the bug, what the bug means
  cp "$1" "$1.orig"
  python3 -c 'import sys; p,o,n=sys.argv[1:4]; s=open(p).read(); assert o in s, "not found: " + o; open(p,"w").write(s.replace(o,n,1))' "$1" "$2" "$3" \
    || { mv "$1.orig" "$1"; echo "STALE     $4 (code changed; update this line)"; survived=$((survived + 1)); return; }
  if "$PY" -m pytest -q -x -p no:cacheprovider tests/test_*.py >/dev/null 2>&1; then
    echo "SURVIVED  $4"; survived=$((survived + 1))
  else
    echo "caught    $4"
  fi
  mv "$1.orig" "$1"
}
trap 'for f in spawner/*.orig spawner/pages/*.orig; do [ -e "$f" ] && mv "$f" "${f%.orig}"; done' EXIT
m spawner/auth.py 'if not hmac.compare_digest(sig, good):' 'if False:' "cookie signature not checked"
m spawner/auth.py 'and (not TS_HOSTS or j.get("hostname") in TS_HOSTS))' ')' "Turnstile hostname not checked"
m spawner/auth.py 'and j.get("action") == TS_ACTION' '' "Turnstile action not checked"
m spawner/auth.py 'if len(q) >= RATE_MAX:' 'if len(q) > RATE_MAX:' "throttle off by one"
m spawner/auth.py 'state != request.cookies.get("oauth_state")' 'False' "OAuth state (CSRF) not checked"
m spawner/auth.py 'if uid and owner and uid != owner:' 'if False:' "signed-in user can open another account's box"
m spawner/auth.py 'return origin is None or urlsplit(origin).netloc in (request.host, urlsplit(PUBLIC_URL).netloc)' 'return True' "a terminal socket accepted from another site's page"
m spawner/spawner.py 'if not same_origin(request):' 'if False:' "cross-site check not applied to the terminal proxy"
m spawner/auth.py 'return str(ipaddress.ip_network(f"{a}/64", strict=False))' 'return str(a)' "IPv6 throttled per address (rotate to bypass)"
m spawner/auth.py 'if not q or now - q[-1] >= RATE_WINDOW]' 'if False]' "throttle never forgets an IP (grows forever)"
m spawner/auth.py 'SECURE_COOKIES = PUBLIC_URL.startswith("https://")' 'SECURE_COOKIES = False' "id cookies allowed over plain http"
m spawner/spawner.py 'if not TS_SECRET and os.environ.get("ALLOW_NO_TURNSTILE") != "1":' 'if False:' "spawner runs without the bot check"
m spawner/auth.py 'secure=True, ' '' "sign-in cookie not Secure"
m spawner/auth.py 'pid = pid if PID_RE.fullmatch(pid) else secrets.token_hex(16)' 'pid = pid or secrets.token_hex(16)' "player id cookie not validated"
m spawner/metrics.py 'if idx in counted:' 'if False:' "levels counted every poll"
m spawner/metrics.py 'ranked = s.get("start", 0) == 0 and' 'ranked = True or' "resumed runs ranked"
m spawner/metrics.py 'if secs <= SOLVE_CAP:' 'if True:' "walk-aways in the histogram"
m spawner/metrics.py 'return word if word in TRACKED_CMDS else "other"' 'return word' "unbounded command labels"
m spawner/metrics.py 'if not PID_RE.fullmatch(pid):' 'if not pid:' "player endpoint takes any pid"
m spawner/common.py 'box_pid.get(r[1])' 'None' "events lose the player id"
m spawner/auth.py 'return bool(GH_CLIENT_ID and GH_CLIENT_SECRET and SESSION_SECRET)' 'return bool(GH_CLIENT_SECRET and SESSION_SECRET)' "sign-in on without a client id"
m spawner/auth.py 'return bool(GH_CLIENT_ID and GH_CLIENT_SECRET and SESSION_SECRET)' 'return bool(GH_CLIENT_ID and GH_CLIENT_SECRET)' "sign-in on without a signing key"
m spawner/auth.py 'resp.set_cookie("pid", pid, httponly=True, samesite="Lax"' 'resp.set_cookie("pid", pid, samesite="Lax"' "player id readable by page scripts"
m spawner/auth.py 'resp.set_cookie(COOKIE, sid, httponly=True, samesite="Lax"' 'resp.set_cookie(COOKIE, sid, httponly=True' "box cookie sent cross-site"
m spawner/auth.py 'or request.remote or "?")' 'or "?")' "socket peer ignored"
m spawner/spawner.py 'return min(row[0], MAX_RESUME) if row and row[0] else 0' 'return row[0] if row and row[0] else 0' "resume past level 20"
m spawner/common.py '    except Exception:
        log.exception("events write failed")' '    finally:
        pass' "a database error breaks play"
# leaderboard scoring
m spawner/spawner.py 'unranked=MAX(COALESCE(players.unranked,0), excluded.unranked)' 'unranked=excluded.unranked' "a later clean poll clears unranked"
m spawner/spawner.py 'WHERE finished=1 AND COALESCE(unranked,0)=0' 'WHERE finished=1' "unranked runs on the leaderboard"
m spawner/spawner.py 'ORDER BY accumulated ASC' 'ORDER BY accumulated DESC' "leaderboard slowest first"
m spawner/spawner.py '{html.escape(d)}' '{d}' "player names not escaped on the leaderboard"
m spawner/spawner.py 'added = solve_time(reaches, prev_max, new_max)' 'added = solve_time(reaches, 0, new_max)' "solved levels' time added again every poll"
m spawner/spawner.py 'resumed_past = s is not None and s.get("start", 0) > prev_max' 'resumed_past = False' "resumed runs ranked on the leaderboard"
m spawner/spawner.py 'finished = 1 if new_max >= WIN_INDEX else finished' 'finished = finished' "reaching dave doesn't finish"
# who gets a box, whose terminal
m spawner/spawner.py 'web.get("/play", h_play_get)' 'web.get("/play", h_play)' "a GET starts a box"
m spawner/spawner.py '    if throttled(ip):' '    if False:' "no per-IP limit on new boxes"
m spawner/spawner.py 'if not await verify_turnstile(' 'if False and not await verify_turnstile(' "bot check result ignored"
m spawner/spawner.py 'if not owns(request, sid):       # not your session' 'if False:' "anyone opens any terminal"
m spawner/spawner.py 'if not s or not owns(request, sid):' 'if not s:' "anyone reads any save's passwords"
m spawner/spawner.py 'headers={"Cache-Control": "no-store"}' 'headers={}' "saved passwords cacheable"
m spawner/spawner.py 'resume_req[sid] = ({}, lvl,' 'resume_req[sid] = ({}, 0,' "an image update sends players back to level 0"
m spawner/spawner.py 'run("flock", LAUNCH_LOCK, "timeout", "120", "incus", "launch",' 'run("incus", "launch",' "boxes created at once (Incus uid-range race)"
# Standby boxes (tests/test_standby.py)
m spawner/spawner.py 'if rc == 0 and not (common.live_image and image != common.live_image):' 'if rc == 0:' "a standby box on the old image is handed out"
m spawner/spawner.py 'if rc == 0 and not (common.live_image and image != common.live_image):' 'if not (common.live_image and image != common.live_image):' "a dead standby box is handed out"
m spawner/spawner.py 'min(WARM_SPARES - len(spares) - building, MAX_PLAYERS - boxes)' 'WARM_SPARES - len(spares) - building' "standby boxes past MAX_PLAYERS boxes"
m spawner/spawner.py '| set(spares) | in_flight)' ')' "the sweep deletes standby boxes"
m spawner/spawner.py '        if common.live_image and image != common.live_image:' '        if False:' "old standby boxes kept after an image deploy"
m spawner/spawner.py 'fill_spares()                     # replace' 'pass  #' "no replacement after a handout"
m spawner/spawner.py 'choose_start(ct, resume_req' 'choose_start("p-" + sid[:12], resume_req' "a resume checked against the wrong box"
# The browser's save (tests/save.test.mjs, through test_save_js.py)
m spawner/pages/save.js '|1[0-9]|20)$/' '|1[0-9]|2[01])$/' "a save resumes straight at Dave"
m spawner/pages/save.js "/^[A-Za-z0-9]{1,64}\$/.test(v)" 'true' "anything is kept as a password"
m spawner/pages/save.js 'm=Math.max(m,+k)' 'm=k>m?k:m' "level 9 counts as higher than 10"
m spawner/pages/save.js '(j.drop||[]).forEach(function(n){delete lv[n];});' '' "levels the server rejected stay saved"
m spawner/pages/save.js 'catch(e){}}' 'catch(e){throw e}}' "a private window breaks the page"
echo; [ "$survived" -eq 0 ] && echo "every planted bug was caught" || echo "$survived bug(s) not caught"
exit "$survived"
