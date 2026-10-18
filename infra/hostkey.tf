# The host's SSH key, made here so it survives rebuilds: cloud-init installs it
# on a new instance, and CI's DEPLOY_KNOWN_HOSTS (written below) never has to
# change. Private half in Terraform state (sensitive).
resource "tls_private_key" "host" {
  algorithm = "ED25519"
}

locals {
  # The current box predates this key; keep trusting its own key until it is
  # replaced (drop this line after the rebuild).
  live_host_key = "handover-ssh.barrypekerman.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEOEGCagc6gLOFxeSBXN+laNI0VuKDHi5uMNNmgxEbEh"
  known_hosts = join("\n", [
    "handover-ssh.barrypekerman.com ${trimspace(tls_private_key.host.public_key_openssh)}",
    local.live_host_key,
  ])
}

resource "github_actions_secret" "deploy_known_hosts" {
  for_each    = local.ci_repos
  repository  = each.key
  secret_name = "DEPLOY_KNOWN_HOSTS"
  value       = local.known_hosts
}

output "host_key_line" {
  description = "known_hosts line for a laptop (~/.ssh/known_hosts)"
  value       = "handover-ssh.barrypekerman.com ${trimspace(tls_private_key.host.public_key_openssh)}"
}
