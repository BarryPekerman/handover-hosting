variable "tenancy_ocid" {
  description = "The tenancy; users, groups, policies and the compartment are created in its root"
  type        = string
}

variable "oci_profile" {
  description = "~/.oci/config profile of a tenancy administrator"
  type        = string
  default     = "DEFAULT"
}

variable "region" {
  description = "The tenancy's home region (IAM changes are made there)"
  type        = string
  default     = "il-jerusalem-1"
}

# Identity-domain tenancies require an email on every user; nothing is sent
# to it unless the user gets a console login, which these never do.
variable "tf_user_email" {
  description = "Email for tf-handover (Terraform Cloud's identity)"
  type        = string
}

variable "images_ci_user_email" {
  description = "Email for handover-images-ci (the game repo's CI identity)"
  type        = string
}
