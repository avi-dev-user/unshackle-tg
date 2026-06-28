"""
User registry + access control (RBAC), persisted to STATE_DIR/users.json.

Rules:
- The bot serves ONLY known, active users. Anyone else is ignored completely
  (no reply, no "access requested" - as if the bot doesn't exist for them).
- Bootstrap admins come from .env ADMIN_IDS and are always admin + active.
- Each user has a per-service permission policy: all / only <list> / except <list>.
- A user can be added by numeric id (works immediately) or by @username
  (pending: the Bot API can't resolve a username to an id, so the entry is
  bound to the real id the first time that username contacts the bot).
"""
import json
import os
import time

from . import config

_PATH = config.STATE_DIR / "users.json"
_users: list[dict] = []          # in-memory cache; each dict is one user record
_seen: dict[int, str] = {}       # every chat_id that EVER contacted the bot -> name (for broadcast)


def _save() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.parent / (_PATH.name + ".tmp")     # atomic write: a crash mid-write can't truncate
    tmp.write_text(json.dumps(
        {"users": _users, "seen": [{"id": i, "name": n} for i, n in _seen.items()]},
        ensure_ascii=False, indent=2))
    os.replace(tmp, _PATH)


def load() -> None:
    """Load the store and ensure every bootstrap admin exists as an admin record."""
    global _users, _seen
    if _PATH.exists():
        try:
            doc = json.loads(_PATH.read_text())
            _users = doc.get("users", [])
            _seen = {e["id"]: e.get("name", "") for e in doc.get("seen", []) if e.get("id") is not None}
        except (ValueError, OSError):
            _users, _seen = [], {}
    else:
        _users, _seen = [], {}
    changed = False
    for aid in config.ADMIN_IDS:
        u = _by_id(aid)
        if u is None:
            _users.append(_new_record(id=aid, name="admin", is_admin=True, added_by=aid))
            changed = True
        elif not u.get("is_admin") or u.get("status") != "active":
            u["is_admin"], u["status"] = True, "active"
            changed = True
    if changed:
        _save()


def _new_record(id=None, username="", name="", is_admin=False, added_by=0) -> dict:
    return {
        "id": id,
        "username": (username or "").lstrip("@").lower(),
        "name": name,
        "status": "active",                 # active | suspended
        "is_admin": bool(is_admin),
        "perm_mode": "all",                  # all | only | except
        "perm_services": [],
        "added_at": 0,                       # stamped by caller (no Date in workflows, fine here)
        "added_by": added_by,
    }


# --- lookups ---------------------------------------------------------------
def _by_id(tg_id: int) -> dict | None:
    return next((u for u in _users if u.get("id") == tg_id), None)


def _by_username(username: str) -> dict | None:
    un = (username or "").lstrip("@").lower()
    return next((u for u in _users if u.get("id") is None and u.get("username") == un), None) if un else None


def get(tg_id: int) -> dict | None:
    return _by_id(tg_id)


def all_users() -> list[dict]:
    """All records, admins first then by name/username/id - stable for paging."""
    return sorted(_users, key=lambda u: (not u.get("is_admin"),
                                         (u.get("name") or u.get("username") or str(u.get("id") or "")).lower()))


def label(u: dict) -> str:
    """Human label for a list row."""
    name = u.get("name") or (f"@{u['username']}" if u.get("username") else "") or str(u.get("id") or "?")
    mark = "🛡️" if u.get("is_admin") else ("⏸️" if u.get("status") != "active" else "🟢")
    if u.get("id") is None:
        mark = "🕓"                          # pending username claim
    return f"{mark} {name}"


def key(u: dict) -> str:
    """Stable callback key for a record (id if known, else username)."""
    return str(u["id"]) if u.get("id") is not None else f"@{u['username']}"


def by_key(k: str) -> dict | None:
    return _by_username(k[1:]) if k.startswith("@") else (_by_id(int(k)) if k.lstrip("-").isdigit() else None)


# --- access checks ---------------------------------------------------------
def is_allowed(tg_id: int) -> bool:
    if tg_id in config.ADMIN_IDS:
        return True
    u = _by_id(tg_id)
    return bool(u and u.get("status") == "active")


def is_admin(tg_id: int) -> bool:
    if tg_id in config.ADMIN_IDS:
        return True
    u = _by_id(tg_id)
    return bool(u and u.get("is_admin") and u.get("status") == "active")


def _mode_ok(u: dict, tag: str) -> bool:
    """The explicit-list rule: all / only <list> / except <list>."""
    mode = u.get("perm_mode", "all")
    svcs = set(u.get("perm_services") or [])
    if mode == "only":
        return tag in svcs
    if mode == "except":
        return tag not in svcs
    return True


def can_use(tg_id: int, tag: str) -> bool:
    """Explicit-list check only (no service metadata). Prefer service_allowed()."""
    if is_admin(tg_id):
        return True
    u = _by_id(tg_id)
    if not u or u.get("status") != "active":
        return False
    return _mode_ok(u, tag)


