# Tests

- **`test_spawner.py`** — unit tests for the spawner's pure logic (browser
  saves, resume, scoring incl. skipped levels, whose box, image updates).
- **`test_auth.py`** — signed cookies (forged, tampered, wrong key), identity,
  the player id, the per-IP throttle, Turnstile (wrong action or hostname,
  verifier down) and GitHub sign-in (state check, failures). Turnstile and
  GitHub are faked.
- **`test_metrics.py`** — finishes (ranked or not, counted once), the
  `/metrics` text, the per-player endpoint, and a broken database never
  breaking play.
- **`test_results.py`** — the published breakdown.

  CI runs them all with coverage and fails below 65% total:
  `pip install pytest pytest-cov aiohttp && pytest tests/test_*.py --cov=spawner`.
  `auth.py`, `metrics.py`, `common.py` and `gamedata.py` are at 100%;
  `spawner.py` is about a third, because its box lifecycle and proxy need Incus
  (the VM test below covers those).
- **`vm/run.sh`** — end to end against bandit-wannabe's Vagrant VM (`make
  local-up` there first): the real spawner, ttyd and tmux, with `vm/bin/incus`
  standing in for Incus and forwarding every "container" into the VM. Eight
  scenarios: fresh play + save, resume with a good and a forged password, one
  box across two signed-in devices, a shared tmux screen, an image change with
  upgrade at the reached level, leaderboard ranking, the /metrics funnel on its localhost port, and the events table with command names. Run it before
  deploying spawner changes: `PYTHON=<python with aiohttp> tests/vm/run.sh`.
