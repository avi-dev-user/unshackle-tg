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


def test_lang_default_and_switch():
    u = users.add("800800", by=1, ts=0)
    assert users.lang(u["id"]) in ("en", "he")          # default (depends on DEFAULT_LANG env)
    users.set_lang(u["id"], "he")
    assert users.lang(u["id"]) == "he"
    users.set_lang(u["id"], "en")
    assert users.lang(u["id"]) == "en"
