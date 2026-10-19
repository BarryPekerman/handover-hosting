# Grafana (OKE cluster, oke-gitops repo) at ctf-monitor.barrypekerman.com. A
# cloudflared deployment in the cluster runs this tunnel to the Grafana
# service, so nothing in the cluster is exposed; Access lets in only the
# admin's email. The run token goes into oke-gitops as the SOPS secret
# monitoring/manifests/grafana-tunnel.enc.yaml (from the output below).
resource "cloudflare_zero_trust_tunnel_cloudflared" "grafana" {
  account_id = local.cf_account
  name       = "oke-grafana"
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "grafana" {
  account_id = local.cf_account
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.grafana.id
  config = {
    ingress = [
      { hostname = "ctf-monitor.barrypekerman.com", service = "http://kube-prometheus-stack-grafana.monitoring.svc.cluster.local:80" },
      { service = "http_status:404" },
    ]
  }
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "grafana" {
  account_id = local.cf_account
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.grafana.id
}

resource "cloudflare_dns_record" "grafana" {
  zone_id                 = local.cf_zone
  name                    = "ctf-monitor.barrypekerman.com"
  type                    = "CNAME"
  content                 = "${cloudflare_zero_trust_tunnel_cloudflared.grafana.id}.cfargotunnel.com"
  proxied                 = true
  ttl                     = 1
  tags                    = []
  include_shadow_metadata = false
  settings = {
    flatten_cname = false
    ipv4_only     = false
    ipv6_only     = false
  }
}

resource "cloudflare_zero_trust_access_application" "grafana" {
  account_id                 = local.cf_account
  name                       = "ctf-monitor"
  type                       = "self_hosted"
  domain                     = cloudflare_dns_record.grafana.name
  session_duration           = "24h"
  app_launcher_visible       = true
  auto_redirect_to_identity  = false
  enable_binding_cookie      = false
  http_only_cookie_attribute = false
  options_preflight_bypass   = false
  destinations = [
    { type = "public", uri = cloudflare_dns_record.grafana.name },
  ]
  policies = [
    { id = cloudflare_zero_trust_access_policy.admin.id, precedence = 1 },
  ]
}

output "grafana_tunnel_token" {
  description = "cloudflared run token for the cluster's Grafana tunnel"
  value       = data.cloudflare_zero_trust_tunnel_cloudflared_token.grafana.token
  sensitive   = true
}
