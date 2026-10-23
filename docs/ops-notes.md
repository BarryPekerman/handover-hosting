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
  security list. Key-only; `ubuntu` is in `incus-admin` (admin and CI only;
  the spawner is not `ubuntu`, see Spawner).
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
  `metrics.py`, `gamedata.py` (aiohttp; code in `/opt/handover`, owned by
  `ubuntu`; its pages are `/opt/handover/pages/*.html`, read at startup, so a
  text edit needs a deploy/restart), systemd unit `handover-spawner`
  (`/etc/systemd/system/handover-spawner.service`). It runs as **`handover`**,
  a system user in no incus group, sandboxed by systemd (read-only system,
  no home dirs, no privileges; `systemd-analyze security handover-spawner`:
  2.1). Its home `/var/lib/handover` holds the state file, the leaderboard and
  its Incus client config. It reaches Incus over `https://127.0.0.1:8443` with
  a certificate trusted for the **`handover` project only** (`incus config
  trust list`: `handover-spawner`), and the project is `restricted`, so Incus
  refuses privileged, nested or shared-idmap boxes, non-pool disks, NICs,
  proxy/unix devices, `raw.lxc` and any server or project change. Player
  boxes live in that project (`incus list --project handover`); images stay in
  the default project, read-only from there. One per-player **Incus**
  container (`p-<sid12>`) + its own loopback **ttyd**, reverse-proxied at
  `/t/<sid>/`.
- **Per-player container:** clone of the golden image `handover` on the
  `isolated` profile — no NIC, `security.guestapi=false`, CPU 50% / mem 320 MiB
  / procs 300, **1400 MiB btrfs disk quota** (counts the shared ~0.95 GiB base
  image, so ~420 MiB writable; pool `games`, 30 GiB loop ceiling),
  own uid range per box (`security.idmap.isolated`). Up to **60 players**
  (`MAX_PLAYERS`); measured idle box: 1.4% of a core, 138 MiB RAM, 46 MiB disk.
- **Standby boxes** (`WARM_SPARES=3`): three boxes (`w-<random>`) are kept
  started from the live image, so a player gets one at once instead of
  waiting ~5 s per box created ahead of them; each handout starts a
  replacement. Fresh starts and resumes both use them (a resume checks its
  saved passwords against whichever box it gets). They count toward the 60
  boxes (with 58 players only 2 are kept, at 60 none), carry no events until
  handed out, are replaced after an image deploy (the reaper checks every
  30 s), and are deleted and rebuilt on a spawner restart. `handover_spares`
  in /metrics. To turn them off: `WARM_SPARES=0` in the unit, redeploy.
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
  in the cluster; tunnel, DNS and Access live in `oci-oke-infra` `terraform/grafana.tf`,
  with the cluster, since 2026-10-20; Access: your email). Counters restart with the spawner; a
  box's counted levels are kept in the state file, so a restart doesn't count
  them twice.
- **Secrets:** `/etc/handover/spawner.env` (root-only; systemd reads it before
  starting the spawner as `handover`):
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
curl -s 127.0.0.1:8080/healthz                        # {"players":N,"max":60}
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

## Capacity: 60 players (measured 2026-10-20)

`MAX_PLAYERS=60` is the limit from now on. The rule behind it: even if every
player deliberately fills their box to its limits, the host (23.4 GiB, no swap,
4 cores) can't run out of memory or disk. If it ran out of memory the kernel
would start killing processes, the spawner among them, and everyone would be
dropped. So the per-box limits are sized for 60:

| | per box | 60 boxes at the limit | measured (whole game in one box) |
|---|---|---|---|
| memory (`limits.memory`) | 320 MiB | 18.75 GiB of 23.4 | peak 231 MiB when squeezed to 256 MiB, nothing killed |
| disk (quota 1400 MiB, incl. the ~0.95 GiB image) | ~420 MiB writable | ~26 GiB of the 30 GiB pool | 93 MiB written |

