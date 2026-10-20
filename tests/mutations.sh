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
trap 'for f in spawner/*.py.orig; do [ -e "$f" ] && mv "$f" "${f%.orig}"; done' EXIT
m spawner/auth.py 'if not hmac.compare_digest(sig, good):' 'if False:' "cookie signature not checked"
m spawner/auth.py 'and (not TS_HOSTS or j.get("hostname") in TS_HOSTS))' ')' "Turnstile hostname not checked"
m spawner/auth.py 'and j.get("action") == TS_ACTION' '' "Turnstile action not checked"
m spawner/auth.py 'if len(q) >= RATE_MAX:' 'if len(q) > RATE_MAX:' "throttle off by one"
m spawner/auth.py 'state != request.cookies.get("oauth_state")' 'False' "OAuth state (CSRF) not checked"
m spawner/auth.py 'if uid and owner and uid != owner:' 'if False:' "signed-in user can open another account's box"
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
echo; [ "$survived" -eq 0 ] && echo "every planted bug was caught" || echo "$survived bug(s) not caught"
exit "$survived"
