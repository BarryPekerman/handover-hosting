# infra — Terraform + cloud-init for the Handover host

Describes the OCI box as code: a VM.Standard.A1.Flex (4 OCPU / 24 GB) running
Ubuntu 24.04. `cloud-init.yaml.tftpl` (rendered by Terraform with the tunnel's
run token) updates packages and starts the tunnel connector on first boot, so
a new box is reachable through `handover-ssh` with port 22 closed; the host is
configured by `../host/site.yml`, which `deploy.yml` applies, followed by
bandit-wannabe's `deploy-image` for the game image. **No secrets and no game
build live here.**

Networking (VCN/subnet) is **referenced, not managed** (`subnet_ocid`), so
Terraform won't touch the existing network. Only the instance is managed.

## Setup

State and runs live in **Terraform Cloud**: org `berries-handover` (its own org,
so the repo's token can't reach anything else), project `handover`,
workspace `handover-infra` (remote execution, working directory `infra`).
Terraform authenticates as **`tf-handover`**, an API-key-only OCI user in group
`handover-terraform`. Its policy (`handover-terraform`) allows managing
instances and boot volumes in compartment **`handover`**, and using the
tenancy's one subnet. Nothing else: no IAM, no network changes, nothing outside
the compartment. Its key and the OCIDs are workspace variables, so nothing
here needs `~/.oci` or `~/.ssh`.

Runs go through GitHub (`.github/workflows/terraform.yml`):

- a PR touching `infra/` gets a speculative plan;
- `gh workflow run terraform.yml -R BarryPekerman/handover-hosting` plans and,
  if there are changes, applies. Starting it is the approval.

`terraform plan` from a laptop also works (`terraform login` first); it runs
remotely in the same workspace.

## Rebuild from scratch

What a newcomer (or a rebuild) needs. **Prerequisites are made by hand, once;
everything after them is code.**

### Prerequisites

| Provider | By hand | Notes |
|---|---|---|
| Cloudflare | Account, zone `barrypekerman.com`, Zero Trust enabled (team name + a payment method, even on Free), API token `terraform-handover` | Token: Account → Cloudflare Tunnel, Access: Apps and Policies, Access: Service Tokens, Turnstile (all Edit); Zone → DNS Edit, Zone Read; only this zone; ~1 year TTL |
| OCI | Tenancy; compartment `handover`; user `tf-handover` (API key only) in group `handover-terraform`; policy `handover-terraform` | Policy: manage instance-family, volume-family, security-lists in compartment handover; use vnics, use subnets in tenancy. The VCN/subnet (`Basic-VCN`) are referenced, not managed |
| Terraform Cloud | Org `berries-handover`, project + workspace `handover-infra` (remote execution, working directory `infra`, auto-apply off) | Workspace vars: `tenancy_ocid`, `user_ocid`, `fingerprint`, `private_key` (sensitive), `compartment_ocid`, `availability_domain`, `subnet_ocid`, `ssh_public_key`; env `CLOUDFLARE_API_TOKEN` and `GITHUB_TOKEN` (both sensitive); owners team token → `TF_API_TOKEN` in this repo |
| GitHub token for Terraform | Fine-grained PAT `terraform-handover-secrets`: Secrets read/write on handover-hosting + bandit-wannabe only | Every plan reads the secrets below, so renew it before it expires (2027-10-09) |
| GitHub | Both repos and the secrets Terraform doesn't write | handover-hosting: `TF_API_TOKEN`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`, `TURNSTILE_SECRET`, `GH_CLIENT_SECRET`, `SESSION_SECRET`, `BACKUP_PASSPHRASE`. bandit-wannabe: `HANDOVER_CREDENTIALS`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`. **Written by Terraform** (github.tf): `CF_ACCESS_CLIENT_ID/SECRET` in both, `CF_TUNNEL_TOKEN` in handover-hosting |

### Order

1. `gh workflow run terraform.yml` (or merge a PR touching `infra/`): the
   instance (cloud-init starts the tunnel connector from its token), the
   tunnel and its routes, DNS, Access, Turnstile, the security list.
2. `gh workflow run deploy.yml` — host config (`host/site.yml`) + spawner. CI
   reaches the box through `handover-ssh` with the service token; port 22
   stays closed. On a new box `DEPLOY_KNOWN_HOSTS` must be updated to its
   host key first (`ssh handover` from a laptop shows it).
3. `gh workflow run deploy-image.yml -R BarryPekerman/bandit-wannabe -f run_id=<latest passing ci on main>`.
4. Restore the leaderboard from the newest `backup.yml` artifact (docs/ops-notes.md).

`prevent_destroy` guards the instance, the tunnel, the `play` record and the
security list. On free-tier A1 a true rebuild means terminating the old box
first, which risks not getting the capacity back; do it knowingly.

## What's still manual / TODO

- A fresh box's first boot is untested end to end (cloud-init starts the
  tunnel connector from the token, so CI can reach it through handover-ssh);
  a rebuild drill would prove it.
- cloud-init wants one real boot to confirm ordering/timing.
