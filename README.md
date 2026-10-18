# handover-hosting

The hosted side of **The Handover** CTF: a spawner that hands each browser
visitor their own isolated Incus container running the game, reachable through
a single port (so Cloudflare Tunnel can terminate upstream, no inbound ports on
the VM). Game content comes from the `bandit-wannabe` repo's `handover` Incus
image; this project only does hosting.

## Host prerequisites (OCI A1, Ubuntu 24.04 ARM)

`host/site.yml` (Ansible) describes all of this; `deploy.yml` applies it on every
deploy, so the live box and a fresh one are configured by the same code. Dry
run: `ansible-playbook -i <host>, -u ubuntu --private-key <key> host/site.yml --check --diff`.

- Incus (`incus admin init --minimal`), `ubuntu` in `incus-admin`.
- `ttyd` (>=1.7, past the old CVE), `python3-aiohttp`, `fail2ban`, rpcbind masked,
  `fs.inotify.max_user_instances=1024`.
- A `games` btrfs pool (per-container disk quota; CoW-shares the base image):
  `incus storage create games btrfs size=30GiB`
- The `isolated` profile: no NIC, per-player caps, guest API off.
  ```
  incus profile create isolated
  incus profile device add isolated root disk pool=games path=/ size=2GiB
  incus profile device add isolated eth0 none
  incus profile set isolated limits.cpu.allowance=50% limits.memory=512MiB limits.processes=300
  incus profile set isolated security.guestapi=false
  ```
- The game image as alias `handover`. It is **not built here**: `bandit-wannabe`'s
  CI builds and tests it, and its `deploy-image` workflow imports it and moves
  the alias (the previous image stays as `handover-prev`).

## Spawner

`spawner/spawner.py`, one aiohttp file. A visitor's box is a container
`p-<sid>` launched from `handover` on the `isolated` profile, plus a ttyd on a
loopback port; the spawner reverse-proxies HTTP **and the terminal WebSocket**
under `/t/<sid>/`.

- **Whose box:** the `hid` cookie, or for a signed-in player their GitHub
  account, so every device signed in to that account gets the same box. Every
  connection joins the box's one tmux session, so the devices share one screen
  (images without tmux fall back to a plain shell per connection).
- **Saves and resume:** the spawner reads each reached level's password from the
  container; the page keeps them in localStorage. "Resume at level N" starts a
  new box at the highest saved level whose password matches. Resumed runs are
  kept off the leaderboard.
- **Image updates:** after an image deploy, idle boxes on the old image are
  recycled after `OUTDATED_IDLE_MIN`; an active player moves over on their
  next reload or Play, at the level they reached.
- **Leaderboard:** progress comes from each container's own login records;
  GitHub sign-in (optional) keys it across sessions.
- **Abuse:** POST-only spawning, Turnstile, a per-IP throttle, a seat cap.
- **Restarts are non-destructive:** live containers are re-adopted.

Routes: `/` landing · `POST /play` find-or-create · `/ready` · `/app/<sid>`
terminal page · `/t/<sid>/*` ttyd proxy · `/save/<sid>` · `/leaderboard` ·
`/login` `/auth/callback` `/logout` · `/healthz`.

Config is env (see `handover-spawner.service` and the docstring at the top of
`spawner.py`); secrets come from `/home/ubuntu/handover-spawner.env`, written by CD.

### Deploying

Manual on purpose (starting it is the approval):
`gh workflow run deploy.yml -R BarryPekerman/handover-hosting` applies the host
playbook (including the tunnel connector, with its run token), copies the
spawner, writes the secrets and restarts
the service.
Pushes only run `ci.yml`. Infra: `infra/README.md`.

## Isolation

Containers run with **no network device** (`isolated` profile): the terminal
rides `incus exec` over the control socket, so a player reaches neither other
players, the host, nor the internet. Isolation is by absence, not by firewall
rule. Verified: a player container has only `lo`.

## Open

- [ ] Mobile key-bar (Esc/Ctrl/Tab/arrows) for phone play.
- [ ] Lock the OCI security list to the admin IP (parked: the admin IP rotates;
      fail2ban covers the noise).

Day-to-day operation: `docs/ops-notes.md`.

Design rationale: `bandit-wannabe/HOSTING.md`.
