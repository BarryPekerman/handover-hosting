# infra — Terraform + cloud-init for the Handover host

Describes the OCI box as code: a VM.Standard.A1.Flex (4 OCPU / 24 GB) running
Ubuntu 24.04. `cloud-init.yaml` only updates packages on first boot; the host
is configured by `../host/site.yml`, which `deploy.yml` applies, followed by
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

## Two paths

**Adopt the running box (non-destructive, recommended).** The live instance
already exists, so import it instead of creating a second one (free A1 has no
room for two):

```sh
terraform import oci_core_instance.handover \
  ocid1.instance.oc1.il-jerusalem-1.anwxiljrpmn4b2ycg5eobj62v2cfrwi4nzfwvavhvh6opstdam4sfvbqmywa
terraform plan   # should be ~no-op; `prevent_destroy` guards the box
```

After import, Terraform manages the box in place. `prevent_destroy` +
`ignore_changes` mean a newer base image or an edited cloud-init will **not**
replace a running instance — rebuilds are deliberate.

**Rebuild from scratch (deliberate, capacity-sensitive).** On the free tier the
4/24 allocation is fully used by the running box, so a true rebuild is
terminate-then-apply, which risks not getting the capacity back. Only do this
knowingly:

```sh
# (optional) terraform state rm, terminate the old box, then:
terraform apply    # creates the instance; cloud-init provisions it
```

Then configure the host and deliver the app, secrets and game image (in this
order: the image needs Incus):

```sh
gh workflow run deploy.yml -R BarryPekerman/handover-hosting
gh workflow run deploy-image.yml -R BarryPekerman/bandit-wannabe -f run_id=<latest passing ci run on main>
```

## What's still manual / TODO

- **Tunnel credentials** for a fresh box: either reuse the existing tunnel's
  creds (add to GitHub Secrets and have CD write them) or create a new tunnel.
- cloud-init wants one real boot to confirm ordering/timing.
