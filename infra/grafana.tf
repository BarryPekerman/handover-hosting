# Grafana's tunnel, DNS record and Access app (ctf-monitor.barrypekerman.com)
# moved to the cluster's own infra, oci-oke-infra terraform/grafana.tf, so
# taking the game down can't take the cluster's monitoring with it. These
# blocks drop them from this state without destroying them; once applied,
# this file can go.
removed {
  from = cloudflare_zero_trust_tunnel_cloudflared.grafana
  lifecycle { destroy = false }
}
removed {
  from = cloudflare_zero_trust_tunnel_cloudflared_config.grafana
  lifecycle { destroy = false }
}
removed {
  from = cloudflare_dns_record.grafana
  lifecycle { destroy = false }
}
removed {
  from = cloudflare_zero_trust_access_application.grafana
  lifecycle { destroy = false }
}
