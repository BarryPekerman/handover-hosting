# infra — Terraform + cloud-init for the Handover host

Describes the OCI box as code: a VM.Standard.A1.Flex (4 OCPU / 24 GB) running
Ubuntu 24.04, provisioned on first boot by `cloud-init.yaml` (Incus, the OCI
bridge-egress fix, the `isolated` profile, cloudflared, and the game image built
from the public `bandit-wannabe` repo). **No secrets live here** — the spawner,
tunnel credentials, and Turnstile secret arrive afterwards via the CD workflow
(`../.github/workflows/deploy.yml`), so nothing sensitive is baked into state,
the image, or user_data.

Networking (VCN/subnet) is **referenced, not managed** (`subnet_ocid`), so
Terraform won't touch the existing network. Only the instance is managed.

## Setup

```sh
cd infra
cp terraform.tfvars.example terraform.tfvars   # values are prefilled
export SUPPRESS_LABEL_WARNING=True
terraform init
terraform plan                                  # read-only; review first
```

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

Then deliver the app + secrets:

```sh
gh workflow run deploy.yml --repo BarryPekerman/handover-hosting
```

## What's still manual / TODO

- **Tunnel credentials** for a fresh box: either reuse the existing tunnel's
  creds (add to GitHub Secrets and have CD write them) or create a new tunnel.
- **Remote state**: move off local state (OCI Object Storage or TF Cloud) before
  this is more than one person.
- cloud-init wants one real boot to confirm ordering/timing.
