variable "oci_profile" {
  description = "Profile in ~/.oci/config to auth with"
  type        = string
  default     = "ALEX"
}

variable "region" {
  type    = string
  default = "il-jerusalem-1"
}

variable "compartment_ocid" {
  description = "Compartment to create the instance in (the tenancy root here)"
  type        = string
}

variable "availability_domain" {
  description = "e.g. dwHr:IL-JERUSALEM-1-AD-1"
  type        = string
}

variable "subnet_ocid" {
  description = "Existing public subnet (Basic-Subnet) to attach the VNIC to"
  type        = string
}

variable "ssh_public_key_path" {
  description = "Public key placed in the instance's authorized_keys"
  type        = string
  default     = "~/.ssh/alex_handover.pub"
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
