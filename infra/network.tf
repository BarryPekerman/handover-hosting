# The subnet's only security list (moved into compartment handover so the
# scoped tf-handover user can manage it). Nothing comes in but ICMP: the game
# and SSH reach the host only through the Cloudflare tunnel, which dials out.
# Break-glass (tunnel down): add TCP 22 from your IP here, or in the console.
resource "oci_core_security_list" "default" {
  compartment_id = var.compartment_ocid
  vcn_id         = "ocid1.vcn.oc1.il-jerusalem-1.amaaEXAMPLE"
  display_name   = "Default Security List for Basic-VCN"

  egress_security_rules {
    destination      = "0.0.0.0/0"
    destination_type = "CIDR_BLOCK"
    protocol         = "all"
    stateless        = false
  }

  # Path MTU discovery (fragmentation needed) from anywhere.
  ingress_security_rules {
    source      = "0.0.0.0/0"
    source_type = "CIDR_BLOCK"
    protocol    = "1"
    stateless   = false
    icmp_options {
      type = 3
      code = 4
    }
  }

  # Destination-unreachable within the VCN.
  ingress_security_rules {
    source      = "10.0.0.0/16"
    source_type = "CIDR_BLOCK"
    protocol    = "1"
    stateless   = false
    icmp_options {
      type = 3
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  to = oci_core_security_list.default
  id = "ocid1.securitylist.oc1.il-jerusalem-1.aaaaEXAMPLE"
}
