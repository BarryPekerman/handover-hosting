terraform {
  required_version = ">= 1.5"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 6.0"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "5.28.0"
    }
  }
  # State and runs live in Terraform Cloud (org berries-handover, project
  # handover) — its own org, so this repo's token reaches nothing else.
  # Runs execute remotely with the workspace's variables, so nothing here
  # needs ~/.oci or ~/.ssh. CI drives it: .github/workflows/terraform.yml.
  cloud {
    organization = "berries-handover"
    workspaces {
      project = "handover"
      name    = "handover-infra"
    }
  }
}

# Authenticates as tf-handover, an API-key-only user whose policy covers
# instances and boot volumes in compartment `handover` (plus attaching to the
# tenancy's one subnet) and nothing else.
# Reads CLOUDFLARE_API_TOKEN from the workspace environment.
provider "cloudflare" {}

provider "oci" {
  tenancy_ocid = var.tenancy_ocid
  user_ocid    = var.user_ocid
  fingerprint  = var.fingerprint
  private_key  = var.private_key
  region       = var.region
}

# Latest Canonical Ubuntu 24.04 arm64 image in this tenancy (so a rebuild always
# starts from a current base; ignore_changes below stops it forcing a replace).
data "oci_core_images" "ubuntu" {
  compartment_id           = var.compartment_ocid
  operating_system         = "Canonical Ubuntu"
  operating_system_version = "24.04"
  shape                    = var.shape
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

resource "oci_core_instance" "handover" {
  # In-transit boot volume encryption is a launch option: changing it would
  # replace the instance, which prevent_destroy forbids on a capacity-scarce
  # free-tier box. Revisit on the next deliberate rebuild.
  #checkov:skip=CKV_OCI_4:needs a rebuild; see comment above
  availability_domain = var.availability_domain
  compartment_id      = var.compartment_ocid
  display_name        = var.display_name
  shape               = var.shape

  shape_config {
    ocpus         = var.ocpus
    memory_in_gbs = var.memory_gbs
  }

  source_details {
    source_type = "image"
    source_id   = data.oci_core_images.ubuntu.images[0].id
  }

  # Metadata only through IMDS v2 (needs the Authorization header), which
  # cloud-init and Oracle Cloud Agent already use. Applies in place.
  instance_options {
    are_legacy_imds_endpoints_disabled = true
  }

  create_vnic_details {
    subnet_id        = var.subnet_ocid # existing Basic-Subnet (referenced, not managed)
    assign_public_ip = true
  }

  metadata = {
    ssh_authorized_keys = var.ssh_public_key
    user_data = base64encode(templatefile("${path.module}/cloud-init.yaml.tftpl", {
      tunnel_token        = data.cloudflare_zero_trust_tunnel_cloudflared_token.handover.token
      cloudflared_version = "2026.10.1" # keep in step with host/site.yml
    }))
  }

  # Free A1 capacity is scarce: never let a plan silently destroy/replace the
  # live box. A newer base image or an edited cloud-init must NOT trigger a
  # replace of a running instance - rebuilds are a deliberate, manual act.
  lifecycle {
    prevent_destroy = true
    # The live box was hand-built (no user_data, slightly different key
    # formatting) and its image/boot details are provider-computed. Ignore the
    # whole metadata block + the image id so *adopting* the running instance is
    # a no-op. A from-scratch `apply` still sets metadata/cloud-init, because
    # ignore_changes only affects updates, never creation.
    ignore_changes = [metadata, source_details[0].source_id]
  }
}

output "public_ip" {
  value = oci_core_instance.handover.public_ip
}
