# Ops Notes — The Handover (hosted)

A living handoff doc: current live state, what's been done, how to operate the
box, and what to check when player feedback arrives. Newest session section on
top. Not player-facing.

---

## System at a glance

- **Live URL:** https://play.barrypekerman.com (anonymous or GitHub sign-in).
- **Host:** OCI ARM (aarch64) instance in compartment `handover` of a family
  member's separate OCI tenancy, isolated from the OKE cluster. Public IP
  `84.13.72.156` (ephemeral; nothing depends on it any more).
- **Access: `ssh handover`** — through the Cloudflare tunnel
  (`handover-ssh.barrypekerman.com`, Cloudflare Access app `handover-ssh`: your
  email login in the browser about once a day; `~/.ssh/config` entry uses
  `cloudflared access ssh`). **Public port 22 is closed** at the subnet's
  security list. Key-only; `ubuntu` is in `incus-admin`.
- **Break-glass** (tunnel down): OCI console → Networking → Virtual cloud
  networks → Basic-VCN → Security Lists → Default Security List → add ingress
  TCP 22 from your IP; then `ssh -i ~/.ssh/handover ubuntu@<ip>`; remove
  the rule afterwards.
- **Serving path:** browser → Cloudflare (DNS is Cloudflare-only; origin IP not
  in DNS) → **Cloudflare Tunnel** `handover-tf` (`cloudflared-tf.service`,
  outbound) → spawner on `127.0.0.1:8080`. The tunnel and its routes are
  Terraform (`infra/tunnel.tf`); the host holds only the run token
  (`/etc/cloudflared/handover-tf.env`). Nothing is open inbound on the origin,
  SSH included.
- **Spawner:** `spawner.py` plus its modules `common.py`, `auth.py`,
  `metrics.py`, `gamedata.py` (aiohttp; all in `~`; its pages are
  `~/pages/*.html`, read at startup, so a text edit needs a deploy/restart), systemd unit `handover-spawner`
  (`/etc/systemd/system/handover-spawner.service`, runs as `ubuntu`,
  `ExecStart=/home/ubuntu/spawner.py`). One per-player **Incus** container
  (`p-<sid12>`) + its own loopback **ttyd**, reverse-proxied at `/t/<sid>/`.
- **Per-player container:** clone of the golden image `handover` on the
  `isolated` profile — no NIC, `security.guestapi=false`, CPU 50% / mem 512 MiB
  / procs 300, **1.5 GiB btrfs disk quota** (counts the shared ~0.95 GiB base
  image, so ~560 MiB writable; pool `games`, 30 GiB loop ceiling),
  own uid range per box (`security.idmap.isolated`). Up to **40 players**
  (`MAX_PLAYERS`); measured idle box: 1.4% of a core, 138 MiB RAM, 46 MiB disk.
- **Code:** hosting (spawner, cloudflared, infra/terraform, this doc) lives in
  **`handover-hosting`**; the game itself lives in **`bandit-wannabe`**, whose CI
  builds the arm64 `handover` image and whose `deploy-image` workflow ships it.
  Nothing is built on the host.
- **Deploys:** all manual `gh workflow run` (that's the approval): `deploy.yml`
  here (spawner), `deploy-image.yml` in bandit-wannabe (image),
  `terraform.yml` here (infra, via Terraform Cloud `berries-handover`).
- **CI access:** each repo's deploy workflow has its own SSH key
  (`DEPLOY_SSH_KEY`; public halves in `host/deploy-keys/`, installed by
  `host/site.yml`) and checks the host key against `DEPLOY_KNOWN_HOSTS`. To
  revoke a repo's access: remove its line from `~ubuntu/.ssh/authorized_keys`
  and its file + loop entry from `host/site.yml`. The admin key
  (`handover`) is in no repo.
- **Monitoring:** the OKE cluster's Prometheus (repo `oke-gitops`) scrapes
  the spawner's `/metrics` (`127.0.0.1:9101`: players, seats, box
  starts/teardowns/resumes, the level funnel, time per level, finishes) and
  node-exporter (`127.0.0.1:9100`) through tunnel routes `handover-metrics` /
  `handover-node`. An Access app lets only the `oke-prometheus` service token
  through (`infra/metrics.tf`). Alerts go to Telegram; the "The Handover"
  Grafana dashboard is in `oke-gitops`, served at
  https://ctf-monitor.barrypekerman.com (tunnel `oke-grafana` from a cloudflared
  in the cluster, `infra/grafana.tf`; Access: your email). Counters restart with the spawner; a
  box's counted levels are kept in the state file, so a restart doesn't count
  them twice.
