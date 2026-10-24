# Auth for tf-handover; set as workspace variables in Terraform Cloud.
variable "tenancy_ocid" {
  type = string
}

variable "user_ocid" {
  type = string
}

variable "fingerprint" {
  type = string
}

variable "private_key" {
  description = "PEM of tf-handover's API key"
  type        = string
  sensitive   = true
}

variable "region" {
  type    = string
  default = "il-jerusalem-1"
}

variable "compartment_ocid" {
  description = "Compartment the instance lives in (`handover`)"
  type        = string
}

variable "availability_domain" {
  description = "e.g. EXMP:IL-JERUSALEM-1-AD-1"
  type        = string
}

variable "subnet_ocid" {
  description = "Existing public subnet (Basic-Subnet) to attach the VNIC to"
  type        = string
}

variable "vcn_ocid" {
  description = "Existing VCN the security list belongs to"
  type        = string
}

variable "securitylist_ocid" {
  description = "Existing default security list to import and manage"
  type        = string
}

variable "ssh_public_key" {
  description = "Public key placed in the instance's authorized_keys (only used at creation)"
  type        = string
}

variable "display_name" {
  type    = string
  default = "MaximalFreeUbuntu"
}

variable "shape" {
  type    = string
  default = "VM.Standard.A1.Flex"
}

variable "ocpus" {
  type    = number
  default = 4
}

variable "memory_gbs" {
  type    = number
  default = 24
}
