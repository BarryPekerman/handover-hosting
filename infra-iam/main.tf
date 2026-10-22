terraform {
  required_version = ">= 1.5"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 6.0"
    }
  }
  # State in Terraform Cloud (same org as infra/), but LOCAL execution: plans
  # and applies run on an admin's laptop with their own OCI profile, so no
  # admin credential is ever stored in Terraform Cloud or CI.
  cloud {
    organization = "berries-handover"
    workspaces {
      project = "handover"
      name    = "handover-iam"
    }
  }
}

# An admin's ~/.oci/config profile. Everything in infra/ runs as the narrowly
# scoped identities made here; this is the only place admin rights are used.
provider "oci" {
  config_file_profile = var.oci_profile
  region              = var.region
}
