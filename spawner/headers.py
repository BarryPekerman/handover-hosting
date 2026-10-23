"""Security headers on every response of the play port, the same set the main
site (barrypekerman.com) sends, with a CSP fitted to each kind of page:

- the spawner's own pages: scripts only with this response's nonce (every
  page has inline scripts the server fills in) or from Turnstile; never framed.
  The templates mark their own scripts nonce="__NONCE__", and each handler
  puts the nonce in (nonced) before inserting anything else, so a <script>
  that got into a page through data (a player's name) never carries it.
- /t/<sid>/...: ttyd's page, proxied as is. Its bundle is one inline script
  and style, so those are allowed there; only /app/<sid> (same origin) frames it.

The headers go on in on_response_prepare, which also runs for streamed and
WebSocket responses.
"""
import secrets

TURNSTILE = "https://challenges.cloudflare.com"

COMMON = {
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


def page_csp(nonce: str) -> str:
    return "; ".join([
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}' {TURNSTILE}",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "connect-src 'self'",
        f"frame-src 'self' {TURNSTILE}",       # the terminal (/app) and the Turnstile widget
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",                  # Play posts to /play
        "frame-ancestors 'none'",
    ])


TERMINAL_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self'",                      # its WebSocket, same origin
    "worker-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'self'",                  # framed by /app/<sid>
])


def is_terminal(request) -> bool:
    return request.path.startswith("/t/")


def nonced(request, template: str) -> str:
    """The template with this response's nonce in its own script tags. Call it
    on the template, before any other substitution."""
    nonce = request.get("csp_nonce") or secrets.token_urlsafe(16)
    request["csp_nonce"] = nonce
    return template.replace("__NONCE__", nonce)


async def add_headers(request, response):
    """on_response_prepare: the headers, on everything the play port sends."""
    for k, v in COMMON.items():
        response.headers.setdefault(k, v)
    if is_terminal(request):
        response.headers["Content-Security-Policy"] = TERMINAL_CSP
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    else:
        nonce = request.get("csp_nonce") or secrets.token_urlsafe(16)
        response.headers["Content-Security-Policy"] = page_csp(nonce)
        response.headers["X-Frame-Options"] = "DENY"
