"""Unit tests for auth.py: who a visitor is, the throttle, the bot check and
GitHub sign-in. Network calls (Turnstile, GitHub) are faked.

    pip install pytest aiohttp && pytest tests/
"""
import asyncio
import os
import sys

import pytest
from aiohttp.test_utils import make_mocked_request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "spawner"))
import auth  # noqa: E402
import common  # noqa: E402


@pytest.fixture(autouse=True)
def reset(tmp_path, monkeypatch):
    common.DB_PATH = str(tmp_path / "lb.db")
    monkeypatch.setattr(auth, "SESSION_SECRET", "test-secret")
    for d in (common.sessions, common.states, common.who, common.ip_hits):
        d.clear()


def req(path="/", cookies=None, headers=None, method="GET"):
    h = dict(headers or {})
    if cookies:
        h["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return make_mocked_request(method, path, headers=h)


def signed_in(uid=7, login="dev"):
    return {"ghid": auth._sign({"uid": uid, "login": login})}


# ---- client IP -----------------------------------------------------------

def test_client_ip_prefers_cloudflare_then_forwarded_for():
    assert auth.client_ip(req(headers={"CF-Connecting-IP": "1.1.1.1",
                                       "X-Forwarded-For": "2.2.2.2"})) == "1.1.1.1"
    assert auth.client_ip(req(headers={"X-Forwarded-For": "2.2.2.2, 10.0.0.1"})) == "2.2.2.2"


def test_client_ip_without_proxy_headers_is_the_socket_peer():
    class Peer:
        def get_extra_info(self, key, default=None):
            return ("5.6.7.8", 40000) if key == "peername" else default
    assert auth.client_ip(make_mocked_request("GET", "/", transport=Peer())) == "5.6.7.8"


# ---- signed cookie -------------------------------------------------------

def test_signed_cookie_round_trips():
    assert auth._unsign(auth._sign({"uid": 7, "login": "dev"})) == {"uid": 7, "login": "dev"}


@pytest.mark.parametrize("token", ["", "no-dot", "abc.def", "!!!.0000", "e30.0" * 3])
def test_garbage_tokens_are_rejected(token):
    assert auth._unsign(token) is None


def test_a_tampered_payload_or_another_key_is_rejected(monkeypatch):
    tok = auth._sign({"uid": 7, "login": "dev"})
    forged = auth._sign({"uid": 8, "login": "evil"}).split(".")[0] + "." + tok.split(".")[1]
    assert auth._unsign(forged) is None
    monkeypatch.setattr(auth, "SESSION_SECRET", "another-key")
    assert auth._unsign(tok) is None


def test_identity():
    assert auth.identity(req(cookies=signed_in(7, "dev"))) == ("7", "@dev")
    assert auth.identity(req()) == (None, "anonymous")
    assert auth.identity(req(cookies={"ghid": "forged.sig"})) == (None, "anonymous")


def test_identity_is_anonymous_without_a_signing_key(monkeypatch):
    cookies = signed_in()
    monkeypatch.setattr(auth, "SESSION_SECRET", "")
    assert auth.identity(req(cookies=cookies)) == (None, "anonymous")


@pytest.mark.parametrize("name,set_cookie", [
    ("pid", lambda r: auth.with_pid(r, "a" * 32)),
    (common.COOKIE, lambda r: auth.with_cookie(r, "s" * 32)),
])
def test_id_cookies_are_hidden_from_page_scripts_and_other_sites(name, set_cookie):
    c = set_cookie(auth.web.Response()).cookies[name]
    assert c["httponly"] and c["samesite"] == "Lax"


# ---- throttle ------------------------------------------------------------

def test_throttle_allows_rate_max_then_blocks_until_the_window_passes(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])
    for _ in range(common.RATE_MAX):
        assert not auth.throttled("9.9.9.9")
    assert auth.throttled("9.9.9.9")
    assert not auth.throttled("8.8.8.8")                 # per IP
    now[0] += common.RATE_WINDOW
    assert not auth.throttled("9.9.9.9")


# ---- fake aiohttp client -------------------------------------------------

class FakeResp:
    def __init__(self, body):
        self.body = body

    async def __aenter__(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self.body


class FakeSession:
    """ClientSession stand-in: replies[url] is the JSON (or exception) returned."""
    calls = []

    def __init__(self, replies):
        self.replies = replies

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, **kw):
        FakeSession.calls.append((url, kw))
        return FakeResp(self.replies[url])

    get = post


# ---- Turnstile -----------------------------------------------------------

def turnstile(monkeypatch, reply):
    monkeypatch.setattr(auth, "TS_SECRET", "ts-secret")
    monkeypatch.setattr(auth, "ClientSession", FakeSession({auth.TS_VERIFY_URL: reply}))
    return lambda token="tok": asyncio.run(auth.verify_turnstile(token, "1.2.3.4"))