def service_allowed(tg_id: int, tag: str, *, needs_auth: bool = False,
                    has_drm: bool = False, category: str = None) -> bool:
    """Full check: explicit-list rule AND attribute rules (block login / block DRM /
    allowed categories). A service that fails ANY rule is denied (and hidden)."""
    if is_admin(tg_id):
        return True
    u = _by_id(tg_id)
    if not u or u.get("status") != "active":
        return False
    if not _mode_ok(u, tag):
        return False
    if u.get("block_auth") and needs_auth:
        return False
    if u.get("block_drm") and has_drm:
        return False
    cats = u.get("cats") or []
    if cats and category not in cats:        # empty = all categories allowed
        return False
    return True


def allowed_services(tg_id: int, all_tags: list[str]) -> list[str]:
    return [t for t in all_tags if can_use(tg_id, t)]


# --- mutations -------------------------------------------------------------
def see(from_obj: dict) -> None:
    """Record ANY chat that contacts the bot (even unapproved) so we can broadcast
    to 'everyone who ever started the bot'. Called before the access gate."""
    tg_id = from_obj.get("id")
    if tg_id is None:
        return
    name = (" ".join(x for x in (from_obj.get("first_name"), from_obj.get("last_name")) if x).strip()
            or (f"@{from_obj['username']}" if from_obj.get("username") else str(tg_id)))
    if _seen.get(tg_id) != name:
        _seen[tg_id] = name
        _save()


def seen_ids() -> list[int]:
    """Everyone who ever contacted the bot (approved or not)."""
    return list(_seen.keys())


def active_ids() -> list[int]:
    """Approved, active users with a known id (broadcast: approved only)."""
    return [u["id"] for u in _users if u.get("id") is not None and u.get("status") == "active"]


def touch(from_obj: dict) -> None:
    """Refresh a known user's display name/username from a Telegram `from` object,
    and bind a pending @username invite to its real id on first contact."""
    tg_id = from_obj.get("id")
    if tg_id is None:
        return
    name = " ".join(x for x in (from_obj.get("first_name"), from_obj.get("last_name")) if x).strip()
    uname = (from_obj.get("username") or "").lower()
    u = _by_id(tg_id)
    if u is None and uname:                  # claim a pending invite
        pend = _by_username(uname)
        if pend is not None:
            pend["id"] = tg_id
            u = pend
    if u is None:
        return
    changed = False
    if name and u.get("name") != name:
        u["name"], changed = name, True
    if uname and u.get("username") != uname:
        u["username"], changed = uname, True
    if changed:
        _save()


def add(identifier: str, by: int, ts: int = 0) -> dict:
    """Add a user by numeric id or @username. Returns the record (existing or new)."""
    ident = identifier.strip()
    if ident.lstrip("-").isdigit():
        tg_id = int(ident)
        u = _by_id(tg_id)
        if u is None:
            u = _new_record(id=tg_id, added_by=by)
            u["added_at"] = ts
            _users.append(u)
            _save()
        return u
    un = ident.lstrip("@").lower()
    if not un:
        raise ValueError("Invalid identifier - send a numeric ID or @username.")
    u = _by_username(un) or next((x for x in _users if x.get("username") == un), None)
    if u is None:
        u = _new_record(username=un, added_by=by)
        u["added_at"] = ts
        _users.append(u)
        _save()
    return u


def remove(k: str) -> bool:
    u = by_key(k)
    if u is None:
        return False
    if u.get("id") in config.ADMIN_IDS:
        raise ValueError("Cannot delete a super admin (defined in .env).")
    _users.remove(u)
    _save()
    return True


def set_status(k: str, status: str) -> dict | None:
    u = by_key(k)
    if u is None:
        return None
    if status != "active" and u.get("id") in config.ADMIN_IDS:
        raise ValueError("Cannot suspend a super admin (defined in .env).")
    u["status"] = status
    _save()
    return u


def set_admin(k: str, value: bool) -> dict | None:
    u = by_key(k)
    if u is None:
        return None
    if not value and u.get("id") in config.ADMIN_IDS:
        raise ValueError("Cannot revoke super admin (defined in .env).")
    u["is_admin"] = bool(value)
    _save()
    return u


def set_perm_mode(k: str, mode: str) -> dict | None:
    u = by_key(k)
    if u is None or mode not in ("all", "only", "except"):
        return None
    u["perm_mode"] = mode
    if mode == "all":
        u["perm_services"] = []
    _save()
    return u


# boolean per-user flags the admin can toggle (extensible: add 'use_default_cookies' etc.)
_FLAGS = ("block_auth", "block_drm", "use_default_cdm", "can_monitor", "can_gofile_upload",
          "can_keys_download")

DEFAULT_CONCURRENCY = 1          # simultaneous downloads for a normal user (a whole
#                                  season/album/podcast = ONE job = one slot)
ADMIN_CONCURRENCY = 99


def concurrency_limit(tg_id: int) -> int:
    """How many simultaneous downloads this user may run."""
    if is_admin(tg_id):
        return ADMIN_CONCURRENCY
    u = _by_id(tg_id)
    return int((u or {}).get("max_concurrent") or DEFAULT_CONCURRENCY)


