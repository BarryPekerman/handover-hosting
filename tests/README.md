# Tests

- **`test_spawner.py`** — unit tests for the spawner's pure logic (browser
  saves, resume, scoring incl. skipped levels, whose box, image updates). Run in
  CI: `pip install pytest aiohttp && pytest tests/`.
- **`vm/run.sh`** — end to end against bandit-wannabe's Vagrant VM (`make
  local-up` there first): the real spawner, ttyd and tmux, with `vm/bin/incus`
  standing in for Incus and forwarding every "container" into the VM. Seven
  scenarios: fresh play + save, resume with a good and a forged password, one
  box across two signed-in devices, a shared tmux screen, an image change with
  upgrade at the reached level, leaderboard ranking, and the /metrics funnel on its localhost port. Run it before
  deploying spawner changes: `PYTHON=<python with aiohttp> tests/vm/run.sh`.
