# Operations — The Handover (hosted)

How the hosted game is run: what the box looks like, how changes ship, what
was measured, and the decisions behind the limits. The operator's runbook
(admin access, break-glass, exact restore commands) is kept in the private
game repo, not here.

---

## System at a glance

- **Serving path:** browser → Cloudflare (DNS is Cloudflare-only; the origin
  IP is not in DNS) → **Cloudflare Tunnel** (outbound from the host) → spawner
  on `127.0.0.1:8080`. The tunnel and its routes are Terraform
  (`infra/tunnel.tf`); the host holds only the run token. Nothing is open
  inbound on the origin, SSH included: SSH goes through the tunnel behind
  Cloudflare Access, key-only.
- **Spawner:** `spawner.py` plus `common.py`, `auth.py`, `metrics.py`,
  `gamedata.py` (aiohttp), systemd unit `handover-spawner`. It runs as
  **`handover`**, a system user in no incus group, sandboxed by systemd
  (read-only system, no home dirs, no privileges; `systemd-analyze security
  handover-spawner`: 2.1). It reaches Incus with a certificate trusted for
  the **`handover` project only**, and the project is `restricted`, so Incus
  refuses privileged, nested or shared-idmap boxes, non-pool disks, NICs,
  proxy/unix devices, `raw.lxc` and any server or project change. One
  per-player **Incus** container (`p-<sid12>`) plus its own loopback
  **ttyd**, reverse-proxied at `/t/<sid>/`.
- **Per-player container:** clone of the golden image `handover` on the
  `isolated` profile — no NIC, `security.guestapi=false`, CPU 50% / mem 320 MiB
  / procs 300, **1400 MiB btrfs disk quota** (counts the shared ~0.95 GiB base
  image, so ~420 MiB writable; pool `games`, 30 GiB ceiling), own uid range
  per box (`security.idmap.isolated`). Up to **60 players** (`MAX_PLAYERS`);
  measured idle box: 1.4% of a core, 138 MiB RAM, 46 MiB disk.
- **Standby boxes** (`WARM_SPARES=3`): three boxes are kept started from the
  live image, so a player gets one at once instead of waiting ~5 s per box
  created ahead of them; each handout starts a replacement. They count toward
  the 60 boxes, are replaced after an image deploy, and are rebuilt on a
  spawner restart.
