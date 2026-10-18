# One-time imports of the hand-made Cloudflare objects (2026-10-18). Delete this
# file after the first apply that includes them.
import {
  to = cloudflare_dns_record.play
  id = "${local.cf_zone}/71ba10874f4970e3c9ed9cb211f88d47"
}
import {
  to = cloudflare_dns_record.ssh
  id = "${local.cf_zone}/3a5453fceaafd20ae0cbf51d55930188"
}
import {
  to = cloudflare_zero_trust_access_application.ssh
  id = "accounts/${local.cf_account}/dd63827f-607d-497b-a212-620f924a3a10"
}
import {
  to = cloudflare_zero_trust_access_policy.admin
  id = "${local.cf_account}/e952e3cc-3e60-4932-80ea-1303c6e49c98"
}
import {
  to = cloudflare_zero_trust_access_policy.ci
  id = "${local.cf_account}/b3f5e769-7aae-4696-aa5d-4fbdb64ee984"
}
import {
  to = cloudflare_turnstile_widget.play
  id = "${local.cf_account}/0x4AAAAAAFYDIEkAlY8LK_O4"
}