- **Secrets:** `/home/ubuntu/handover-spawner.env` (git-ignored, host-only):
  `SESSION_SECRET`, `GH_CLIENT_SECRET`, `TURNSTILE_SECRET`. Tunnel creds are
  cloudflared's. Nothing sensitive is in git or the image.

## Current state (2026-10-18)

Everything working. Image and spawner ship through the pipelines; Terraform
runs in Terraform Cloud as the scoped OCI user `tf-handover` (instance in
compartment `handover`). Host config is `host/site.yml`, applied by
`deploy.yml`.

## Operator playbook

```bash
ssh handover                                          # get on the box (tunnel)

# health / who's playing
curl -s 127.0.0.1:8080/healthz                        # {"players":N,"max":40}
curl -s 127.0.0.1:9101/metrics                        # what Prometheus sees
incus list -c ns -f compact                           # live containers
cat ~/.handover-sessions.json | python3 -m json.tool  # sid -> container/port

# logs
journalctl -u handover-spawner -n 50 --no-pager
journalctl -u handover-spawner -f                     # follow

# safe restart (graceful; WS close + 5s timeout, re-adopts live containers)
sudo systemctl restart handover-spawner

# game image: shipped by bandit-wannabe's deploy-image workflow (not built here)
incus image list -c lfsu                              # handover = live, handover-prev = rollback
# rollback to the previous image:
incus query -X PATCH /1.0/images/aliases/handover \
  -d "{\"target\":\"$(incus image info handover-prev | awk '/^Fingerprint/{print $2}')\"}"

# profile / pool config that backs the containers
incus profile show isolated
incus storage list
fail2ban-client status sshd
```

