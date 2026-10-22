# The compartment everything of the Handover lives in, and the two identities
# that act on it:
#   tf-handover         Terraform Cloud's runs of infra/: the VM, its network
#                       rules and the game-image bucket. No IAM.
#   handover-images-ci  the game repo's CI: objects in the handover-images
#                       bucket, nothing else.
# Both are API-key only (no console password, tokens or other credentials).
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
  description    = "Terraform for The Handover: instances, boot volumes and the game-image bucket in compartment handover only; may attach to the tenancy's subnet"
  statements = [
    "Allow group handover-terraform to manage instance-family in compartment handover",
    "Allow group handover-terraform to manage volume-family in compartment handover",
    "Allow group handover-terraform to manage security-lists in compartment handover",
    "Allow group handover-terraform to use vnics in tenancy",
    "Allow group handover-terraform to use subnets in tenancy",
    # The game-image bucket (infra/storage.tf); its namespace is a tenancy-wide read.
    "Allow group handover-terraform to manage buckets in compartment handover",
    "Allow group handover-terraform to read objectstorage-namespaces in tenancy",
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

# The values the game repo's CI needs (its OCI_CLI_* secrets; README.md).
output "images_ci" {
  value = {
    user        = oci_identity_user.images_ci.id
    tenancy     = var.tenancy_ocid
    fingerprint = oci_identity_api_key.images_ci.fingerprint
  }
}
