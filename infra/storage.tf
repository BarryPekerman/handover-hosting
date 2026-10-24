# The game images: the-handover-linux-ctf's CI uploads each tested release build here
# (~270 MB, named handover-<ci run id>-<commit>.tar.zst) and keeps the newest 6;
# its deploy-image workflow ships one to the host from here. They live in this
# tenancy because only this host uses them, and a bucket outlives a rebuilt VM.
# Always Free covers 20 GB of Object Storage in the tenancy.
#
# CI reaches the bucket as handover-images-ci, an API-key-only user allowed to
# manage objects in this bucket and nothing else. It lives in ../infra-iam,
# applied by an admin (tf-handover can't manage IAM).

data "oci_objectstorage_namespace" "this" {
  compartment_id = var.tenancy_ocid
}

resource "oci_objectstorage_bucket" "images" {
  #checkov:skip=CKV_OCI_7:Oracle-managed encryption; a customer key needs a Vault for one bucket of rebuildable images
  #checkov:skip=CKV_OCI_8:versioning would keep every pruned image; the point is bounded storage
  #checkov:skip=CKV_OCI_10:nothing consumes object events
  compartment_id = var.compartment_ocid
  namespace      = data.oci_objectstorage_namespace.this.namespace
  name           = "handover-images"
  access_type    = "NoPublicAccess" # the images hold the live level passwords
  storage_tier   = "Standard"
  versioning     = "Disabled"

  # Emptying it is the only way to lose the stored images; deleting it should
  # never be a side effect of a plan.
  lifecycle {
    prevent_destroy = true
  }
}

# the-handover-linux-ctf's workflows address the bucket by namespace; the API key
# itself is a hand-set secret (README.md).
resource "github_actions_secret" "images_namespace" {
  repository  = "the-handover-linux-ctf"
  secret_name = "OCI_IMAGES_NAMESPACE"
  value       = data.oci_objectstorage_namespace.this.namespace
}

output "images_bucket" {
  value = "${oci_objectstorage_bucket.images.namespace}/${oci_objectstorage_bucket.images.name}"
}

# The nightly leaderboard backups (backup.yml), encrypted before upload, and the
# monthly restore test (restore-test.yml) that proves they decrypt and open.
# Written by handover-backup, which may add and read objects but not delete
# them (../infra-iam). Each backup is kept 30 days, the time the story page
# promises raw records outlive the game; closing the game empties the bucket
# (docs/operations.md). A few KB a night, well inside Always Free's 20 GB.
resource "oci_objectstorage_bucket" "backups" {
  #checkov:skip=CKV_OCI_7:Oracle-managed encryption; the objects are already gpg-encrypted before upload
  #checkov:skip=CKV_OCI_8:versioning would keep deleted backups past the 30 days promised
  #checkov:skip=CKV_OCI_10:nothing consumes object events
  compartment_id = var.compartment_ocid
  namespace      = data.oci_objectstorage_namespace.this.namespace
  name           = "handover-backups"
  access_type    = "NoPublicAccess"
  storage_tier   = "Standard"
  versioning     = "Disabled"

  lifecycle {
    prevent_destroy = true
  }
}

resource "oci_objectstorage_object_lifecycle_policy" "backups" {
  namespace = data.oci_objectstorage_namespace.this.namespace
  bucket    = oci_objectstorage_bucket.backups.name

  rules {
    name        = "delete-after-30-days"
    action      = "DELETE"
    is_enabled  = true
    target      = "objects"
    time_amount = 30
    time_unit   = "DAYS"
  }
}

# backup.yml and restore-test.yml address the bucket by namespace
resource "github_actions_secret" "backups_namespace" {
  repository  = "handover-hosting"
  secret_name = "OCI_BACKUP_NAMESPACE"
  value       = data.oci_objectstorage_namespace.this.namespace
}
