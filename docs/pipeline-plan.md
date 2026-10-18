# Plan — build once, deploy what was tested

Goal: every change to the box reaches it through a pipeline you can watch in
GitHub (or Terraform Cloud), and what ships is exactly what was tested. Nothing
is built or hand-edited on the host.

## Where things stand (2026-10-18)

| Layer | What it is | How it changes today |
|---|---|---|
| Infra | OCI A1 instance (Terraform, `infra/`) | By hand. Local state on the laptop (serial 1, instance imported). |
| Host config | Incus, `games` pool, `isolated` profile, fail2ban, sysctls, cloudflared, ttyd, rpcbind masked | By hand. `cloud-init.yaml` mirrors it but only runs at first boot, and it clones `bandit-wannabe` anonymously, which fails now that the repo is private. |
| Spawner | `spawner/`, unit, tunnel config | **CD**: push to `master` → `deploy.yml`. |
| Game image | `handover` Incus image, 269 MiB, arm64 | CI builds + tests an *x86* copy and throws it away; the live arm64 image is built by hand on the host. `incus-image.sh build` publishes to the live alias **before** solve/leak tests run. |

Passwords are generated at build time into `credentials/` (21 `*_pass` + 5
decoy files), which exists only on the host. Resume depends on them staying the
same across builds.

## Phase 1 — game image pipeline (bandit-wannabe) ✅ done 2026-10-18 (except step 5)

1. **Passwords into a GitHub secret.** Copy the host's existing `credentials/`
   (tar + base64) into the repo secret `HANDOVER_CREDENTIALS`, so live players'
   saves and notes stay valid. Keep an offline backup. Changing it later is a
   deliberate rotation that resets every save.
2. **Build on ARM in CI.** `build-and-solve` runs on `ubuntu-24.04-arm`
   (standard ARM runners work in private repos since Jan 2026; confirm on the
   first run, and note it uses Actions minutes, ~10 min per run).
   - Release builds (push to `main`, manual dispatch) restore the real
     credentials from the secret.
   - Other builds (PRs, branches) keep generating throwaway credentials.
3. **Test before publish.** Build → solve + leak scan + interactive smoke on a
   throwaway copy → only then `incus image export` → upload as a private
   workflow artifact (7-day retention).
4. **Deploy job** (needs the build; `main` only; GitHub environment
   `production`):
   - download the artifact, copy it to the host, `incus image import` under
     `handover-<sha>`;
   - move the `handover` alias to it and keep the previous one as
     `handover-prev`, which makes rollback a one-line alias swap. Prune older
     images.
   - Running players keep their containers; new players get the new image.
   - The full log lives in the Actions run.
5. **Host cleanup.** Remove `~/bandit-wannabe` and its `credentials/` from the
   host once the first pipeline deploy has succeeded.
6. Add the `DEPLOY_HOST` / `DEPLOY_SSH_KEY` secrets to bandit-wannabe (same
   values as handover-hosting).

## Phase 2 — Terraform on Terraform Cloud (handover-hosting `infra/`) — in progress

Done: TFC project/workspace + variables, `tf-handover` user/group/policy,
compartment `handover`, `cloud {}` backend, `terraform.yml` (PR plan, manual
apply). Driven from GitHub Actions with a team token, so no VCS connection.
Left: move the instance into `handover`, migrate the state, confirm a no-op
plan, delete the local state.

1. **Workspace.** TFC org + workspace `handover-infra`, `cloud {}` block in
   `main.tf`, remote execution, auto-apply **off**.
2. **API-driven from GitHub Actions** (`terraform.yml`): a PR touching
   `infra/` gets a speculative plan; `gh workflow run` plans and applies.
3. **Provider auth without the laptop.** Replace `config_file_profile` with
   explicit `tenancy_ocid` / `user_ocid` / `fingerprint` / `private_key` /
   `region` variables, set as sensitive workspace variables. Use a dedicated OCI
   API user with a policy scoped to the compartment, not the personal ALEX key.
4. **No file paths.** `ssh_authorized_keys` becomes a string variable instead
   of `file(...)`; the remote runner has no `~/.ssh`.
5. **Migrate state.** One-time `terraform init -migrate-state` from the laptop,
   check that the plan is a no-op, then delete the local `terraform.tfstate`.
6. **Guards stay.** `prevent_destroy` + `ignore_changes` already stop a plan
   from replacing the live box.

## Phase 3 — host config as code (the real gap)

1. **Ansible playbook** `host/site.yml` in handover-hosting describing
   everything done by hand: Incus init, `games` btrfs pool (30 GiB), `isolated`
   profile (no NIC, `security.guestapi=false`, CPU/mem/procs/disk limits),
   inotify sysctl, fail2ban sshd jail, rpcbind masked, ttyd, cloudflared, the
   spawner unit and user.
2. **Reconcile with the live box.** First run `--check --diff` against the
   host and adjust the playbook until it reports no changes. Then the playbook
   describes reality.
3. **CD.** `deploy.yml` runs it on changes to `host/**` (before the spawner
   copy), so host changes go through review and leave a log.
4. **cloud-init shrinks** to bootstrap only (user, key, python). The playbook
   does the rest, and Phase 1 delivers the game image. This also removes the
   broken anonymous clone.

## Later

- Monitoring (the OKE idea): push-only from the host; containers stay NIC-less.
- Rebuild drill: terraform apply → host playbook → image deploy → spawner
  deploy, on a scratch instance, once capacity allows.

## Decisions (2026-10-18)

1. **Branch.** Deploy from `main`.
2. **Approval.** Yes. GitHub Free has no required reviewers on private repos, so deploys are `workflow_dispatch` only; starting one (`gh workflow run`) is the approval.
3. **TFC.** Existing org `Berries-`; new project for this.
4. **First deploy.** The mail fix + resume ship as the pipeline's first deploy.