def test_turnstile_is_off_without_a_secret(monkeypatch):
    monkeypatch.setattr(auth, "TS_SECRET", "")
    assert asyncio.run(auth.verify_turnstile("", "1.2.3.4"))


def test_turnstile_passes_a_good_token(monkeypatch):
    check = turnstile(monkeypatch, {"success": True, "action": auth.TS_ACTION,
                                    "hostname": "play.barrypekerman.com"})
    assert check()


@pytest.mark.parametrize("reply", [
    {"success": False, "action": "play", "hostname": "play.barrypekerman.com"},
    {"success": True, "action": "other", "hostname": "play.barrypekerman.com"},   # token for another form
    {"success": True, "action": "play", "hostname": "evil.example"},              # token from another site
    RuntimeError("siteverify down"),
])
def test_turnstile_rejects(monkeypatch, reply):
    assert not turnstile(monkeypatch, reply)()


def test_turnstile_rejects_a_missing_token(monkeypatch):
    assert not turnstile(monkeypatch, {"success": True})("")


# ---- GitHub sign-in ------------------------------------------------------

TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"


@pytest.fixture
def oauth(monkeypatch):
    monkeypatch.setattr(auth, "GH_CLIENT_ID", "cid")
    monkeypatch.setattr(auth, "GH_CLIENT_SECRET", "csecret")

    def github(token_reply, user_reply=None):
        monkeypatch.setattr(auth, "ClientSession", FakeSession({TOKEN_URL: token_reply, USER_URL: user_reply}))
    return github


def callback(code="c0de", state="st", cookie_state="st"):
    r = req(f"/auth/callback?code={code}&state={state}", cookies={"oauth_state": cookie_state})
    return asyncio.run(auth.h_callback(r))


@pytest.mark.parametrize("missing", ["GH_CLIENT_ID", "GH_CLIENT_SECRET", "SESSION_SECRET"])
def test_sign_in_is_off_if_any_setting_is_missing(oauth, monkeypatch, missing):
    assert auth.oauth_ready()
    monkeypatch.setattr(auth, missing, "")
    assert not auth.oauth_ready()
    assert asyncio.run(auth.h_login(req("/login"))).location == "/"


def test_login_redirects_to_github_with_a_state_cookie(oauth):
    r = asyncio.run(auth.h_login(req("/login")))
    state = r.cookies["oauth_state"].value
    assert r.location.startswith("https://github.com/login/oauth/authorize?client_id=cid")
    assert f"state={state}" in r.location and f"redirect_uri={common.PUBLIC_URL}/auth/callback" in r.location


def test_callback_signs_the_player_in(oauth):
    oauth({"access_token": "gho_x"}, {"id": 42, "login": "octo"})
    r = callback()
    assert r.location == "/"
    assert auth._unsign(r.cookies["ghid"].value) == {"uid": 42, "login": "octo"}
    assert r.cookies["ghid"]["secure"] and r.cookies["ghid"]["httponly"]


@pytest.mark.parametrize("kw", [{"state": "other"}, {"code": ""}, {"cookie_state": ""}])
def test_callback_rejects_a_bad_state_or_code(oauth, kw):
    oauth({"access_token": "gho_x"}, {"id": 42, "login": "octo"})
    assert "ghid" not in callback(**kw).cookies


@pytest.mark.parametrize("token_reply,user_reply", [
    ({"error": "bad_verification_code"}, None),       # GitHub refused the code
    ({"access_token": "gho_x"}, {"message": "Bad credentials"}),
    (RuntimeError("github down"), None),
])
def test_callback_failures_go_home_signed_out(oauth, token_reply, user_reply):
    oauth(token_reply, user_reply)
    r = callback()
    assert r.location == "/" and "ghid" not in r.cookies


def test_logout_forgets_the_account_and_this_devices_box():
    r = asyncio.run(auth.h_logout(req("/logout", cookies=signed_in())))
    assert r.cookies["ghid"]["max-age"] == "0" and r.cookies[common.COOKIE]["max-age"] == "0"


@pytest.mark.parametrize("cookies", [{"pid": "../../etc"}, {"pid": "A" * 32}, {}])
def test_a_malformed_player_id_cookie_gets_a_fresh_id(cookies):
    for uid in (None, "7"):                     # anonymous, and a first sign-in
        common.DB_PATH = common.DB_PATH + "x"   # fresh account table each time
        pid = auth.player_id(req(cookies=cookies), uid)
        assert auth.PID_RE.fullmatch(pid) and pid != cookies.get("pid")
