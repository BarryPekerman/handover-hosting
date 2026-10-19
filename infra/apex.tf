# barrypekerman.com itself is reserved for a personal site that isn't built yet.
# Until then the bare domain redirects to the game, so a visitor who drops the
# "play." gets the game instead of Cloudflare's 522 (the apex A record is a
# hand-made placeholder, 192.0.0.1, with nothing behind it). 302, not 301, so
# browsers don't cache it: delete this file when the site is ready.
resource "cloudflare_ruleset" "apex_redirect" {
  zone_id     = local.cf_zone
  name        = "apex redirect"
  description = "barrypekerman.com -> the game, until the personal site exists"
  kind        = "zone"
  phase       = "http_request_dynamic_redirect"
  rules = [{
    ref         = "apex_to_play"
    description = "Bare domain to play.barrypekerman.com"
    expression  = "(http.host eq \"barrypekerman.com\")"
    action      = "redirect"
    action_parameters = {
      from_value = {
        status_code           = 302
        target_url            = { value = "https://play.barrypekerman.com/" }
        preserve_query_string = false
      }
    }
  }]
}
