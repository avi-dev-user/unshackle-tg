"""Round-trip + invariant tests for the persisted stores (auth / users / monitors) against a
throwaway STATE_DIR. Covers the security/durability fixes: _safe() path guard, per-user
isolation, encrypted credentials, the WVD magic check, the seen cap, and RBAC default-deny."""
import pytest

from src import auth, monitors, users


# --- auth: path-traversal guard ------------------------------------------------------------
def test_safe_accepts_tokens_rejects_traversal():
    assert auth._safe("MAKO") == "MAKO"
    assert auth._safe("12345-2") == "12345-2"
    for bad in ("../etc", "a/b", "..", "", "a.b", "x;y"):
        with pytest.raises(ValueError):
            auth._safe(bad)


# --- auth: JSON cookie export -> Netscape conversion ---------------------------------------
def test_json_cookies_to_netscape():
    import json
    arr = json.dumps([
        {"name": "sess", "value": "abc", "domain": ".example.com", "path": "/",
         "secure": True, "expirationDate": 1893456000.5, "hostOnly": False},
        {"name": "csrf", "value": "xyz", "domain": "web.example.com", "path": "/app",
         "secure": False, "hostOnly": True},
    ])
    out = auth.json_cookies_to_netscape(arr)
    assert auth.is_cookie_file(out)                                  # converts to a valid cookies.txt
    assert ".example.com\tTRUE\t/\tTRUE\t1893456000\tsess\tabc" in out   # subdomain cookie, float expiry floored
    assert "web.example.com\tFALSE\t/app\tFALSE\t0\tcsrf\txyz" in out    # hostOnly -> FALSE flag, session -> 0
    # Playwright/Puppeteer storage state shape
    pw = json.dumps({"cookies": [{"name": "t", "value": "v", "domain": "x.tv", "expires": 100}]})
    assert "x.tv\tFALSE\t/\tFALSE\t100\tt\tv" in auth.json_cookies_to_netscape(pw)
    # not JSON cookies -> None so the caller keeps the original text / errors cleanly
    assert auth.json_cookies_to_netscape('{"hello": 1}') is None
    assert auth.json_cookies_to_netscape("# Netscape HTTP Cookie File\n.x.com\tTRUE\t/\tTRUE\t0\ta\tb") is None
    # add_cookies accepts a JSON export end to end
    acct = auth.add_cookies(5005, "SVC", arr)
    assert auth.is_cookie_file(auth._cookie_path("SVC", acct["profile"]).read_text("utf-8"))


# --- auth: per-user credential isolation + encryption --------------------------------------
def test_credentials_are_per_user_and_encrypted():
    a = auth.add_credential(1001, "SVC", "alice", "secretpw")
    auth.add_credential(2002, "SVC", "bob", "bobpw")
    # user 1001 gets their own creds back, decrypted; user 2002 cannot see them
    assert auth.get_credential(1001, "SVC", a["profile"]) == "alice:secretpw"
    assert {x["profile"] for x in auth.list_accounts(2002, "SVC")} \
        .isdisjoint({x["profile"] for x in auth.list_accounts(1001, "SVC")})
    # the on-disk index must not contain the plaintext password
    assert "secretpw" not in auth._INDEX.read_text("utf-8")


# --- auth: CDM device validation -----------------------------------------------------------
def test_add_wvd_requires_signature_and_size():
    with pytest.raises(ValueError):
        auth.add_wvd(3003, b"x" * 10)                  # too small
    with pytest.raises(ValueError):
        auth.add_wvd(3003, b"junkjunk" * 20)           # no WVD/PRD signature
    acct = auth.add_wvd(3003, b"WVD" + b"\x00" * 100)  # valid signature
    assert acct["device"] in {w["profile"] for w in auth.list_wvd(3003)}


# --- monitors: seen cap + round-trip + atomic save -----------------------------------------
def test_monitor_seen_is_capped():
    m = monitors.add(4004, 4004, "SVC", "http://x", "Show", {}, seen=[], ts=0)
    monitors.mark_seen(m["id"], [f"ep{i}" for i in range(2500)])
    seen = monitors.get(m["id"])["seen"]
    assert len(seen) == 2000                            # capped
    assert seen[-1] == "ep2499"                         # newest kept (recency order)
    monitors.remove(m["id"])
    assert monitors.get(m["id"]) is None


