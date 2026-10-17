# handover-hosting

The hosted side of **The Handover** CTF: a spawner that hands each browser
visitor their own isolated Incus container running the game, reachable through
a single port (so Cloudflare Tunnel can terminate upstream, no inbound ports on
the VM). Game content comes from the `bandit-wannabe` repo's `handover` Incus
image; this project only does hosting.

## Host prerequisites (OCI A1, Ubuntu 24.04 ARM)

- Incus (`sudo apt install incus`, `incus admin init --minimal`), user in `incus-admin`.
- **OCI firewall holes for the bridge** (OCI's stock iptables REJECTs the bridge):
  ```
  sudo iptables -I INPUT  -i incusbr0 -j ACCEPT
  sudo iptables -I FORWARD -i incusbr0 -j ACCEPT
  sudo iptables -I FORWARD -o incusbr0 -j ACCEPT
  sudo iptables -t nat -A POSTROUTING -s <bridge-cidr> ! -o incusbr0 -j MASQUERADE
  sudo netfilter-persistent save
  ```
- The game image present as alias `handover` (built by `bandit-wannabe/scripts/incus-image.sh build` on this ARM host).
- `ttyd` (>=1.7, past the old CVE) and `python3-aiohttp`.
- A `games` btrfs storage pool so player containers get a per-container disk
  quota (the `dir` pool can't; btrfs also CoW-shares the base image):
  ```
  incus storage create games btrfs size=30GiB
  ```
- The `isolated` profile (NIC-less + per-player CPU/mem/proc/disk caps, guest
  API off so the host name doesn't leak into containers):
  ```
  incus profile create isolated
  incus profile device add isolated root disk pool=games path=/ size=2GiB
  incus profile device add isolated eth0 none
  incus profile set isolated limits.cpu.allowance=50% limits.memory=512MiB limits.processes=300
  incus profile set isolated security.guestapi=false
  ```

## Spawner

`spawner/spawner.py` — one file, aiohttp. Each visitor (cookie `hid`) gets a
container `p-<sid>` launched from `handover` on the `isolated` profile, plus a
ttyd on a loopback port; the spawner reverse-proxies HTTP **and the terminal
WebSocket** under `/t/<sid>/`. Global cap with a "server full" page; a reaper
deletes containers after idle / max-lifetime; stopping the service tears down
every live container.

Routes: `/` landing · `/play` find-or-create + redirect · `/t/<sid>/*` proxy · `/healthz`.

Config (env, see `handover-spawner.service`): `LISTEN_HOST/PORT`, `BASE_IMAGE`,
`PROFILE`, `MAX_PLAYERS`, `IDLE_MIN`, `MAX_LIFETIME_MIN`, `TTYD_PORT_BASE`, `LOGIN_USER`.

### Install as a service

```
scp spawner/spawner.py ubuntu@HOST:~/spawner.py
scp spawner/handover-spawner.service ubuntu@HOST:/tmp/
ssh ubuntu@HOST 'sudo mv /tmp/handover-spawner.service /etc/systemd/system/ \
  && sudo systemctl daemon-reload && sudo systemctl enable --now handover-spawner'
curl -s HOST:8080/healthz   # {"players":0,"max":20}
```

## Isolation

Containers run with **no network device** (`isolated` profile): the terminal
rides `incus exec` over the control socket, so a player reaches neither other
players, the host, nor the internet. Isolation is by absence, not by firewall
rule. Verified: a player container has only `lo`.

## Status

- [x] Spawner: per-player isolated container, one-port HTTP+WS proxy, cookie
      identity, global cap, return-visitor stickiness, and **non-destructive
      restarts** (re-adopts live containers + respawns ttyd on boot).
- [x] Instant "spinning up…" spinner (provisions in the background).
- [x] Browser UX: terminal + a localStorage **notes panel** (survives resets,
      never sent to the server).
- [x] **Resume:** the browser saves each reached level's password in
      localStorage; "resume at level N" starts the terminal at the highest
      saved level whose password matches the container's. Bad entries are
      dropped; resumed runs are kept off the leaderboard.
- [x] Ingress: Cloudflare Tunnel (`cloudflared`) → `localhost:8080`; **no inbound
      ports** on the box.
- [x] DNS: live at **https://play.barrypekerman.com**.
- [x] Abuse hardening: container creation is **POST-only** + a per-IP throttle
      (keyed on `CF-Connecting-IP`), so crawlers/loops can't fill the seats.
- [ ] Turnstile on the Play button + a Cloudflare per-IP rate-limit rule on
      `/play` (both configured in the Cloudflare dashboard).
- [ ] Lock the OCI security list to the admin IP (script staged).
- [ ] Mobile key-bar (Esc/Ctrl/Tab/arrows) for phone play.
- [ ] Analytics at teardown (playtime rows → SQLite).
- [ ] btrfs/ZFS pool for ~1s CoW clones (currently `dir`: whole-rootfs copy).
- [ ] Image-update propagation: a returning visitor keeps their existing
      container, so a rebuilt image doesn't reach them until it recycles.

See `bandit-wannabe/HOSTING.md` for the full design.