**One box, the whole game** (`scripts/footprint.sh`: every level's intended
solution run as that level's user, 20/20 solved):
- Booted: 135 MiB; with a player logged in through tmux: 155 MiB.
- Memory climbs by level in this test: at the old 512 MiB limit it ended at
  444 MiB, but 256 MiB of that was file cache the kernel can drop. Real
  memory was ~154 MiB (anon 92 + kernel 62). Part of the climb is each
  login's `systemd --user` manager (~13 MiB with its helper). It exits ~10 s
  after that login ends (logind's stop delay), and the test logs in to the
  next level faster than that, so it overstates a real player, whose
  managers live only while those logins are open. Masking `user@` or
  dropping `pam_systemd` was tried in a box and rejected: it logs errors, and
  `pam_systemd` provides the per-user slice limits.
- Squeezed to 256 MiB: still 20/20, peak 231 MiB, `oom_kill 0`, but the
  kernel freed cache 428 times (`memory.events high`). Too tight to be the
  limit; 320 MiB leaves ~90 MiB over that peak.

**Many boxes** (`scripts/capacity.sh`, at the old 512 MiB profile; boxes in
bursts of 10, each with a player logged in through tmux). The run was aimed
at 80 and stopped at 66 once 60 was decided:

| boxes | slowest to be ready | of which booting | free memory | boxes' memory | pool | load (1 min) | spawner |
|---|---|---|---|---|---|---|---|
| 10 | 48 s | 4 s | 21.5 GiB | 1.5 GiB | 2.7 GiB | 2.4 | 1.0 ms |
| 20 | 68 s | 5 s | 20.8 GiB | 3.0 GiB | 3.7 GiB | 8.0 | 1.0 ms |
| 30 | 97 s | 6 s | 20.0 GiB | 4.5 GiB | 4.5 GiB | 12.3 | 1.1 ms |
| 40 | 128 s | 7 s | 19.2 GiB | 6.0 GiB | 5.3 GiB | 25.7 | 0.9 ms |
| 50 | 165 s | 11 s | 18.4 GiB | 7.5 GiB | 6.3 GiB | 34.6 | 0.9 ms |
| 60 | 206 s | 11 s | 17.5 GiB | 9.0 GiB | 7.1 GiB | 37.7 | 1.5 ms |

An earlier run to 40: all 40 boxes searching the filesystem at once took 6 s,
and the spawner answered `/healthz` within 22 ms throughout.

- **Memory and disk aren't the limit:** about 150 MiB and 50-90 MiB per box in
  use; the profile limits are what cap it at 60.
- **CPU is fine for playing** (an idle box: 1.4% of a core) but **starting
  boxes is slow**. Boxes are created one at a time (`LAUNCH_LOCK`, below),
  about 4.5 s each on a quiet host, then boot in parallel (2-11 s). So the
  Nth player of a burst waits about 4.5 x N s: 8 at once were all ready in
  37 s. It's slower when the host is busy: right after many boxes were
  deleted (btrfs cleans them up in the background) one create took 7-15 s,
  and with 50-60 boxes running the 10th of a burst waited ~3 min (table).
  Nobody fails; the spinner waits. Per-box uid ranges aren't the cost: a box
  without one (`security.idmap.isolated=false`) was no faster (4.8 s vs
  4.5 s), as Incus re-owns the image's files either way. Standby boxes
  (above, added after this run) take the first 3 of any burst off the queue.
- 80 would fit at 256 MiB per box, but that's the squeezed case above: a
  player opening more than the game needs could get processes in their own
  box killed.
- The runs show on the Grafana host panels (node-exporter) on 2026-10-20,
  11:55-12:35 and 14:25-15:05 UTC; nothing reached the leaderboard or the
  player metrics.

Rerun both after any change to the profile, the image size or the host
shape, while nobody plays (they refuse otherwise): see `tests/README.md`.

## Reboots

Kernel updates install unattended; `handover-maybe-reboot.timer` (04:00 Israel
time) reboots when `/run/reboot-required` exists and `/healthz` says 0
players, otherwise it waits for the next night, but at most 72 h (counted from
when the script first saw it, `/run/handover-reboot-pending-since`): then it
writes a 10-minute warning into every open terminal and reboots anyway. Decision without rebooting:
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
scp restore/handover-leaderboard.db handover:/tmp/       # then, spawner stopped:
ssh handover 'sudo install -o handover -g handover -m 600 /tmp/handover-leaderboard.db /var/lib/handover/ && rm /tmp/handover-leaderboard.db'
```

The live level passwords are not in it (they're the `HANDOVER_CREDENTIALS`
secret; readable copy: `~/handover-live-credentials.tgz` on the admin laptop).

## Gameplay data and closing the game

Players are told on the landing and story pages that their session is recorded:
every command line (capped at 200 characters) with its exit status, wrong
passwords, and when each level is reached, under a random player id (`pid`
cookie; linked to the GitHub account for signed-in players). No IPs are kept.
It all lands in the `events` table of `/var/lib/handover/handover-leaderboard.db` (nightly
backup). The promise: publish only totals and averages, then delete the raw data.

While the game runs, the Grafana dashboard **The Handover — players** shows it
per player: the spawner serves `/data/players`, `/data/levels` and
`/data/player?pid=` (JSON) on the metrics port only, which the `handover-metrics`
tunnel route exposes behind the `oke-prometheus` Access token; Grafana's
Infinity datasource reads them with that token. The numbers come from
`spawner/gamedata.py`, the same code `scripts/results.py` uses, so the live view
and the published breakdown agree.

When the game closes:

1. `sudo python3 scripts/results.py /var/lib/handover/handover-leaderboard.db > results.md` (on the
   host, or on a restored backup). It prints totals and averages only, with no
   player ids or command lines, so it can be published as is.
2. Delete the raw records: `sudo -u handover sqlite3 /var/lib/handover/handover-leaderboard.db 'DELETE FROM events; DELETE FROM player_ids; VACUUM;'`,
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

Strong: the spawner and every ttyd (the processes that read player traffic)
run as `handover`, not an Incus admin: Incus trusts its certificate for the
restricted `handover` project only, so a bug in them reaches player boxes,
not the host (break-tested on Incus 6.0.0, 2026-10-23; see Spawner).
Kernel surface: no user namespaces inside a box
(`linux.sysctl.user.max_user_namespaces=0`), io_uring only for host root
(`kernel.io_uring_disabled=1`), unprivileged bpf and userfaultfd off (pinned
in `/etc/sysctl.d/99-handover.conf`). Not done: denying `keyctl`/`add_key`/
`request_key` with `security.syscalls.deny` had no effect on Incus 6.0.0 in
testing (the calls still ran), and `raw.seccomp` is barred by the restricted
project on purpose; box root still has keyrings.
Unprivileged containers, no network device (no egress, no cloud
metadata), guest API off, fork-bomb/mem-bomb contained by cgroup limits, a 1400 MiB
disk quota caps fill, 30 GiB pool is the host ceiling. Spawning gated by
Cloudflare Turnstile (the spawner refuses to start without its secret) +
per-IP rate limit (5/10 min; IPv6 counted per /64) + hard 60-seat cap. The
terminal WebSocket only opens from the game's own page (Origin check: every
`*.barrypekerman.com` is the same site for the SameSite=Lax cookies); the id
cookies are `Secure`.
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

- **2026-10-23:** kernel surface for players: no user namespaces in boxes,
  io_uring for host root only, bpf/userfaultfd sysctls pinned (see Hardening
  posture). The game solved end to end with them on; running boxes pick up
  the profile change when they're next created.

- **2026-10-23:** the spawner runs as `handover` with a certificate restricted
  to the new `handover` Incus project, sandboxed by systemd, instead of as
  `ubuntu` (in `incus-admin`, root-equivalent). Code moved to `/opt/handover`,
  state and leaderboard to `/var/lib/handover`, secrets to
  `/etc/handover/spawner.env`. The first deploy moves the leaderboard and
  deletes the boxes in the default project (players resume from their saves);
  it leaves `/home/ubuntu/handover-leaderboard.db.migrated`: **delete it once
  the new leaderboard is checked** (it holds raw player records).

- **2026-10-20:** standby boxes (`WARM_SPARES=3`): players get a started box
  at once; see System at a glance.
- **2026-10-20:** limit raised to **60 players**: `MAX_PLAYERS=60`, box
  memory 512 -> 320 MiB, disk quota 1536 -> 1400 MiB (Capacity, above).
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
