# Ops Notes — The Handover (hosted)

A living handoff doc: current live state, what's been done, how to operate the
box, and what to check when player feedback arrives. Newest session section on
top. Not player-facing.

---

## System at a glance

- **Live URL:** https://play.barrypekerman.com (anonymous or GitHub sign-in).
- **Host:** OCI ARM (aarch64) instance, public IP `84.13.72.156`.
  Access: `ssh -i ~/.ssh/alex_handover ubuntu@84.13.72.156` (key-only; `ubuntu`
  is in `incus-admin`). This is a family member's separate OCI tenancy,
  isolated from the OKE cluster.
- **Serving path:** browser → Cloudflare (DNS is Cloudflare-only; origin IP not
  in DNS) → **Cloudflare Tunnel** (`cloudflared`, outbound) → spawner on
  `127.0.0.1:8080`. No inbound web port on the origin; only SSH :22 is open
  externally.
- **Spawner:** `spawner.py` (aiohttp), systemd unit `handover-spawner`
  (`/etc/systemd/system/handover-spawner.service`, runs as `ubuntu`,
  `ExecStart=/home/ubuntu/spawner.py`). One per-player **Incus** container
  (`p-<sid12>`) + its own loopback **ttyd**, reverse-proxied at `/t/<sid>/`.
- **Per-player container:** clone of the golden image `handover` on the
  `isolated` profile — no NIC, `security.guestapi=false`, CPU 50% / mem 512 MiB
  / procs 300, **2 GiB btrfs disk quota** (pool `games`, 30 GiB loop ceiling).
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
  (`alex_handover`) is in no repo.
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
H=84.13.72.156; K=~/.ssh/alex_handover
ssh -i $K ubuntu@$H                                   # get on the box

# health / who's playing
curl -s 127.0.0.1:8080/healthz                        # {"players":N,"max":20}
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
pushes to `master` only run `ci.yml`). `.github/workflows/deploy.yml`
copies `spawner/`, the unit and the tunnel config to the host, writes the
secrets env file from GitHub Secrets and restarts the spawner. Don't scp by
hand. The game image ships the same way from bandit-wannabe: a `ci` run on
`main` builds and tests it, then
`gh workflow run deploy-image.yml -R BarryPekerman/bandit-wannabe -f run_id=<id>`
imports it and moves the `handover` alias (previous kept as `handover-prev`).
Host setup changes go in `host/site.yml` (Ansible), never by hand: `deploy.yml`
applies it on every deploy. Dry run from a laptop:
`ansible-playbook -i 84.13.72.156, -u ubuntu --private-key ~/.ssh/alex_handover host/site.yml --check --diff`.

## Backups

`backup.yml` runs nightly (02:17 UTC): the leaderboard DB and `~/.cloudflared`,
encrypted with `BACKUP_PASSPHRASE` (a copy is in `~/handover-backup-passphrase.txt`
on the admin laptop), kept 30 days as the `handover-backup` artifact. Restore:

```bash
gh run download <run> -R BarryPekerman/handover-hosting -n handover-backup
gpg --pinentry-mode loopback --passphrase-file ~/handover-backup-passphrase.txt \
  -d handover-backup-*.tgz.gpg | tar xzf - -C restore/
scp restore/handover-leaderboard.db ubuntu@$H:~/          # spawner stopped
scp -r restore/cloudflared/. ubuntu@$H:~/.cloudflared/
```

The live level passwords are not in it (they're the `HANDOVER_CREDENTIALS`
secret; readable copy: `~/handover-live-credentials.tgz` on the admin laptop).

## Known gotchas / non-bugs (seen this session)

- **"I dropped into level10":** was a misread of `level0@<hostname>`. Prompt now
  shows `level0@handover` (fixed hostname) to kill the ambiguity. Accounts are
  un-padded: `level0, level1, … level10, … level20` (no `level01`); they sort
  lexically so `level10` lists right under `level1`.
- **Restarting the spawner resets a live player's terminal** to a fresh login
  (ttyd is killed + respawned; the container survives via re-adopt). Brief blip,
  not data loss.
- **Idle reaper = 60 min** (`IDLE_MIN`), max lifetime 1440 min. Idle containers
  disappear; a returning player gets a fresh container (and can resume).
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
metadata), guest API off, fork-bomb/mem-bomb contained by cgroup limits, 2 GiB
disk quota caps fill, 30 GiB pool is the host ceiling. Spawning gated by
Cloudflare Turnstile + per-IP rate limit (5/10 min) + hard 20-seat cap.
fail2ban on sshd. Host SSH is key-only.

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

- **2026-10-18** (deployed through the new pipelines unless marked):
  - Pipelines: image built/tested in bandit-wannabe CI and shipped by
    `deploy-image`; spawner deploy manual-only; Terraform on TFC with a scoped
    OCI user `tf-handover`; instance moved into compartment `handover`, state
    migrated, remote plan is a no-op.
  - Spawner: graceful moves onto a new image; a signed-in player's box
    follows their GitHub account across devices.
  - MOTD no longer shows a pager hint.
  - Host config is `host/site.yml` (applied by deploy.yml); tmux shared screen.
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
