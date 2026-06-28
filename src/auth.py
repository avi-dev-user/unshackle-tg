"""
Per-user, multi-account auth store.

Each user can add MULTIPLE accounts (cookie sets) per service - exactly like
real life (e.g. two Netflix accounts). Each account maps to an unshackle
*profile*, and its cookies live where unshackle reads them:

    cookies/<SERVICE>/<profile>.txt

profile = "<tg_id>"          for the first account
        = "<tg_id>-<n>"      for additional accounts
The bot passes the chosen profile to the download API, so unshackle uses that
exact account's cookies. A small index tracks human labels per account.

Cookies are stored as plaintext (unshackle reads them directly) with strict
permissions (file 600, dir 700). Credentials (user:pass) - future - go in the
Fernet-encrypted creds store.
"""
import json
import os
import re
from pathlib import Path

from . import config

try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None

_INDEX = config.STATE_DIR / "accounts.json"


def _fernet():
    """Fernet cipher from ENCRYPTION_KEY - used to encrypt user:pass at rest."""
    if Fernet is None or not config.ENCRYPTION_KEY:
        raise ValueError("Encryption unavailable (ENCRYPTION_KEY missing or the cryptography package is not installed).")
    return Fernet(config.ENCRYPTION_KEY.encode() if isinstance(config.ENCRYPTION_KEY, str)
                  else config.ENCRYPTION_KEY)


def _load() -> dict:
    if _INDEX.exists():
        try:
            return json.loads(_INDEX.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def _save(idx: dict) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _INDEX.parent / (_INDEX.name + ".tmp")   # atomic write: a crash mid-write can't truncate
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, _INDEX)
    os.chmod(_INDEX, 0o600)


_SAFE_NAME = re.compile(r"[A-Za-z0-9_-]+")


def _safe(name: str) -> str:
    """Reject service/profile names that aren't a plain token, to stop path traversal
    (these come from callback data and are used to build filesystem paths)."""
    if not _SAFE_NAME.fullmatch(str(name or "")):
        raise ValueError("Invalid service or account name.")
    return str(name)


def _cookie_path(service: str, profile: str) -> Path:
    d = config.COOKIES_DIR / _safe(service)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d / f"{_safe(profile)}.txt"


def list_accounts(tg_id: int, service: str) -> list[dict]:
    """Accounts a user has for a service: [{label, profile}]."""
    return _load().get(str(tg_id), {}).get(service, [])


def _next_profile(tg_id: int, service: str) -> str:
    existing = list_accounts(tg_id, service)
    if not existing:
        return str(tg_id)
    n = len(existing) + 1
    return f"{tg_id}-{n}"


def is_cookie_file(text: str) -> bool:
    """Loose check that text looks like a Netscape cookies.txt."""
    if "# Netscape HTTP Cookie File" in text or "# HTTP Cookie File" in text:
        return True
    # at least one TAB-separated line with 6+ fields
    return any(len(line.split("\t")) >= 6 for line in text.splitlines() if not line.startswith("#"))


