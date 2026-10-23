# CI credentials that come from Terraform-made objects are written into the
# repos' Actions secrets here, so nobody copies a token by hand. The provider
# reads GITHUB_TOKEN from the workspace: a fine-grained PAT with Secrets
# read/write on these two repos only (expires 2027-10-09; every plan reads
# these secrets, so renew it before then and update the workspace variable).

locals {
  ci_repos = toset(["handover-hosting", "the-handover-linux-ctf"])
}

# The service token CI uses to get past Cloudflare Access on handover-ssh.
resource "cloudflare_zero_trust_access_service_token" "ci" {
  account_id = local.cf_account
  name       = "handover-ci-tf"
  duration   = "8760h"
}

resource "github_actions_secret" "cf_access_client_id" {
  for_each    = local.ci_repos
  repository  = each.key
  secret_name = "CF_ACCESS_CLIENT_ID"
  value       = cloudflare_zero_trust_access_service_token.ci.client_id
}

resource "github_actions_secret" "cf_access_client_secret" {
  for_each    = local.ci_repos
  repository  = each.key
  secret_name = "CF_ACCESS_CLIENT_SECRET"
  value       = cloudflare_zero_trust_access_service_token.ci.client_secret
}

# Bot check on Play: both halves from the widget in cloudflare.tf. deploy.yml
# writes them into the spawner's env file.
resource "github_actions_secret" "turnstile_sitekey" {
  repository  = "handover-hosting"
  secret_name = "TURNSTILE_SITEKEY"
  value       = cloudflare_turnstile_widget.play.sitekey
}

resource "github_actions_secret" "turnstile_secret" {
  repository  = "handover-hosting"
  secret_name = "TURNSTILE_SECRET"
  value       = cloudflare_turnstile_widget.play.secret
}

# Run token for the host's tunnel connector (deploy.yml -> host/site.yml).
resource "github_actions_secret" "cf_tunnel_token" {
  repository  = "handover-hosting"
  secret_name = "CF_TUNNEL_TOKEN"
  value       = data.cloudflare_zero_trust_tunnel_cloudflared_token.handover.token
}
