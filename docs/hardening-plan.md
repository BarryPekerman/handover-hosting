# Plan — making the hosted setup less fragile (2026-10-18)

From the fragility review. Cheapest and highest-leverage first. Each item says
what's done when it's done.

## 1. Credential blast radius

- [x] **1a. Own Terraform Cloud org for handover.** Done: org `berries-handover`. `TF_API_TOKEN` is an owners
  token for `Berries-`, which also holds `oci-oke-infra`. Move the
  `handover-infra` workspace to a new free org (new workspace, variables, a new
  `tf-handover` API key since the old private key isn't recoverable, state
  pushed), point `infra/main.tf` and the workflow at it, then revoke the old
  token and remove the old workspace and key.
  *Done when:* a PR plan and a dispatch run work against the new org, and the
  `Berries-` token is gone.
- [x] **1b. Per-repo deploy keys.** Done: deploy.yml and backup.yml run on their own key; deploy-image.yml's key passed an SSH test and gets its first real run at the next image deploy. Today both repos deploy with the host's
  admin key. Give each repo its own key (public halves managed by
  `host/site.yml`), pin the host key in a secret instead of `ssh-keyscan` on
  every run.
  *Done when:* both deploy workflows run on their own keys and the admin key is
  no longer in any repo's secrets.

## 2. Things that only exist on the host

- [x] **2a. Nightly backup** (done: `backup.yml`; restore tested) of the leaderboard DB (the tunnel files dropped out when the tunnel moved to Terraform):
  a scheduled workflow pulls them over SSH, encrypts them with a passphrase
  secret, and keeps them as a private artifact (30 days).
  *Done when:* a run produces an artifact and a restore of it is tested.
- [x] **2b. ~~Reserved public IP~~ — superseded:** SSH and CI go through the
  Cloudflare tunnel (`handover-ssh`, Access-protected); public port 22 is
  closed, so the IP no longer matters. Original note: in Terraform, so a stop/start can't change the
  address the deploy pipelines use. OCI can't convert an ephemeral IP, so the
  address changes once (SSH + deploys; the site is unaffected — the tunnel dials
  out).
  *Done when:* the IP is a Terraform resource and both `DEPLOY_HOST` secrets point at it.

## 3. Patching

- [x] **3. Kernel updates take effect.** Done: `handover-maybe-reboot.timer` (04:00 Israel time; reboots only if pending and 0 players). Original note: Unattended upgrades install kernels but
  never reboot. Either Ubuntu Livepatch (needs a free Ubuntu Pro token) or a
  playbook-managed timer that reboots at night only when a reboot is required
  and no one is playing (players resume afterwards).
  *Done when:* one of them is in `host/site.yml`.

## 4. Spawner safety net

- [x] **4a. Unit tests in CI** (done: `tests/test_spawner.py`, 13 tests) for the pure logic: `parse_save`, `solve_time`,
  `resolve_sid`/`owns`, `choose_start`, `outdated`.
- [x] **4b. Commit the VM regression harness** (done: `tests/vm/run.sh`) (fake `incus` that forwards to the
  Vagrant VM, plus the six end-to-end scenarios) as a manual test.

## 5. Smaller fixes

- [x] **5a. Disk overcommit:** quota is now 1.5 GiB. Correction (2026-10-19): the limit applies to *referenced* data incl. the ~0.95 GiB shared image, so the earlier 1 GiB left players only ~50 MiB, and 512 MiB stopped boxes from starting. Pool usage is the exclusive part (~50 MiB/box, ≤ ~560 MiB). 20 seats × 2 GiB quota = 40 GiB on a 30 GiB pool.
  Measure real per-container usage, then size the pool or the quota so they fit.
- [~] **5b. Leaderboard audit:** found a 0-second finish (straight to `dave`); runs that skip levels are now unranked (deployed). Old bogus row: pending decision. rows written before the `_EXE` fix may be forged.
- [x] **5c. One branch:** done; `local-rebuild` deleted, bandit-wannabe works on `main`.

## 7. Rebuildability

- [x] **7a. Tunnel at first boot.** Done: `infra/cloud-init.yaml.tftpl` starts `cloudflared-tf` from the token (untested until a rebuild drill). Original note: With port 22 closed, a fresh box can't be
  reached to install the tunnel. Store the tunnel's token as a sensitive TFC
  variable; cloud-init installs cloudflared and runs it, so CI can reach a new
  box straight away.
- [x] **7b. Cloudflare as code.** Done: DNS, Access app + policies, Turnstile imported; tunnel `handover-tf` made in Terraform and the old one removed. The CI service token and the secrets made from it are Terraform too (`github.tf`); the hand-made token is deleted. Original note: Tunnel, DNS records, the Access app, its two
  policies and the service token were created by hand; the Cloudflare
  Terraform provider (with an API token in TFC) would make a from-scratch
  rebuild `terraform apply` plus the deploys.
- [x] **7c. Rotate the `handover-ci` service token** (done; old secret verified refused) (its secret went through a
  chat), then set the new one in both repos from a terminal.

## 6. Monitoring (separate phase)

- [ ] **6. External uptime probe** from the OKE cluster (blackbox exporter against
  `/healthz` and the landing page). No access to the game tenancy needed. Logs
  and metrics from the host come after, as a deliberate decision to link the
  tenancies.