def json_cookies_to_netscape(text: str) -> str | None:
    """Convert a JSON cookie export to a Netscape cookies.txt, or None if it isn't JSON cookies.

    Accepts the common browser-extension formats (Cookie-Editor / EditThisCookie - a JSON array
    of {name,value,domain,path,...}) and Playwright/Puppeteer storage state ({"cookies":[...]}).
    Lets a user paste/upload the JSON they exported instead of hand-converting to Netscape."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        data = data.get("cookies") or data.get("Cookies")
    if not isinstance(data, list):
        return None
    lines = ["# Netscape HTTP Cookie File"]
    for c in data:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        domain = str(c.get("domain") or c.get("Domain") or "").strip()
        if not domain:
            continue
        host_only = c.get("hostOnly")
        if host_only is None:
            host_only = not domain.startswith(".")
        if not host_only and not domain.startswith("."):
            domain = "." + domain                       # leading dot = applies to subdomains
        flag = "FALSE" if host_only else "TRUE"
        path = str(c.get("path") or "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        raw_exp = c.get("expirationDate") or c.get("expires") or c.get("expiry") or 0
        try:
            expiry = max(0, int(float(raw_exp)))
        except (ValueError, TypeError):
            expiry = 0
        lines.append("\t".join([domain, flag, path, secure, str(expiry),
                                str(c["name"]), str(c.get("value", ""))]))
    return "\n".join(lines) + "\n" if len(lines) > 1 else None


def add_cookies(tg_id: int, service: str, cookie_text: str, label: str = None) -> dict:
    """Save a new cookie account for the user. Returns the account dict."""
    cookie_text = json_cookies_to_netscape(cookie_text) or cookie_text   # accept JSON exports too
    if not is_cookie_file(cookie_text):
        raise ValueError("This doesn't look like a Netscape cookies.txt or a JSON cookie export.")
    profile = _next_profile(tg_id, service)
    path = _cookie_path(service, profile)
    path.write_text(cookie_text, "utf-8")
    os.chmod(path, 0o600)

    idx = _load()
    user = idx.setdefault(str(tg_id), {})
    accounts = user.setdefault(service, [])
    label = (label or "").strip() or f"Account {len(accounts) + 1}"
    account = {"label": label, "profile": profile, "kind": "cookies"}
    accounts.append(account)
    _save(idx)
    return account


DEFAULT_PROFILE = "_default"   # shared admin-provided cookies, used when a user has none of their own


def set_default_cookies(service: str, cookie_text: str) -> None:
    """Admin: store shared cookies for a service (used as a fallback for users without their own).
    Fixes e.g. YouTube 'confirm you're not a bot' on catch-all downloads."""
    cookie_text = json_cookies_to_netscape(cookie_text) or cookie_text   # accept JSON exports too
    if not is_cookie_file(cookie_text):
        raise ValueError("This doesn't look like a Netscape cookies.txt or a JSON cookie export.")
    path = _cookie_path(service, DEFAULT_PROFILE)
    path.write_text(cookie_text, "utf-8")
    os.chmod(path, 0o600)


def has_default_cookies(service: str) -> bool:
    try:
        return (config.COOKIES_DIR / _safe(service) / f"{DEFAULT_PROFILE}.txt").exists()
    except ValueError:
        return False


def add_credential(tg_id: int, service: str, username: str, password: str, label: str = None) -> dict:
    """Save a username+password account, encrypted at rest. No cookie file is written;
    the secret lives only as a Fernet token in the index. Returns the account dict."""
    if not (username and password):
        raise ValueError("Both a username and a password are required (format user:pass).")
    token = _fernet().encrypt(f"{username}:{password}".encode()).decode()
    profile = _next_profile(tg_id, service)
    idx = _load()
    accounts = idx.setdefault(str(tg_id), {}).setdefault(service, [])
    label = (label or "").strip() or username or f"Account {len(accounts) + 1}"
    account = {"label": label, "profile": profile, "kind": "creds", "enc": token}
    accounts.append(account)
    _save(idx)
    return account


def add_refresh_token(tg_id: int, service: str, refresh_token: str, label: str = None) -> dict:
    """Save a refresh-token credential (from a device-flow login, e.g. STING Android-TV).

    Stored as ``refresh:<token>:refresh`` so the engine's Credential parser accepts it
    (it rejects an empty username) and the service runs its refresh-login mode via the
    ``refresh`` extra. Encrypted at rest like any other credential."""
    if not refresh_token:
        raise ValueError("A refresh token is required.")
    token = _fernet().encrypt(f"refresh:{refresh_token}:refresh".encode()).decode()
    profile = _next_profile(tg_id, service)
    idx = _load()
    accounts = idx.setdefault(str(tg_id), {}).setdefault(service, [])
    account = {"label": (label or "").strip() or "TV login", "profile": profile, "kind": "creds", "enc": token}
    accounts.append(account)
    _save(idx)
    return account


_SHARED = "_shared"  # synthetic index key for admin-provided shared/default credentials


def set_default_credential(service: str, refresh_token: str, label: str = "Shared (default)") -> dict:
    """Admin: store a shared refresh-token credential for a service, used as a fallback for
    users who have no account of their own (mirror of set_default_cookies). One per service."""
    if not refresh_token:
        raise ValueError("A refresh token is required.")
    enc = _fernet().encrypt(f"refresh:{refresh_token}:refresh".encode()).decode()
    idx = _load()
    idx.setdefault(_SHARED, {})[service] = [
        {"label": label, "profile": DEFAULT_PROFILE, "kind": "creds", "enc": enc}
    ]
    _save(idx)
    return idx[_SHARED][service][0]


def has_default_credential(service: str) -> bool:
    return bool(_load().get(_SHARED, {}).get(service))


def _default_credential(service: str) -> str | None:
    for a in _load().get(_SHARED, {}).get(service, []):
        if a.get("kind") == "creds" and a.get("enc"):
            return _fernet().decrypt(a["enc"].encode()).decode()
    return None


