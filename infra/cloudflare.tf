# Cloudflare side of the Handover: DNS, the Access app + policies guarding SSH,
# and the Turnstile widget on Play. Imported 2026-10-18 from what was set up by
# hand (imports.tf); the provider authenticates with CLOUDFLARE_API_TOKEN, a
# sensitive env var in the TFC workspace (token "terraform-handover": tunnel,
# Access, Turnstile and DNS on barrypekerman.com only).
#
# Not managed here (yet): the tunnel itself (phase 2 replaces it with one made
# here) and the handover-ci service token (created by hand; its id is below).

locals {
  cf_account  = "dbcba7623c52bc78632ae5f31399a06d"
  cf_zone     = "8e1c1adb257eb08052cc949c2ae7bb0c"
  tunnel_host = "ff6fc525-6791-4d75-aec0-a652197aaee2.cfargotunnel.com"
  ci_token_id = "9d89f0e8-38fc-4ebb-a9aa-1af61cfb7fe7" # service token handover-ci
}

resource "cloudflare_dns_record" "play" {
  zone_id                 = local.cf_zone
  name                    = "play.barrypekerman.com"
  type                    = "CNAME"
  content                 = local.tunnel_host
  proxied                 = true
  ttl                     = 1
  tags                    = []
  include_shadow_metadata = false
  settings = {
    flatten_cname = false
    ipv4_only     = false
    ipv6_only     = false
  }
  lifecycle {
    prevent_destroy = true # the game's address
  }
}

resource "cloudflare_dns_record" "ssh" {
  zone_id                 = local.cf_zone
  name                    = "handover-ssh.barrypekerman.com"
  type                    = "CNAME"
  content                 = local.tunnel_host
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

# Public port 22 is closed; SSH reaches the host only through the tunnel, and
# Access lets through either the admin (browser login) or CI (service token).
resource "cloudflare_zero_trust_access_application" "ssh" {
  account_id                 = local.cf_account
  name                       = "ssh.play"
  type                       = "self_hosted"
  domain                     = cloudflare_dns_record.ssh.name
  session_duration           = "24h"
  app_launcher_visible       = true
  auto_redirect_to_identity  = false
  enable_binding_cookie      = false
  http_only_cookie_attribute = false
  options_preflight_bypass   = false
  destinations = [
    {
      type = "public"
      uri  = cloudflare_dns_record.ssh.name
    },
  ]
  policies = [
    { id = cloudflare_zero_trust_access_policy.admin.id, precedence = 1 },
    { id = cloudflare_zero_trust_access_policy.ci.id, precedence = 2 },
  ]
}

resource "cloudflare_zero_trust_access_policy" "admin" {
  account_id       = local.cf_account
  name             = "handover-admin"
  decision         = "allow"
  include          = [{ email = { email = "barrypek@gmail.com" } }]
  connection_rules = { rdp = {} }
}

resource "cloudflare_zero_trust_access_policy" "ci" {
  account_id       = local.cf_account
  name             = "handover-ci"
  decision         = "non_identity" # service auth: no browser login
  include          = [{ service_token = { token_id = local.ci_token_id } }]
  connection_rules = { rdp = {} }
}

# Bot check on Play. The sitekey is public (spawner unit); the secret is the
# TURNSTILE_SECRET GitHub secret.
resource "cloudflare_turnstile_widget" "play" {
  account_id      = local.cf_account
  name            = "barrypekerman.com (Spin)"
  domains         = ["127.0.0.1", "barrypekerman.com", "localhost"]
  mode            = "managed"
  region          = "world"
  bot_fight_mode  = false
  clearance_level = "no_clearance"
  ephemeral_id    = false
  offlabel        = false
}
