# The tunnel the host's cloudflared runs, made and configured here (remotely
# managed: the routes below are the whole config, no file on the host). The
# host only needs the run token, delivered as the CF_TUNNEL_TOKEN secret by
# deploy.yml and, for a fresh box, by cloud-init.
resource "cloudflare_zero_trust_tunnel_cloudflared" "handover" {
  account_id = local.cf_account
  name       = "handover-tf"
  config_src = "cloudflare"
  lifecycle {
    prevent_destroy = false # drill B (2026-10-19): restored after the rebuild
  }
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "handover" {
  account_id = local.cf_account
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.handover.id
  config = {
    ingress = [
      { hostname = "play.barrypekerman.com", service = "http://localhost:8080" },
      # SSH for the admin and CI, behind the Access app in cloudflare.tf.
      { hostname = "handover-ssh.barrypekerman.com", service = "ssh://localhost:22" },
      { service = "http_status:404" },
    ]
  }
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "handover" {
  account_id = local.cf_account
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.handover.id
}

output "tunnel_token" {
  description = "cloudflared run token for the host (CF_TUNNEL_TOKEN)"
  value       = data.cloudflare_zero_trust_tunnel_cloudflared_token.handover.token
  sensitive   = true
}

output "tunnel_cname" {
  value = "${cloudflare_zero_trust_tunnel_cloudflared.handover.id}.cfargotunnel.com"
}
