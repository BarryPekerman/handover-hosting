# Tests

| Command | What it runs | Time |
|---|---|---|
| `make test` | ruff + unit tests with coverage (what CI runs) | seconds |
| `make mutate` | plants known bugs one at a time; the unit tests must catch each | ~1 min |
| `make e2e` | the real spawner, ttyd and tmux against the Vagrant game VM | ~1 min |
| `scripts/capacity.sh` | on the host: how many boxes it holds (see below) | ~10 min |

All three build their own `.venv` on first use.

## Unit tests

- **`test_spawner.py`** — the spawner's own rules: browser saves, resume
  (including the level-20 cap for signed-in players), scoring incl. skipped
  levels, whose box, image updates, the pages loading with placeholders filled.
- **`test_auth.py`** — signed cookies (forged, tampered, wrong key), identity,
  the player id, cookie flags, the per-IP throttle, where the client IP comes
  from, Turnstile (wrong action or hostname, verifier down) and GitHub sign-in
  (off if any setting is missing, state check, failures). Turnstile and GitHub
  are faked.
- **`test_metrics.py`** — finishes (ranked or not, counted once), the
  `/metrics` text, the per-player endpoint, and a broken database never
  breaking play.
- **`test_routes.py`** — through real HTTP requests to the app, with Incus
  faked: the leaderboard's scoring (time added once per level, finishing,
  resumed or skipped runs unranked and never cleared, fastest first, names
  escaped) and who gets a box and whose terminal (a GET never starts one, the
  per-IP limit and bot check run before a box is spent, another account or a
  missing cookie can't open a terminal or read a save, an image update resumes
  at the level reached).
- **`test_results.py`** — the published breakdown.
- **`save.test.mjs`** (run by `test_save_js.py` with Node or Deno) — the
  browser's save: survives a reload, only levels 1-20 with plain passwords are
  kept, a corrupt save or a private window reads as empty instead of breaking
  the page, level 10 beats level 9, and the server's rejected levels and new
  passwords win over what the browser had. Skipped only where neither runtime
  is installed; CI has Node.

CI fails below 77% total coverage. `auth.py`, `metrics.py`, `common.py` and
`gamedata.py` are at 100%; `spawner.py` is about half. What's left there is
mostly the box lifecycle and the terminal proxy, which need Incus (the VM test
covers those), plus the landing page text. The floor only goes
up: raise it when coverage rises, never lower it to get a change through.

## Testing the tests (`mutations.sh`)

Coverage says which lines ran; it doesn't say a test would notice if they were
wrong. `make mutate` checks that (and CI runs it): each of its 41 lines plants one realistic bug
(a forged cookie accepted, the bot check skipping the hostname, an off-by-one in
the throttle, a resumed run ranked, boxes created without the launch lock, the
browser keeping a level the server rejected...), runs
the unit tests, and restores the code. Every bug must be **caught**; a
**SURVIVED** line names a behaviour no test pins down. When you add a rule worth
protecting, add a line for it. If the code a line targets changes, the line
reports **STALE** until it's updated.

What makes a test worth keeping: it fails when a rule the game depends on
breaks. A test that only restates the code (reads back a constant, checks a
file contains the text it was written with, checks SQLite stores a row) passes
whatever the code does, so it's noise. These were removed or rewritten in
October 2026:

| Was | Why it was weak | Now |
|---|---|---|
| `test_player_row`: insert a row, read it back | tested SQLite, not our code | removed; `test_signed_in_resume_level_comes_from_the_leaderboard_capped_at_20` uses the same lookup to test a real rule (a winner resumes at 20, not at Dave) |
| `test_oauth_needs_all_three_settings`: one setting missing | dropping the client-id or signing-key check still passed | `test_sign_in_is_off_if_any_setting_is_missing`, once per setting, and `/login` must then go home |
| `test_pid_cookie_...` and `test_box_cookie_...`: value and max-age equal the constants | restated the code; missed the flags that matter | `test_id_cookies_are_hidden_from_page_scripts_and_other_sites`: httponly and SameSite=Lax on both |
| `test_client_ip_without_headers_or_a_peer_is_unknown` | mostly tested that a fake request has no socket | `test_client_ip_without_proxy_headers_is_the_socket_peer` |
| `test_a_well_formed_player_id_is_kept_...` | duplicated `test_player_id_follows_cookie_then_account` | removed |
| `test_login_without_oauth_goes_home` | covered by the sign-in test above | removed |
| pages test: starts with `<!doctype html>`, `loadSave` is in `save.js` | checked files contain the text they were written with | removed those lines; the placeholder check and the data notice stay |

Each rewrite was checked by planting the bug it should catch (now in
`mutations.sh`); with the old one-case sign-in test, two of them survive.

## End to end (`vm/`)

**`vm/run.sh`** runs against bandit-wannabe's Vagrant VM: the real spawner,
ttyd and tmux, with `vm/bin/incus` standing in for Incus and forwarding every
"container" into the VM. Eight scenarios: fresh play + save, resume with a good
and a forged password, one box across two signed-in devices, a shared tmux
screen, an image change with upgrade at the reached level, leaderboard ranking,
the /metrics funnel on its localhost port, and the events table with commands.
Run it before deploying spawner changes: `make e2e` starts the VM if it isn't
running. Every check prints ok or FAIL, and the run exits non-zero if any
failed. Every wait has a limit (a box ready in 90 s, the image update seen in
60 s, the whole run in 15 min), so a stuck spawner fails a check instead of
hanging; there are no fixed sleeps.

## Capacity (`scripts/capacity.sh`, on the host)

Starts `load-N` boxes from the live image on the players' profile, in bursts
of 10 up to 40, each with a player logged in through tmux the way ttyd does it,
and records how long they took to come up, memory, pool use and how fast the
spawner still answers; then every box searches the filesystem at once. The
boxes never go through the spawner, so nothing reaches the leaderboard or the
analytics. It refuses to start while anyone plays, stops if a player arrives or
memory runs low, and deletes every load box on exit.

    scp scripts/capacity.sh handover: && ssh handover 'bash capacity.sh 40 </dev/null'

Results from 20 October 2026 are in `docs/ops-notes.md` (Capacity).
