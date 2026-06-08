"""
Auto-monitor store: a user watches a source (series/podcast URL). A background loop
periodically lists titles and downloads any episode it hasn't seen before, sending it
to the user. Persisted to STATE_DIR/monitors.json. Permission-gated by users.can_monitor.
"""
import json
import os

from . import config

_PATH = config.STATE_DIR / "monitors.json"
_mons: list[dict] = []


def load() -> None:
    global _mons
    if _PATH.exists():
        try:
            _mons = json.loads(_PATH.read_text()).get("monitors", [])
        except (ValueError, OSError):
            _mons = []
    else:
        _mons = []


def _save() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.parent / (_PATH.name + ".tmp")     # atomic write: a crash mid-write can't truncate
    tmp.write_text(json.dumps({"monitors": _mons}, ensure_ascii=False, indent=2))
    os.replace(tmp, _PATH)


def all_monitors() -> list[dict]:
    return list(_mons)


def user_monitors(tg_id: int) -> list[dict]:
    return [m for m in _mons if m.get("uid") == tg_id]


def get(mon_id: str) -> dict | None:
    return next((m for m in _mons if m.get("id") == mon_id), None)


def add(uid: int, chat: int, service: str, title_id: str, name: str, params: dict,
        seen: list, interval: int = 1800, ts: int = 0, interval_max: int = 0,
        schedule: dict = None) -> dict:
    """Create a monitor. `seen` = episode keys to treat as already-handled (everything
    NOT in seen will be downloaded). `interval` = scan period in seconds; `interval_max` > 0
    makes it a random range [interval, interval_max] re-rolled each cycle; `schedule`
    ({'at':'HH:MM','days':[...]|None}) overrides the interval with a daily/weekly fixed time.
    `params` = the download options to reuse each time."""
    n = len(user_monitors(uid)) + 1
    m = {
        "id": f"{uid}-{n}-{ts}", "uid": uid, "chat": chat, "service": service,
        "title_id": title_id, "name": name, "params": params,
        "seen": list(seen), "interval": int(interval), "interval_max": int(interval_max or 0),
        "schedule": schedule, "added_at": ts,
    }
    _mons.append(m)
    _save()
    return m


def remove(mon_id: str) -> bool:
    global _mons
    before = len(_mons)
    _mons = [m for m in _mons if m.get("id") != mon_id]
    if len(_mons) == before:
        return False
    _save()
    return True


def set_interval(mon_id: str, seconds: int, interval_max: int = 0) -> dict | None:
    m = get(mon_id)
    if m is not None:
        m["interval"] = int(seconds)
        m["interval_max"] = int(interval_max or 0)
        m["schedule"] = None                 # choosing an interval clears any fixed schedule
        _save()
    return m


def set_schedule(mon_id: str, schedule: dict) -> dict | None:
    m = get(mon_id)
    if m is not None:
        m["schedule"] = schedule
        _save()
    return m


def set_mode(mon_id: str, mode: str) -> dict | None:
    return set_param(mon_id, "mode", mode)


def set_param(mon_id: str, key: str, value) -> dict | None:
    """Set a single download param on a monitor (mode / send_as / cover / ...)."""
    m = get(mon_id)
    if m is not None:
        m.setdefault("params", {})[key] = value
        _save()
    return m


def mark_seen(mon_id: str, keys: list) -> None:
    m = get(mon_id)
    if m is not None:
        seen = list(m.get("seen") or [])
        for k in keys:                             # keep recency order; newest appended last
            if k not in seen:
                seen.append(k)
        m["seen"] = seen[-2000:]                   # cap so a long-running feed can't grow it unbounded
        _save()