Deploying spawner changes: `gh workflow run deploy.yml` (manual = the approval;
pushes to `main` only run `ci.yml`). `.github/workflows/deploy.yml`
applies `host/site.yml` (with the tunnel's run token from `CF_TUNNEL_TOKEN`),
copies `spawner/`, writes the secrets env file from GitHub Secrets and restarts
the spawner. It refuses a commit that isn't on `main` with a passing `ci` run
(wait for CI after merging), and fails if the spawner doesn't answer `/healthz`
on the host within 30 s, stops, logs an `ERROR`/`Traceback` since the restart,
or doesn't answer through the tunnel. A red deploy has still restarted the
spawner: look at the step's output, fix, deploy again. Don't scp by
hand. The game image ships the same way from bandit-wannabe: a `ci` run on
`main` builds and tests it, then
`gh workflow run deploy-image.yml -R BarryPekerman/bandit-wannabe -f run_id=<id>`
imports it and moves the `handover` alias (previous kept as `handover-prev`).
Host setup changes go in `host/site.yml` (Ansible), never by hand: `deploy.yml`
applies it on every deploy. Dry run from a laptop:
`ansible-playbook -i handover, host/site.yml --check --diff`.

## Capacity (measured 2026-10-20)

`scripts/capacity.sh` on the live host (4 cores, 23.4 GiB, no swap), 40 boxes
in bursts of 10, each with a player logged in through tmux:

| boxes | slowest to be ready | of which booting | free memory | boxes' memory | pool | load (1 min) |
|---|---|---|---|---|---|---|
| 10 | 160 s | 8 s | 22.1 GiB | 1.5 GiB | 4.0 GiB | 7.9 |
| 20 | 121 s | 6 s | 21.3 GiB | 3.0 GiB | 3.7 GiB | 8.9 |
| 30 | 97 s | 6 s | 20.5 GiB | 4.5 GiB | 4.5 GiB | 9.8 |
| 40 | 131 s | 7 s | 19.7 GiB | 6.1 GiB | 5.4 GiB | 20.8 |

All 40 boxes searching the filesystem at once took 6 s; the spawner answered
`/healthz` within 22 ms throughout.

- **Memory isn't the limit.** About 85 MiB per box with a player in it; 40
  boxes leave 19.7 GiB free. Even all 40 at their 512 MiB limit (20 GiB) fits
  in 23.4 GiB, just. `MAX_PLAYERS=40` stays.
- **Starting boxes is.** Starting one box takes 5-15 s, because Incus re-owns
  every file of the ~0.95 GiB image to the box's own uid range
  (`security.idmap.isolated`; the files on disk belong to uid 1065536 etc.).
  That's CPU-heavy (load 20 on 4 cores during a burst) and doesn't get faster
  in parallel. So when 10 people press Play in the same minute, the 10th waits
  about 2 minutes on the spinner; 2-3 at once wait 30-45 s. Booting after the
  start is under 10 s, well inside the spawner's limit, so nobody fails; they
  wait. Changing it means giving up per-box uid ranges or moving the pool,
  i.e. infrastructure; not done.
- The run shows on the Grafana host panels (node-exporter) between 11:55 and
  12:35 UTC on 2026-10-20; nothing reached the leaderboard or the player
  metrics.

Rerun after any change to the profile, the image size or the host shape,
while nobody plays (it refuses otherwise): see `tests/README.md`.

## Reboots

Kernel updates install unattended; `handover-maybe-reboot.timer` (04:00 Israel
time) reboots only when `/run/reboot-required` exists and `/healthz` says 0
players, otherwise it waits for the next night. Decision without rebooting:
`sudo env DRY_RUN=1 /usr/local/sbin/handover-maybe-reboot`; history:
`journalctl -t handover-reboot`. After a reboot, player containers are gone
(they don't autostart); the spawner drops their sessions and players resume.

## Backups

`backup.yml` runs nightly (02:17 UTC): the leaderboard DB, encrypted with `BACKUP_PASSPHRASE` (a copy is in `~/handover-backup-passphrase.txt`
on the admin laptop), kept 30 days as the `handover-backup` artifact. Restore:

```bash
gh run download <run> -R BarryPekerman/handover-hosting -n handover-backup
gpg --pinentry-mode loopback --passphrase-file ~/handover-backup-passphrase.txt \
  -d handover-backup-*.tgz.gpg | tar xzf - -C restore/
scp restore/handover-leaderboard.db handover:~/          # spawner stopped
```

The live level passwords are not in it (they're the `HANDOVER_CREDENTIALS`
secret; readable copy: `~/handover-live-credentials.tgz` on the admin laptop).

## Gameplay data and closing the game

Players are told on the landing and story pages that their session is recorded:
every command line (capped at 200 characters) with its exit status, wrong
passwords, and when each level is reached, under a random player id (`pid`
cookie; linked to the GitHub account for signed-in players). No IPs are kept.
It all lands in the `events` table of `~/handover-leaderboard.db` (nightly
backup). The promise: publish only totals and averages, then delete the raw data.

While the game runs, the Grafana dashboard **The Handover — players** shows it
per player: the spawner serves `/data/players`, `/data/levels` and
`/data/player?pid=` (JSON) on the metrics port only, which the `handover-metrics`
tunnel route exposes behind the `oke-prometheus` Access token; Grafana's
Infinity datasource reads them with that token. The numbers come from
`spawner/gamedata.py`, the same code `scripts/results.py` uses, so the live view
and the published breakdown agree.

When the game closes:

1. `python3 scripts/results.py ~/handover-leaderboard.db > results.md` (on the
   host, or on a restored backup). It prints totals and averages only, with no
   player ids or command lines, so it can be published as is.
2. Delete the raw records: `sqlite3 ~/handover-leaderboard.db 'DELETE FROM events; DELETE FROM player_ids; VACUUM;'`,
   and delete the backup artifacts in GitHub Actions (they expire after 30 days anyway).

## Known gotchas / non-bugs (seen this session)

- **Boxes are created one at a time (`LAUNCH_LOCK`).** Incus 6.0.0 races when
  two boxes are created at once with `security.idmap.isolated`: it hands out a
  uid range below root's subuid range (196608, 262144...; root's starts at
  1000000), and the box fails to start (`newuidmap ... not allowed`) or runs
  on a range another box has. Found by the capacity test, 2026-10-20; never
  seen by a player. The spawner's `incus launch` and the image deploy's
  warm-up `incus init` take `flock /run/lock/handover-launch`; anything else
  that creates boxes on the host must too.

- **The btrfs quota counts the shared base image.** Incus sets the limit on a
  box's *referenced* data (~0.95 GiB of image + its own writes), not on what
  it adds to the pool. A quota at or below the image size makes boxes fail to
  start (2026-10-19: 512 MiB did; fixed to 1.5 GiB). Measure with
  `sudo btrfs qgroup show -reF /var/lib/incus/storage-pools/games/containers/<box>`.
- **Players can't write files over 10 MiB** (`fsize` limit, security role) —
  by design; "File size limit exceeded" is that, not the disk quota.

- **"I dropped into level10":** was a misread of `level0@<hostname>`. Prompt now
  shows `level0@handover` (fixed hostname) to kill the ambiguity. Accounts are
  un-padded: `level0, level1, … level10, … level20` (no `level01`); they sort
  lexically so `level10` lists right under `level1`.
- **Restarting the spawner resets a live player's terminal** to a fresh login
  (ttyd is killed + respawned; the container survives via re-adopt). Brief blip,
  not data loss.
- **Idle reaper = 15 min** (`IDLE_MIN`, was 60 until launch), max lifetime 1440 min.
  Idle = no keystrokes. Idle containers disappear; a returning player gets a
  fresh container and resumes from their save. Short so drive-by visitors (a
  LinkedIn post) don't hold seats for an hour.
- **After an image deploy** boxes on the old image are recycled once idle for
  5 min (`OUTDATED_IDLE_MIN`); active players move over on their next reload
  or Play, at the level they reached.
- **One shared password set:** every container is a clone of the same golden
  image, so all level passwords are identical across players/sessions. A
  player's "progress" is just the passwords they've collected; any fresh
  container works when they return. The spawner saves them for the player
  (`/save/<sid>` → localStorage) and checks them on resume. Release builds use
  the same passwords (secret `HANDOVER_CREDENTIALS`), so saves survive image
  deploys; rotating that secret resets every save ("doesn't match this
  server").
- **Whose box:** `/app/<sid>` and `/t/<sid>/` require the httponly `hid` cookie
  to match the URL sid, or a sign-in as the GitHub account that owns the box.
  Signed-in players get the same box on every device, and every connection
  attaches to the box's one tmux session (`play`), so devices share a screen;
  anonymous ones lose the box with their cookies (a new box, then resume).

## Hardening posture (break-tested)

Strong: unprivileged containers, no network device (no egress, no cloud
metadata), guest API off, fork-bomb/mem-bomb contained by cgroup limits, a 1.5 GiB
disk quota caps fill, 30 GiB pool is the host ceiling. Spawning gated by
Cloudflare Turnstile + per-IP rate limit (5/10 min) + hard 40-seat cap.
No inbound ports at all: SSH only through the tunnel behind Cloudflare Access,
key-only. (fail2ban still runs but sees only tunnel traffic from localhost,
which it ignores.)

## Parked / not done

- **SSH :22 IP allow-list** (OCI security list): deliberately skipped — operator
  IP rotates. Revisit only if SSH abuse becomes real (fail2ban covers noise).
- Low-severity residuals: none open that block anything.

## Check when player feedback arrives

- **Cross-session leaderboard time:** plumbing is in (spawner folds per-level
  solve time into a GitHub-keyed total, writes `/run/handover/playtime` in the
  container for the victory screen), but a real GitHub-login *finish* posting a
  sensible time to `/leaderboard` has not been eyeballed end-to-end. Verify on
  the first real completion.
- **Phone rendering in the wild:** narrow 32-col banners + landscape nudge +
  fontSize 13 are the mitigations; confirm against real devices/feedback.
- **Seat behaviour under real load** (squatting, cap at 20, 60-min reclaim).

## Change log

- **2026-10-20:** capacity measured (above); box creation serialised host-wide
  (`LAUNCH_LOCK`) after the test found Incus's uid-range race; CI runs
  `make mutate`; the browser save has tests; e2e waits have limits.

- **2026-10-19:** events table (`events` in the leaderboard DB: start, resume,
  reach, finish, end, cmd; no player identity) and command names from the
  image's bash hook; level times over 4 h stay out of the histogram; Grafana
  tunnel `oke-grafana` at ctf-monitor.
- **2026-10-19:** metrics for the OKE Prometheus (spawner `/metrics`,
  node-exporter, tunnel routes + Access app `handover-metrics`). Rotating the
  `oke-prometheus` token means re-encrypting `prometheus-cf-access` in
  `oke-gitops` from the new Terraform outputs.

- **2026-10-18** (deployed through the new pipelines unless marked):
  - Pipelines: image built/tested in bandit-wannabe CI and shipped by
    `deploy-image`; spawner deploy manual-only; Terraform on TFC with a scoped
    OCI user `tf-handover`; instance moved into compartment `handover`, state
    migrated, remote plan is a no-op.
  - Spawner: graceful moves onto a new image; a signed-in player's box
    follows their GitHub account across devices.
  - MOTD no longer shows a pager hint.
  - Host config is `host/site.yml` (applied by deploy.yml); tmux shared screen.
  - CI secrets from Terraform: service token `handover-ci-tf` and the tunnel
    token are written into the repos by `infra/github.tf`; the hand-made
    service token was deleted.
  - Cloudflare in Terraform: DNS, Access app + policies, Turnstile imported;
    a Terraform-made tunnel `handover-tf` replaced the file-configured one
    (switched with both up, then the old tunnel, its connector and the host's
    account-level `cert.pem` were removed).
  - SSH moved behind the tunnel + Cloudflare Access; public port 22 closed;
    CI uses the `handover-ci` service token (shared `tunnel-ssh` action).
  - Static-analysis pass (ruff, pylint, vulture, radon, actionlint, zizmor,
    checkov, jscpd): spawner refactor, actions pinned to SHAs, read-only
    workflow tokens. PR #1 (IMDS v2 only) awaits merge + terraform apply.
  - Resume from the browser save (see gotchas). Resumed runs set `unranked` in
    the leaderboard DB (column added on startup).
  - Progress check only counts su records with `_EXE=/usr/bin/su`. Before, any
    player could fake a reach with `logger -t su "session opened for user
    level20"` and climb the leaderboard. Rows written before this fix may be
    forged.
  - `bandit-wannabe` `0f3f868`: plain `mail` shows Dave's note directly
    (playtester got lost in the mailx prompt).

- **2026-10-17** (this session):
  - `bandit-wannabe` `9156bad`: removed the dead AWS/EC2/Terraform deploy path;
    docs now lead with the live OCI+Incus hosting (sign-in, leaderboard).
  - `handover-hosting` `496152b`: btrfs `games` pool + 2 GiB per-container disk
    quota; `security.guestapi=false` (no host-name leak).
  - `handover-hosting` `6b6dd09`: `IDLE_MIN` 240→60; fail2ban (sshd jail).
  - `handover-hosting` `f3b388b`: fixed container hostname `handover`; proxy
    cookie↔sid binding; disabled+masked rpcbind.
  - Earlier: phone-safe MOTD/victory, `go dave`/`go 21`, victory time format,
    spawner graceful-restart fix (`d90a3f0`), password-auth narrative wink.