def test_store_save_is_atomic_and_reloads(tmp_path):
    # the store file exists and reloads cleanly (atomic _save wrote it via tmp+replace)
    monitors.add(5005, 5005, "SVC", "http://y", "Y", {}, seen=[], ts=0)
    assert monitors._PATH.exists()
    import json
    json.loads(monitors._PATH.read_text())              # valid JSON, not truncated


# --- users: RBAC default-deny --------------------------------------------------------------
def test_unknown_user_is_denied():
    assert users.is_allowed(999999) is False
    assert users.service_allowed(999999, "SVC") is False


def test_added_user_permissions():
    u = users.add("700700", by=1, ts=0)
    uid = u["id"]
    assert users.service_allowed(uid, "ANY") is True            # perm_mode 'all' by default
    users.set_perm_mode(users.key(u), "only")
    users.toggle_perm_service(users.key(u), "ALLOWED")
    assert users.service_allowed(uid, "ALLOWED") is True
    assert users.service_allowed(uid, "OTHER") is False
    # attribute rules AND-combine
    users.set_perm_mode(users.key(u), "all")
    users.toggle_flag(users.key(u), "block_drm")
    assert users.service_allowed(uid, "X", has_drm=True) is False
    assert users.service_allowed(uid, "X", has_drm=False) is True


def test_gofile_mode_default_and_set():
    u = users.add("800800", by=1, ts=0)
    uid = u["id"]
    assert users.gofile_mode(uid) == "ask"                  # default: prompt each download
    assert users.gofile_mode(999999) == "ask"               # unknown user -> safe default
    users.set_gofile_mode(uid, "always")
    assert users.gofile_mode(uid) == "always"
    users.set_gofile_mode(uid, "never")
    assert users.gofile_mode(uid) == "never"
    assert users.set_gofile_mode(uid, "bogus") is None       # invalid mode rejected
    assert users.gofile_mode(uid) == "never"                 # unchanged after a bad set


def test_default_credential_resolves_only_for_default_profile():
    # The shared default credential is returned only when the DEFAULT_PROFILE is asked for - NOT
    # for a user's own profile id. launch_download must therefore re-resolve against DEFAULT_PROFILE
    # after it falls back to it (a user with no account of their own), or STING gets no credential.
    u = users.add("820820", by=1, ts=0)
    uid = u["id"]
    auth.set_default_credential("STING", "refresh-token-value")
    assert auth.has_default_credential("STING") is True
    assert auth.get_credential(uid, "STING", auth.DEFAULT_PROFILE)            # default profile -> found
    assert auth.get_credential(uid, "STING", str(uid)) is None               # own (empty) profile -> none
    # a user WITH their own account still gets their own credential, not the default
    auth.add_credential(uid, "STING", "me@example.test", "pw", label="mine")
    own = auth.get_credential(uid, "STING", str(uid))
    assert own and own.startswith("me@example.test:")


def test_delivery_mode_default_and_set():
    u = users.add("810810", by=1, ts=0)
    uid = u["id"]
    assert users.delivery_mode(uid) == "ask"                 # default: prompt each download
    assert users.delivery_mode(999998) == "ask"              # unknown user -> safe default
    users.set_delivery_mode(uid, "link")
    assert users.delivery_mode(uid) == "link"
    users.set_delivery_mode(uid, "ask")
    assert users.delivery_mode(uid) == "ask"
    assert users.set_delivery_mode(uid, "bogus") is None      # invalid mode rejected
    assert users.delivery_mode(uid) == "ask"                  # unchanged after a bad set


def test_lang_default_and_switch():
    u = users.add("800800", by=1, ts=0)
    assert users.lang(u["id"]) in ("en", "he")          # default (depends on DEFAULT_LANG env)
    users.set_lang(u["id"], "he")
    assert users.lang(u["id"]) == "he"
    users.set_lang(u["id"], "en")
    assert users.lang(u["id"]) == "en"
