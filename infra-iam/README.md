# infra-iam — the OCI identities the Handover runs as

Creates, in the tenancy's root:

| Object | What it may do |
|---|---|
| compartment `handover` | holds everything of the Handover |
| user `tf-handover` + group `handover-terraform` + policy | Terraform Cloud's runs of `../infra`: instances, boot volumes, security lists and buckets in `handover`; use the tenancy's subnet; read the Object Storage namespace. No IAM |
| user `handover-images-ci` + group + policy | the game repo's CI: objects in the `handover-images` bucket, nothing else |
| user `handover-backup` + group + policy | handover-hosting's `backup.yml` and `restore-test.yml`: create, list and read objects in the `handover-backups` bucket; no delete, no overwrite |
| policy `handover-objectstorage-lifecycle` | Object Storage itself may delete expired objects in `handover-backups` (its 30-day lifecycle rule) |

All three users are API-key only: no console password, auth tokens, DB or OAuth2
client credentials, or other credentials.

**Run by an admin from a laptop, never from CI.** This is the one place admin
rights are used, so the admin key stays on the admin's disk: the provider reads
a profile from `~/.oci/config`, and the Terraform Cloud workspace
`handover-iam` holds only the state (execution mode **Local**). Everything in
`../infra` then runs as the narrowly scoped `tf-handover`.

## Setup (once)

1. An admin API key in `~/.oci/config` (any profile name), and the `oci` CLI
   (two capabilities the provider can't set go through it; `iam.tf`).
2. Terraform Cloud: workspace `handover-iam` in org `berries-handover`, project
   `handover`, execution mode **Local**; `terraform login`.
3. The CI key pair. The private half never enters the repo or the state:
   ```
   openssl genrsa -out ~/.oci/handover-images-ci.pem 2048 && chmod 600 ~/.oci/handover-images-ci.pem
   openssl rsa -pubout -in ~/.oci/handover-images-ci.pem -out keys/handover-images-ci.pub.pem
   ```
4. `cp terraform.tfvars.example terraform.tfvars` and fill it in (it's
   git-ignored).

## Apply

```
terraform init
terraform plan     # read it: IAM changes are the ones to be sure of
terraform apply
```

Then hand the game repo its CI identity (`terraform output images_ci`):

```
R=<owner>/<game repo>
gh secret set OCI_CLI_USER        -R $R --body '<images_ci.user>'
gh secret set OCI_CLI_TENANCY     -R $R --body '<images_ci.tenancy>'
gh secret set OCI_CLI_FINGERPRINT -R $R --body '<images_ci.fingerprint>'
gh secret set OCI_CLI_KEY_CONTENT -R $R < ~/.oci/handover-images-ci.pem
```

and handover-hosting its backup identity (`terraform output backup`; key pair
made like step 3, as `~/.oci/handover-backup.pem` and `keys/handover-backup.pub.pem`):

```
R=<owner>/handover-hosting
gh secret set OCI_BACKUP_USER        -R $R --body '<backup.user>'
gh secret set OCI_BACKUP_TENANCY     -R $R --body '<backup.tenancy>'
gh secret set OCI_BACKUP_FINGERPRINT -R $R --body '<backup.fingerprint>'
gh secret set OCI_BACKUP_KEY_CONTENT -R $R < ~/.oci/handover-backup.pem
```

Then give `tf-handover` an API key (console: the user → API keys → add), whose
private half goes into the `handover-infra` workspace's `private_key`
variable; its OCID and fingerprint into `user_ocid` and `fingerprint`.

**Rotating the CI key:** make a new pair as in step 3, apply (the old key is
replaced), update `OCI_CLI_FINGERPRINT` and `OCI_CLI_KEY_CONTENT`.

## Adopting objects made by hand

A tenancy where some of these already exist (this one did: the compartment and
`tf-handover` were made in the console on 2026-10-18) brings them under
Terraform with import blocks in a local, git-ignored file, e.g. `adopt.tf`:

```hcl
import {
  to = oci_identity_compartment.handover
  id = "<compartment OCID>"
}
import {
  to = oci_identity_user_capabilities_management.terraform
  id = "capabilities/<tf-handover user OCID>"
}
# likewise: oci_identity_group.terraform, oci_identity_user.terraform,
# oci_identity_user_group_membership.terraform (membership OCID),
# oci_identity_policy.terraform
```

The plan must show the imports as no-ops (apart from intended changes) before
anything is applied; delete the file afterwards.
