# The compartment everything of the Handover lives in, and the two identities
# that act on it:
#   tf-handover         Terraform Cloud's runs of infra/: the VM, its network
#                       rules and the game-image bucket. No IAM.
#   handover-images-ci  the game repo's CI: objects in the handover-images
#                       bucket, nothing else.
#   handover-backup     handover-hosting's backup workflows: create and read
#                       objects in the handover-backups bucket, never delete.
# All three are API-key only (no console password, tokens or other credentials).
#
# Oracle stamps CreatedBy/CreatedOn defined tags on everything; they're
# ignored so they never show up as drift.

resource "oci_identity_compartment" "handover" {
  compartment_id = var.tenancy_ocid
  name           = "handover"
  description    = "The Handover CTF host (managed by Terraform in TFC Berries-/handover)"
  enable_delete  = false # deleting a compartment deletes everything in it

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [defined_tags]
  }
}

# --- tf-handover: Terraform Cloud (infra/) ---

resource "oci_identity_group" "terraform" {
  compartment_id = var.tenancy_ocid
  name           = "handover-terraform"
  description    = "Terraform for The Handover (TFC Berries-/handover); scoped to compartment handover"

  lifecycle {
    ignore_changes = [defined_tags]
  }
}

resource "oci_identity_user" "terraform" {
  compartment_id = var.tenancy_ocid
  name           = "tf-handover"
  description    = "Terraform Cloud runs for The Handover. API key only, no console login."
  email          = var.tf_user_email

  lifecycle {
    ignore_changes = [defined_tags]
  }
}

resource "oci_identity_user_capabilities_management" "terraform" {
  user_id                      = oci_identity_user.terraform.id
  can_use_api_keys             = true
  can_use_auth_tokens          = false
  can_use_console_password     = false
  can_use_customer_secret_keys = false
  can_use_smtp_credentials     = false
}

resource "oci_identity_user_group_membership" "terraform" {
  user_id  = oci_identity_user.terraform.id
  group_id = oci_identity_group.terraform.id
}

# tf-handover's API key is not managed here: its private half is a sensitive
# variable in the handover-infra workspace, and rotating it is a hand step
# (upload the new public key, update the workspace, delete the old key).

resource "oci_identity_policy" "terraform" {
  compartment_id = var.tenancy_ocid
  name           = "handover-terraform"
  description    = "Terraform for The Handover: instances, boot volumes, the game-image and backup buckets in compartment handover only; may attach to the tenancy's subnet"
  statements = [
    "Allow group handover-terraform to manage instance-family in compartment handover",
    "Allow group handover-terraform to manage volume-family in compartment handover",
    "Allow group handover-terraform to manage security-lists in compartment handover",
    "Allow group handover-terraform to use vnics in tenancy",
    "Allow group handover-terraform to use subnets in tenancy",
    # The game-image bucket (infra/storage.tf); its namespace is a tenancy-wide read.
    "Allow group handover-terraform to manage buckets in compartment handover",
    "Allow group handover-terraform to read objectstorage-namespaces in tenancy",
    # Setting the backup bucket's lifecycle rule (infra/storage.tf) takes
    # object permissions on that bucket
    "Allow group handover-terraform to manage objects in compartment handover where target.bucket.name = 'handover-backups'",
  ]

  lifecycle {
    ignore_changes = [defined_tags]
  }
}

# --- handover-images-ci: the game repo's CI (upload, prune, deploy) ---

resource "oci_identity_group" "images_ci" {
  compartment_id = var.tenancy_ocid
  name           = "handover-images-ci"
  description    = "The game repo's CI: objects in the handover-images bucket only"

  lifecycle {
    ignore_changes = [defined_tags]
  }
}

resource "oci_identity_user" "images_ci" {
  compartment_id = var.tenancy_ocid
  name           = "handover-images-ci"
  description    = "The game repo's CI (upload, prune, deploy of game images). API key only, no console login."
  email          = var.images_ci_user_email

  lifecycle {
    ignore_changes = [defined_tags]
  }
}

resource "oci_identity_user_capabilities_management" "images_ci" {
  user_id                      = oci_identity_user.images_ci.id
  can_use_api_keys             = true
  can_use_auth_tokens          = false
  can_use_console_password     = false
  can_use_customer_secret_keys = false
  can_use_smtp_credentials     = false
}

resource "oci_identity_user_group_membership" "images_ci" {
  user_id  = oci_identity_user.images_ci.id
  group_id = oci_identity_group.images_ci.id
}

# Only the public half is here; the private key goes from the admin's disk
# straight into the game repo's OCI_CLI_KEY_CONTENT secret (README.md).
resource "oci_identity_api_key" "images_ci" {
  user_id   = oci_identity_user.images_ci.id
  key_value = file("${path.module}/keys/handover-images-ci.pub.pem")
}

resource "oci_identity_policy" "images_ci" {
  compartment_id = var.tenancy_ocid
  name           = "handover-images-ci"
  description    = "The game repo's CI: read the handover-images bucket and manage its objects; nothing else"
  statements = [
    "Allow group handover-images-ci to read buckets in compartment handover where target.bucket.name = 'handover-images'",
    "Allow group handover-images-ci to manage objects in compartment handover where target.bucket.name = 'handover-images'",
  ]

  lifecycle {
    ignore_changes = [defined_tags]
  }
}