def get_credential(tg_id: int, service: str, profile: str) -> str | None:
    """Decrypt and return 'user:pass' for a credentials-account, or None.
    Falls back to the shared default credential when the default profile is in use."""
    for a in list_accounts(tg_id, service):
        if a.get("profile") == profile and a.get("kind") == "creds" and a.get("enc"):
            return _fernet().decrypt(a["enc"].encode()).decode()
    if profile == DEFAULT_PROFILE:
        return _default_credential(service)
    return None


def first_credential(tg_id: int, service: str) -> str | None:
    """The user's 'user:pass' from their first credentials-account for a service, for read
    calls (search / list-titles / list-tracks) that run before a specific account is picked.
    Falls back to the shared default credential, else None."""
    for a in list_accounts(tg_id, service):
        if a.get("kind") == "creds" and a.get("enc"):
            return _fernet().decrypt(a["enc"].encode()).decode()
    return _default_credential(service)


def list_wvd(tg_id: int) -> list[dict]:
    """A user's CDM (.wvd) devices: [{label, profile(=device name), kind, device}]."""
    return _load().get(str(tg_id), {}).get("_CDM", [])


def add_wvd(tg_id: int, data: bytes, label: str = None, ext: str = ".wvd") -> dict:
    """Save a user-uploaded CDM device (.wvd Widevine / .prd PlayReady) under a unique
    name. The device NAME (stem) is passed to unshackle as `cdm`; unshackle resolves
    <name>.prd or <name>.wvd itself."""
    ext = (ext or ".wvd").lower()
    if ext not in (".wvd", ".prd"):
        raise ValueError("A CDM file must be .wvd (Widevine) or .prd (PlayReady).")
    if not data or len(data) < 50:
        raise ValueError("The CDM file is invalid (empty/too small).")
    # serialized devices start with a format signature (pywidevine "WVD" / pyplayready "PRD"):
    # reject anything else early instead of storing a junk "device" that fails opaquely later.
    if data[:3] not in (b"WVD", b"PRD"):
        raise ValueError("That doesn't look like a valid CDM device (.wvd / .prd) file.")
    existing = list_wvd(tg_id)
    n = len(existing) + 1
    device = f"u{tg_id}" if not existing else f"u{tg_id}-{n}"
    config.WVD_DIR.mkdir(parents=True, exist_ok=True)
    path = config.WVD_DIR / f"{device}{ext}"
    path.write_bytes(data)
    os.chmod(path, 0o600)
    idx = _load()
    accts = idx.setdefault(str(tg_id), {}).setdefault("_CDM", [])
    kind_label = "PlayReady" if ext == ".prd" else "Widevine"
    label = (label or "").strip() or f"{kind_label} {n}"
    acct = {"label": label, "profile": device, "kind": "cdm", "device": device, "ext": ext}
    accts.append(acct)
    _save(idx)
    return acct


def wvd_device(tg_id: int, profile: str) -> str | None:
    """The device name for a chosen CDM (if its file still exists), to pass as `cdm`."""
    for a in list_wvd(tg_id):
        if a["profile"] == profile and (config.WVD_DIR / f"{a['device']}{a.get('ext', '.wvd')}").exists():
            return a["device"]
    return None


def remove_wvd(tg_id: int, profile: str) -> bool:
    idx = _load()
    accts = idx.get(str(tg_id), {}).get("_CDM", [])
    gone = next((a for a in accts if a["profile"] == profile), None)
    if gone is None:
        return False
    idx[str(tg_id)]["_CDM"] = [a for a in accts if a["profile"] != profile]
    _save(idx)
    p = config.WVD_DIR / f"{profile}{gone.get('ext', '.wvd')}"
    if p.exists():
        p.unlink()
    return True


def remove_account(tg_id: int, service: str, profile: str) -> bool:
    idx = _load()
    accounts = idx.get(str(tg_id), {}).get(service, [])
    new = [a for a in accounts if a["profile"] != profile]
    if len(new) == len(accounts):
        return False
    idx[str(tg_id)][service] = new
    _save(idx)
    p = _cookie_path(service, profile)
    if p.exists():
        p.unlink()
    return True


def rename_account(tg_id: int, service: str, profile: str, label: str) -> bool:
    idx = _load()
    for a in idx.get(str(tg_id), {}).get(service, []):
        if a["profile"] == profile:
            a["label"] = label.strip() or a["label"]
            _save(idx)
            return True
    return False


def user_services(tg_id: int) -> dict:
    """All services this user configured → {service: [accounts]}."""
    return _load().get(str(tg_id), {})
