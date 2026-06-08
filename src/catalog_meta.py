"""Service catalog logic: URL routing, per-service metadata, categorisation, and the
per-user permission check. Pure-ish helpers over the cached catalog in state.py - no
Telegram I/O. The menus/wizard read these to decide what a user may see and download.

Data-driven: no service names are hardcoded here. A service is categorised from (in order)
the admin override, the category the service declares in its own metadata, the deployment's
config seeds, then the needs_auth heuristic. URL routing uses the deployment's domain map
and each service's own title_regex/url. See config.CATEGORY_SEEDS / DOMAIN_SERVICES /
FREE_SERVICES / CATCHALL_SERVICE / FEED_SERVICE."""
import json
import re

from . import config, state, users

_CAT_OVERRIDE_PATH = config.STATE_DIR / "categories.json"
_cat_override: dict[str, str] = {}      # admin-set, persisted: {tag: "il"|"free"|"sub"}


def unwrap_url(url: str) -> str:
    """Branch/app.link/share smart-links wrap the real URL in a query param. Extract it
    so we route to the right service (e.g. cbstve.app.link?$canonical_url=cbs.com/...)."""
    from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlparse, urlsplit, urlunsplit
    try:
        q = parse_qs(urlparse(url).query)
        for k in ("$canonical_url", "canonical_url", "$desktop_url", "$ios_url", "url", "u"):
            v = q.get(k)
            if v and v[0].startswith("http"):
                url = unquote(v[0])
                break
    except Exception:
        pass
    # strip Branch/marketing tracking params (~channel, ~campaign, $..., utm_*) that confuse parsers
    try:
        s = urlsplit(url)
        kept = [(k, val) for k, val in parse_qsl(s.query)
                if not (k.startswith(("~", "$")) or k.startswith("utm_"))]
        url = urlunsplit((s.scheme, s.netloc, s.path, urlencode(kept), s.fragment))
    except Exception:
        pass
    return url


def detect_service(url: str) -> str | None:
    """Identify the service from a pasted URL - safely. Match only when the URL's HOST is
    claimed by a service (the deployment's domain map, or the host appears in a service's
    regex/url); feed URLs go to the configured feed service. Avoids false matches to
    credential services. Returns the configured catch-all (e.g. a yt-dlp service) for any
    other http link, or None if none is configured."""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower().replace("www.", "")
    low = url.lower()
    for dom, tag in config.DOMAIN_SERVICES.items():
        if dom in host:
            return tag
    if config.FEED_SERVICE and any(k in low for k in ("/feed", "rss", ".xml", "podcast")):
        return config.FEED_SERVICE
    if host:
        cands = {host}
        parts = host.split(".")
        if len(parts) >= 2:
            cands.add(".".join(parts[-2:]))      # registrable domain (subdomain-agnostic)
        for s in state.services_cached():
            blob = f"{s.get('title_regex')} {s.get('url')}".replace("\\", "").lower()
            if any(c in blob for c in cands):    # a service's pattern names this host/domain
                return s["tag"]
        # a catch-all service (e.g. yt-dlp, ~1800 sites) handles any other site, if configured
        return config.CATCHALL_SERVICE or None
    return None


def svc_desc(tag: str) -> str:
    """Short human description for a service (from its docstring)."""
    return (state.meta(tag).get("description") or "").strip()


def svc_link(tag: str) -> str:
    """Best-effort homepage link: short_help URL → URL in docstring → domain in title_regex."""
    m = state.meta(tag)
    u = m.get("url") or ""
    if u.startswith("http"):
        return u
    mt = re.search(r'https?://[^\s)\'"]+', m.get("help", "") or "")
    if mt:
        return mt.group(0).rstrip(').,')
    tr = m.get("title_regex")
    blob = (" ".join(tr) if isinstance(tr, list) else (tr or "")).replace("\\", "")
    dm = re.search(r'([a-z0-9-]+\.(?:com|net|tv|io|fm|org|co\.il|co|to))', blob, re.I)
    return f"https://{dm.group(1)}" if dm else ""


def svc_needs_auth(tag: str) -> bool:
    return bool(state.meta(tag).get("needs_auth"))


def svc_auth_methods(tag: str) -> list[str]:
    """Which auth a service accepts - auto-detected by unshackle ('cookies'/'credentials')."""
    return state.meta(tag).get("auth_methods") or (["cookies"] if svc_needs_auth(tag) else [])


def load_cat_overrides() -> None:
    global _cat_override
    if _CAT_OVERRIDE_PATH.exists():
        try:
            _cat_override = json.loads(_CAT_OVERRIDE_PATH.read_text("utf-8"))
        except Exception:
            _cat_override = {}


def set_cat_override(tag: str, cat: str) -> None:
    _cat_override[tag] = cat
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    _CAT_OVERRIDE_PATH.write_text(json.dumps(_cat_override, ensure_ascii=False, indent=2), "utf-8")


def categorise(tag: str) -> str:
    if tag in _cat_override:                # admin override wins (set from the bot, no code edit)
        return _cat_override[tag]
    declared = (state.meta(tag).get("category") or "").lower()   # service self-declares
    if declared in ("il", "free", "sub"):
        return declared
    if tag in config.CATEGORY_SEEDS:        # deployment-configured seed
        return config.CATEGORY_SEEDS[tag]
    # free-with-optional-login / ad-supported: their needs_auth is optional, so the
    # heuristic below would wrongly bucket them as paid.
    if tag in config.FREE_SERVICES:
        return "free"
    return "sub" if svc_needs_auth(tag) else "free"   # accurate: derived from the service


def can_use(uid: int, tag: str) -> bool:
    """Full per-user permission check, with the service's live attributes."""
    return users.service_allowed(uid, tag, needs_auth=svc_needs_auth(tag),
                                 has_drm=bool(state.meta(tag).get("has_drm")),
                                 category=categorise(tag))