# --- handover-backup: the nightly backup and its restore test ---
resource "oci_identity_group" "backup" {
  compartment_id = var.tenancy_ocid
  name           = "handover-backup"
  description    = "handover-hosting's backup.yml and restore-test.yml: write and read the handover-backups bucket, never delete"

  lifecycle {
    ignore_changes = [defined_tags]
  }
}

resource "oci_identity_user" "backup" {
  compartment_id = var.tenancy_ocid
  name           = "handover-backup"
  description    = "The leaderboard backups (backup.yml, restore-test.yml). API key only, no console login."
  email          = var.backup_user_email

  lifecycle {
    ignore_changes = [defined_tags]
  }
}

resource "oci_identity_user_capabilities_management" "backup" {
  user_id                      = oci_identity_user.backup.id
  can_use_api_keys             = true
  can_use_auth_tokens          = false
  can_use_console_password     = false
  can_use_customer_secret_keys = false
  can_use_smtp_credentials     = false
}

resource "oci_identity_user_group_membership" "backup" {
  user_id  = oci_identity_user.backup.id
  group_id = oci_identity_group.backup.id
}

# Only the public half is here; the private key goes from the admin's disk
# straight into handover-hosting's OCI_BACKUP_KEY_CONTENT secret (README.md).
resource "oci_identity_api_key" "backup" {
  user_id   = oci_identity_user.backup.id
  key_value = file("${path.module}/keys/handover-backup.pub.pem")
}

# Create, list and read objects, and nothing else: no delete, no overwrite.
# Whoever holds the CI key can add backups but can't destroy the ones there.
# Old backups go by the bucket's lifecycle rule (infra/storage.tf), which is
# Object Storage acting with the service policy below.
resource "oci_identity_policy" "backup" {
  compartment_id = var.tenancy_ocid
  name           = "handover-backup"
  description    = "The leaderboard backups: create and read objects in the handover-backups bucket; never delete or overwrite"
  statements = [
    "Allow group handover-backup to read buckets in compartment handover where target.bucket.name = 'handover-backups'",
    "Allow group handover-backup to manage objects in compartment handover where all {target.bucket.name = 'handover-backups', any {request.permission = 'OBJECT_CREATE', request.permission = 'OBJECT_INSPECT', request.permission = 'OBJECT_READ'}}",
  ]

  lifecycle {
    ignore_changes = [defined_tags]
  }
}

# Object lifecycle rules run as the Object Storage service, which needs its own
# permission to delete expired objects. Scoped to the backup bucket.
resource "oci_identity_policy" "objectstorage_lifecycle" {
  compartment_id = var.tenancy_ocid
  name           = "handover-objectstorage-lifecycle"
  description    = "Object Storage may delete expired objects in the handover-backups bucket (its lifecycle rule)"
  statements = [
    "Allow service objectstorage-${var.region} to manage object-family in compartment handover where target.bucket.name = 'handover-backups'",
  ]

  lifecycle {
    ignore_changes = [defined_tags]
  }
}

# --- The rest of "API key only" ---
# oci_identity_user_capabilities_management (oracle/oci 6.37) can't switch off
# DB credentials and OAuth2 client credentials, and a new identity-domain user
# gets both, which would let the key's holder mint itself more credentials.
# So both are switched off through the domain's SCIM API, by the admin running
# this (needs the oci CLI). Runs once per user; re-run with
# `terraform apply -replace='terraform_data.api_key_only["<user>"]'`.

data "oci_identity_domains" "default" {
  compartment_id = var.tenancy_ocid
  display_name   = "Default"
}

resource "terraform_data" "api_key_only" {
  for_each = {
    (oci_identity_user.terraform.name) = oci_identity_user.terraform.id
    (oci_identity_user.images_ci.name) = oci_identity_user.images_ci.id
    (oci_identity_user.backup.name)    = oci_identity_user.backup.id
  }
  triggers_replace = [each.value]

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    environment = {
      OCI_CLI_PROFILE        = var.oci_profile
      SUPPRESS_LABEL_WARNING = "True"
      ENDPOINT               = data.oci_identity_domains.default.domains[0].url
      USER_NAME              = each.key
    }
    command = <<-EOT
      set -euo pipefail
      cap=urn:ietf:params:scim:schemas:oracle:idcs:extension:capabilities:User
      id=$(oci identity-domains users list --endpoint "$ENDPOINT" --filter "userName eq \"$USER_NAME\"" \
        --query 'data.resources[0].id' --raw-output)
      oci identity-domains user patch --endpoint "$ENDPOINT" --user-id "$id" \
        --schemas '["urn:ietf:params:scim:api:messages:2.0:PatchOp"]' \
        --operations "[{\"op\":\"replace\",\"path\":\"$cap:canUseDbCredentials\",\"value\":false},
                       {\"op\":\"replace\",\"path\":\"$cap:canUseOAuth2ClientCredentials\",\"value\":false}]" >/dev/null
      echo "$USER_NAME: DB and OAuth2 client credentials off"
    EOT
  }
}

# The values the game repo's CI needs (its OCI_CLI_* secrets; README.md).
output "images_ci" {
  value = {
    user        = oci_identity_user.images_ci.id
    tenancy     = var.tenancy_ocid
    fingerprint = oci_identity_api_key.images_ci.fingerprint
  }
}

# The values handover-hosting's backup workflows need (OCI_BACKUP_* secrets; README.md).
output "backup" {
  value = {
    user        = oci_identity_user.backup.id
    tenancy     = var.tenancy_ocid
    fingerprint = oci_identity_api_key.backup.fingerprint
  }
}