- **Monitoring:** Prometheus in the OKE cluster
  ([oci-oke-infra](https://github.com/BarryPekerman/oci-oke-infra)) scrapes
  the spawner's `/metrics` (players, seats, box starts/teardowns/resumes, the
  level funnel, time per level, finishes) and node-exporter through tunnel
  routes that only its Access service token can use (`infra/metrics.tf`).
  Alerts go to Telegram.
- **Secrets** live in a root-only env file on the host, written by CD from
  GitHub Secrets. Nothing sensitive is in git or the image.

## How changes ship

- **Spawner and host config** (`spawner/`, `host/`): `auto-deploy.yml` runs
  when `ci` passes on `main` and every night. If `spawner/` or `host/` changed
  since the last successful deploy, it waits until `/healthz` shows 0 players
  (up to 5.5 h; nobody is ever kicked) and then runs `deploy.yml`. If players
  stay on the whole time, the run fails and the next night retries.
  `gh workflow run deploy.yml` deploys by hand.
- **`deploy.yml`** applies `host/site.yml` (Ansible), copies the spawner,
  writes the secrets and restarts it. It refuses a commit that isn't on `main`
  with a passing `ci` run, and fails if the spawner doesn't come back healthy,
  logs an error since the restart, or doesn't answer through the tunnel.
- **Game image:** built and tested by the game repo's CI, shipped by its
  manual `deploy-image` workflow, which moves the `handover` alias (the
  previous image stays as `handover-prev` for rollback).
- **Infrastructure:** `terraform.yml`, on Terraform Cloud.

## Capacity: 60 players (measured 2026-10-20)

`MAX_PLAYERS=60` is the limit. The rule behind it: even if every player
deliberately fills their box to its limits, the host (23.4 GiB, no swap, 4
cores) can't run out of memory or disk. If it ran out of memory the kernel
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

Rerun both after any change to the profile, the image size or the host
shape, while nobody plays (they refuse otherwise): see `tests/README.md`.

## Reboots

Kernel updates install unattended; `handover-maybe-reboot.timer` (04:00 Israel
time) reboots when `/run/reboot-required` exists and `/healthz` says 0
players, otherwise it waits for the next night, but at most 72 h: then it
writes a 10-minute warning into every open terminal and reboots anyway.
After a reboot, player containers are gone (they don't autostart); the
spawner drops their sessions and players resume from their saves.

## Backups

`backup.yml` runs nightly: the leaderboard DB, encrypted with a random
passphrase, uploaded to a private OCI bucket that deletes each backup after
30 days. CI writes as `handover-backup` (`infra-iam`), which can add and read
objects but not delete or overwrite them, so a leaked CI key can't destroy
the backups.

`restore-test.yml` runs monthly (and by hand): it decrypts the newest backup
and opens the DB (integrity check, the three tables, row counts). Red if the
newest is older than 36 h or anything fails.

Restoring is a manual admin step: download the newest object, decrypt it,
and install the DB into the spawner's state directory with the spawner
stopped. The exact commands are in the private runbook.

The live level passwords are not in the backup; they're a CI secret of the
game repo.

## Credentials that expire

`credential-expiry.yml` runs on the 1st of each month and goes red 45 days
before any of these (dates in the workflow, conservative: made + 1 year):

| Expires | Credential | Breaks | Rotate |
|---|---|---|---|
| 2027-10-09 | GitHub fine-grained PAT, HCP workspace variable `GITHUB_TOKEN` | every Terraform plan | New PAT (Secrets read/write on handover-hosting and the-handover-linux-ctf), paste it into the workspace variable |
| 2027-10-18 | Access service token `handover-ci-tf` | deploy, backup, rebuild (CI can't SSH through Access) | A Terraform run replacing `cloudflare_zero_trust_access_service_token.ci` (HCP: Start new run, Replace). `infra/github.tf` writes the new id and secret into both repos' Actions secrets |
| 2027-10-19 | Access service token `oke-prometheus` | scraping and Grafana's player view; both Handover "down" alerts fire while the host is fine | A Terraform run replacing `cloudflare_zero_trust_access_service_token.prometheus`; read outputs `prometheus_access_client_id`/`_secret`, put them in oke-gitops `monitoring/manifests/prometheus-cf-access.enc.yaml` (SOPS), merge, then restart Grafana (it reads them as env at start) |

After rotating, set the new date in `.github/workflows/credential-expiry.yml`.

## Gameplay data and closing the game

Players are told on the landing and story pages that their session is recorded:
every command line (capped at 200 characters) with its exit status, wrong
passwords, and when each level is reached, under a random player id (linked
to the GitHub account for signed-in players). No IPs are kept. It all lands
in the leaderboard DB's `events` table. The promise: publish only totals and
averages, then delete the raw data.

The numbers come from `spawner/gamedata.py`, the same code `scripts/results.py`
uses, so the live Grafana view and the published breakdown agree.

When the game closes:

1. `scripts/results.py <leaderboard db> > results.md` (on the host, or on a
   restored backup). It prints totals and averages only, with no player ids
   or command lines, so it can be published as is.
2. Delete the raw records (`DELETE FROM events; DELETE FROM player_ids;
   VACUUM;`), disable the `backup` workflow, and empty the backup bucket as
   the admin (CI can't delete), or the raw records outlive the promise on the
   story page by up to 30 days.

## Known gotchas

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
  start (2026-10-19: 512 MiB did).
- **Players can't write files over 10 MiB** (`fsize` limit, security role) —
  by design; "File size limit exceeded" is that, not the disk quota.
- **Restarting the spawner resets a live player's terminal** to a fresh login
  (ttyd is killed and respawned; the container survives via re-adopt). Brief
  blip, not data loss.
- **Idle reaper = 15 min** (`IDLE_MIN`), max lifetime 24 h. Idle = no
  keystrokes. A returning player gets a fresh container and resumes from
  their save. Short so drive-by visitors don't hold seats for an hour.
- **After an image deploy** boxes on the old image are recycled once idle for
  5 min (`OUTDATED_IDLE_MIN`); active players move over on their next reload
  or Play, at the level they reached.
- **One shared password set:** every container is a clone of the same golden
  image, so all level passwords are identical across players. A player's
  progress is the passwords they've collected; the spawner saves them for
  the player (`/save/<sid>` → localStorage) and checks them on resume. Release
  builds use the same passwords, so saves survive image deploys; rotating
  them resets every save.
- **Whose box:** `/app/<sid>` and `/t/<sid>/` require the httponly `hid`
  cookie to match the URL sid, or a sign-in as the GitHub account that owns
  the box. Signed-in players get the same box on every device, sharing one
  tmux session.
- **Forged progress:** the progress check only counts `su` records with
  `_EXE=/usr/bin/su`. Before that fix (2026-10-18), any player could fake a
  reach with `logger -t su "session opened for user level20"`.

## Hardening posture (break-tested)

The spawner and every ttyd (the processes that read player traffic) run as
`handover`, not an Incus admin: Incus trusts its certificate for the
restricted `handover` project only, so a bug in them reaches player boxes,
not the host (break-tested on Incus 6.0.0, 2026-10-23).

Kernel surface: no user namespaces inside a box
(`linux.sysctl.user.max_user_namespaces=0`), io_uring only for host root
(`kernel.io_uring_disabled=1`), unprivileged bpf and userfaultfd off. Not
done: denying `keyctl`/`add_key`/`request_key` with `security.syscalls.deny`
had no effect on Incus 6.0.0 in testing (the calls still ran), and
`raw.seccomp` is barred by the restricted project on purpose; box root still
has keyrings.

Unprivileged containers, no network device (no egress, no cloud metadata),
guest API off, fork-bomb/mem-bomb contained by cgroup limits, a 1400 MiB disk
quota caps fill, the 30 GiB pool is the host ceiling. Spawning is gated by
Cloudflare Turnstile (the spawner refuses to start without its secret), a
per-IP rate limit (5/10 min; IPv6 counted per /64) and a hard 60-seat cap.
The terminal WebSocket only opens from the game's own page (Origin check);
the id cookies are `Secure`. No inbound ports at all.

## Not done, by choice

- **One VM.** Everything runs on one OCI Always Free A1 instance: when it or
  its tunnel hiccups, the game is down (seen 2026-10-23: a 502 for a few
  seconds). A second host would double the free quota this uses and need
  shared state (sessions, leaderboard). The answer is to see outages, not
  prevent them: a public probe of `/healthz` and `rebuild.yml` (rehearsed:
  about 10 minutes from nothing).