def set_concurrency(k: str, n: int) -> dict | None:
    u = by_key(k)
    if u is None:
        return None
    u["max_concurrent"] = max(1, min(int(n), 20))
    _save()
    return u


def lang(tg_id: int) -> str:
    """The user's UI language ('en' default, 'he'). Unknown users get the default."""
    from .i18n import DEFAULT_LANG, LANGS
    u = _by_id(tg_id)
    code = (u or {}).get("lang") or DEFAULT_LANG
    return code if code in LANGS else DEFAULT_LANG


def set_lang(tg_id: int, code: str) -> dict | None:
    from .i18n import LANGS
    u = _by_id(tg_id)
    if u is None or code not in LANGS:
        return None
    u["lang"] = code
    _save()
    return u


def note_recent(tg_id: int, tag: str) -> None:
    """Record a service the user just used → front of their recent list (cap 8)."""
    u = _by_id(tg_id)
    if u is None:
        return
    u["recent"] = ([tag] + [t for t in (u.get("recent") or []) if t != tag])[:8]
    _save()


def recent(tg_id: int) -> list[str]:
    u = _by_id(tg_id)
    return (u or {}).get("recent") or []


GOFILE_MODES = ("ask", "always", "never")


def gofile_mode(tg_id: int) -> str:
    """How this user wants the extra gofile download link handled per download:
    'ask' (default) - prompt each time; 'always' - always upload; 'never' - never."""
    u = _by_id(tg_id)
    m = (u or {}).get("gofile_mode")
    return m if m in GOFILE_MODES else "ask"


def set_gofile_mode(tg_id: int, mode: str) -> dict | None:
    """Set the user's gofile-link preference (their own setting, not an admin flag)."""
    u = _by_id(tg_id)
    if u is None or mode not in GOFILE_MODES:
        return None
    u["gofile_mode"] = mode
    _save()
    return u


# release-group tag the user wants appended to output filenames (scene-style "-TAG").
_TAG_ALLOWED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
TAG_MAX_LEN = 24


def sanitize_tag(raw: str) -> str:
    """Keep only filename-safe tag characters and cap the length. '' means 'no tag'."""
    return "".join(c for c in (raw or "").strip() if c in _TAG_ALLOWED)[:TAG_MAX_LEN]


def tag_pref(tg_id: int) -> str:
    """The user's preferred group tag ('' = use the server default from unshackle.yaml)."""
    u = _by_id(tg_id)
    return (u or {}).get("group_tag") or ""


def set_tag_pref(tg_id: int, raw: str) -> str | None:
    """Set (or clear, when sanitized to '') the user's group tag.
    Returns the stored tag, or None for an unknown user."""
    u = _by_id(tg_id)
    if u is None:
        return None
    tag = sanitize_tag(raw)
    if tag:
        u["group_tag"] = tag
    else:
        u.pop("group_tag", None)
    _save()
    return tag


def can_monitor(tg_id: int) -> bool:
    """May this user set up auto-monitors? Admins always; others by flag."""
    if is_admin(tg_id):
        return True
    u = _by_id(tg_id)
    return bool(u and u.get("status") == "active" and u.get("can_monitor"))


def can_gofile_upload(tg_id: int) -> bool:
    """May this user use the direct 'upload a file to gofile' action? Admins always; others
    by the grantable per-user flag (it lets a user publish arbitrary files via our token)."""
    if is_admin(tg_id):
        return True
    u = _by_id(tg_id)
    return bool(u and u.get("status") == "active" and u.get("can_gofile_upload"))


def can_keys_download(tg_id: int) -> bool:
    """May this user download from a manifest + supplied content keys (no license server)?
    Admins always; others by the grantable per-user flag."""
    if is_admin(tg_id):
        return True
    u = _by_id(tg_id)
    return bool(u and u.get("status") == "active" and u.get("can_keys_download"))


def toggle_flag(k: str, flag: str) -> dict | None:
    """Toggle a boolean per-user flag (see _FLAGS)."""
    u = by_key(k)
    if u is None or flag not in _FLAGS:
        return None
    u[flag] = not u.get(flag)
    _save()
    return u


def can_use_default_cdm(tg_id: int) -> bool:
    """May this user fall back to the SHARED default CDM? Admins always; others by flag."""
    if is_admin(tg_id):
        return True
    u = _by_id(tg_id)
    return bool(u and u.get("status") == "active" and u.get("use_default_cdm"))


def toggle_cat(k: str, cat: str) -> dict | None:
    """Toggle an allowed category (il/free/sub). Empty set = all categories allowed."""
    u = by_key(k)
    if u is None or cat not in ("il", "free", "sub"):
        return None
    cats = set(u.get("cats") or [])
    cats.symmetric_difference_update({cat})
    u["cats"] = sorted(cats)
    _save()
    return u


def toggle_perm_service(k: str, tag: str) -> dict | None:
    u = by_key(k)
    if u is None:
        return None
    svcs = set(u.get("perm_services") or [])
    svcs.symmetric_difference_update({tag})
    u["perm_services"] = sorted(svcs)
    _save()
    return u
