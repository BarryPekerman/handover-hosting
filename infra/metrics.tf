# Metrics for the OKE Prometheus: the spawner's /metrics and node-exporter,
# both listening on the host's localhost only. Prometheus reaches them through
# these two tunnel routes, and Access lets through only its service token
# (sent as CF-Access-Client-Id/Secret headers; the oke-gitops repo keeps the
# pair in a SOPS secret, from the outputs below).
locals {
  metrics_hosts = {
    metrics = { name = "handover-metrics.barrypekerman.com", service = "http://localhost:9101" }
    node    = { name = "handover-node.barrypekerman.com", service = "http://localhost:9100" }
  }
}

resource "cloudflare_dns_record" "metrics" {
  for_each                = local.metrics_hosts
  zone_id                 = local.cf_zone
  name                    = each.value.name
  type                    = "CNAME"
  content                 = "${cloudflare_zero_trust_tunnel_cloudflared.handover.id}.cfargotunnel.com"
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

resource "cloudflare_zero_trust_access_service_token" "prometheus" {
  account_id = local.cf_account
  name       = "oke-prometheus"
  duration   = "8760h"
}

resource "cloudflare_zero_trust_access_policy" "prometheus" {
  account_id       = local.cf_account
  name             = "handover-prometheus"
  decision         = "non_identity"
  include          = [{ service_token = { token_id = cloudflare_zero_trust_access_service_token.prometheus.id } }]
  connection_rules = { rdp = {} }
}

resource "cloudflare_zero_trust_access_application" "metrics" {
  account_id                 = local.cf_account
  name                       = "handover-metrics"
  type                       = "self_hosted"
  domain                     = local.metrics_hosts.metrics.name
  session_duration           = "24h"
  app_launcher_visible       = false
  auto_redirect_to_identity  = false
  enable_binding_cookie      = false
  http_only_cookie_attribute = false
  options_preflight_bypass   = false
  destinations = [
    for k, h in local.metrics_hosts : { type = "public", uri = cloudflare_dns_record.metrics[k].name }
  ]
  policies = [
    { id = cloudflare_zero_trust_access_policy.prometheus.id, precedence = 1 },
  ]
}

output "prometheus_access_client_id" {
  value     = cloudflare_zero_trust_access_service_token.prometheus.client_id
  sensitive = true
}

output "prometheus_access_client_secret" {
  value     = cloudflare_zero_trust_access_service_token.prometheus.client_secret
  sensitive = true
}
