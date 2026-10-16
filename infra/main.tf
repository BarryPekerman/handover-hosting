terraform {
  required_version = ">= 1.5"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 6.0"
    }
  }
  # For real use, move state to a remote backend (OCI Object Storage / TF Cloud).
  # Local state is fine to start but contains sensitive values - it's git-ignored.
}

provider "oci" {
  config_file_profile = var.oci_profile # [ALEX] in ~/.oci/config
  region              = var.region
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

  create_vnic_details {
    subnet_id        = var.subnet_ocid # existing Basic-Subnet (referenced, not managed)
    assign_public_ip = true
  }

  metadata = {
    ssh_authorized_keys = file(var.ssh_public_key_path)
    user_data           = base64encode(file("${path.module}/cloud-init.yaml"))
  }

  # Free A1 capacity is scarce: never let a plan silently destroy/replace the
  # live box. A newer base image or an edited cloud-init must NOT trigger a
  # replace of a running instance - rebuilds are a deliberate, manual act.
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [source_details[0].source_id, metadata["user_data"]]
  }
}

output "public_ip" {
  value = oci_core_instance.handover.public_ip
}
