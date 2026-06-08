"""Auto-monitor: the background scheduler that polls each saved monitor at its own
interval/schedule and downloads new episodes, plus the inline UI to create and edit
monitors. The scheduler state (_mon_last/_mon_iv) is mutated in place and shared with
the dispatch, which clears it when a monitor's interval changes."""
import asyncio
import datetime as _dt
import html
import random
import re
import time
from zoneinfo import ZoneInfo

from . import config, monitors, state, users
from .catalog_meta import can_use, detect_service
from .download import build_flags, launch_download, sel_label, to_sel
from .engine import UnshackleError
from .errors import report_error, user_error
from .format import _fmt_eta
from .i18n import tr
from .session import active_jobs, sess
from .state import engine
from .tg import PAGE, edit, grid_rows, send

MONITOR_INTERVAL = 1800            # default per-monitor scan period (30 min)
MONITOR_TICK = 60                  # base loop tick - each monitor runs at its own interval
_mon_last: dict = {}               # monitor id -> last-check epoch
_mon_iv: dict = {}                 # monitor id -> interval chosen for the current cycle (random if range)


def _parse_interval(text: str):
    """Parse a monitor interval. Supports composite durations ('4d3h2s', '55h', '30m', '90s'),
    a range that re-rolls each cycle ('6-7h', '90-120m'), or a bare number (minutes, back-compat).
    Returns (min_seconds, max_seconds_or_None), or (None, None) if unparseable / under 30s."""
    text = (text or "").strip().lower().replace(" ", "")
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}

    def to_sec(part):
        toks = re.findall(r"(\d+)([dhms])", part)
        if toks and re.fullmatch(r"(\d+[dhms])+", part):
            return sum(int(n) * units[u] for n, u in toks)
        return int(part) * 60 if part.isdigit() else None

    if "-" in text:                                   # range A-B (e.g. 6-7h, 90-120m)
        a, b = text.split("-", 1)
        ua, ub = re.search(r"[dhms]$", a), re.search(r"[dhms]$", b)
        if ub and not ua:                             # "6-7h" → apply B's unit to A
            a += ub.group(0)
        lo, hi = to_sec(a), to_sec(b)
        return (lo, hi) if (lo and hi and hi >= lo >= 30) else (None, None)
    sec = to_sec(text)
    return (sec, None) if (sec and sec >= 30) else (None, None)


def _pick_iv(mon: dict) -> int:
    """The interval to wait for this cycle - random within [interval, interval_max] if a range."""
    lo = int(mon.get("interval") or MONITOR_INTERVAL)
    hi = int(mon.get("interval_max") or 0)
    return random.randint(lo, hi) if hi > lo else lo


try:
    _SCHED_TZ = ZoneInfo(config.SCHEDULE_TZ)   # deployment-configured (default UTC)
except Exception:
    _SCHED_TZ = ZoneInfo("UTC")
_DAY_MAP = {"א": 6, "ראשון": 6, "sun": 6, "ב": 0, "שני": 0, "mon": 0, "ג": 1, "שלישי": 1, "tue": 1,
            "ד": 2, "רביעי": 2, "wed": 2, "ה": 3, "חמישי": 3, "thu": 3, "ו": 4, "שישי": 4, "fri": 4,
            "ש": 5, "שבת": 5, "sat": 5}                # token → Python weekday (Mon=0 ... Sun=6)
# weekday -> display name per language (used by _schedule_label)
_DAY_NAMES = {"en": {6: "Sun", 0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat"},
              "he": {6: "א'", 0: "ב'", 1: "ג'", 2: "ד'", 3: "ה'", 4: "ו'", 5: "שבת"}}


def _parse_schedule(text: str):
    """'HH:MM' → daily at that time; 'א,ג 22:30' / 'sun,tue 22:30' → those weekdays.
    Returns {'at': 'HH:MM', 'days': [weekday...] or None}, or None if no time found."""
    m = re.search(r"(\d{1,2}):(\d{2})", text or "")
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    days = None
    daypart = (text[:m.start()] or "").strip().lower()
    if daypart:
        days = sorted({_DAY_MAP[t] for t in re.split(r"[,\s]+", daypart) if t in _DAY_MAP}) or None
    return {"at": f"{hh:02d}:{mm:02d}", "days": days}


def _schedule_label(sched: dict, lang: str = "en") -> str:
    days = sched.get("days")
    names = _DAY_NAMES.get(lang, _DAY_NAMES["en"])
    when = (tr("EVERY_DAY", lang) if not days
            else tr("DAYS", lang).format(days=",".join(names[d] for d in days)))
    return tr("AT", lang).format(when=when, at=sched['at'])


def _schedule_due(mon: dict, now_epoch: float) -> bool:
    """A scheduled monitor is due if (Israel time) we're on an allowed day, past its time, and
    haven't run since today's scheduled moment (tracked via _mon_last)."""
    sched = mon["schedule"]
    now = _dt.datetime.fromtimestamp(now_epoch, _SCHED_TZ)
    days = sched.get("days")
    if days is not None and now.weekday() not in days:
        return False
    hh, mm = map(int, sched["at"].split(":"))
    scheduled = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return now >= scheduled and _mon_last.get(mon["id"], 0) < scheduled.timestamp()


def _iv_label(mon: dict, lang: str = "en") -> str:
    """Human label for a monitor's schedule/interval (handles cron, random-range, fixed)."""
    if mon.get("schedule"):
        return _schedule_label(mon["schedule"], lang)
    lo = int(mon.get("interval") or MONITOR_INTERVAL)
    hi = int(mon.get("interval_max") or 0)
    return (tr("RANDOM", lang).format(lo=_fmt_eta(lo, lang), hi=_fmt_eta(hi, lang)) if hi > lo
            else next((tr(lbl, lang) for lbl, sec in _MON_INTERVALS if sec == lo), _fmt_eta(lo, lang)))


async def monitor_loop():
    """Background: each monitor is checked at its OWN interval/schedule (base tick = 1 min)."""
    while True:
        await asyncio.sleep(MONITOR_TICK)
        now = time.time()
        for mon in monitors.all_monitors():
            if mon.get("schedule"):                    # cron-like: daily/weekly at a fixed time
                if not _schedule_due(mon, now):
                    continue
                _mon_last[mon["id"]] = now
            else:                                      # interval / random range
                iv = _mon_iv.get(mon["id"]) or _pick_iv(mon)
                if now - _mon_last.get(mon["id"], 0) < iv:
                    continue
                _mon_last[mon["id"]] = now
                _mon_iv[mon["id"]] = _pick_iv(mon)     # re-roll for the next cycle (random range)
            try:
                await _check_monitor(mon)
            except Exception as e:
                await report_error(f"monitor {mon.get('id')}", e, mon.get("uid"))


def _ep_key(t: dict) -> str:
    return str(t.get("id") or f"{t.get('season', 0)}x{t.get('number', 0)}")


async def _check_monitor(mon: dict):
    uid, chat = mon["uid"], mon["chat"]
    if not users.can_monitor(uid):                 # permission revoked → silently skip
        return
    titles = await engine.list_titles(mon["service"], mon["title_id"])
    eps = [t for t in titles if t.get("type") == "episode"]
    seen = set(mon.get("seen") or [])
    fresh = sorted([t for t in eps if _ep_key(t) not in seen],
                   key=lambda x: (x.get("season") or 0, x.get("number") or 0))
    for t in fresh:
        if len(active_jobs.get(uid, {})) >= users.concurrency_limit(uid):
            break                                  # at the user's limit → rest next cycle
        wanted = (f"S{int(t.get('season') or 1):02d}E{int(t['number']):02d}"
                  if t.get("number") else None)
        await _launch_monitor_dl(mon, t, wanted)
        monitors.mark_seen(mon["id"], [_ep_key(t)])


async def _launch_monitor_dl(mon: dict, title: dict, wanted):
    uid, chat, p = mon["uid"], mon["chat"], mon.get("params", {})
    lang = users.lang(uid)
    profile = p.get("profile", str(uid))
    name = f"{mon['name']} · {title.get('name') or wanted or ''}".strip(" ·")
    flags, q = build_flags(uid, mon["service"], profile, p.get("tracks") or p.get("mode"),
                            p.get("quality", "best"), cdm=p.get("cdm"))
    m = await send(chat, tr("NEW_EPISODE_IN_STARTING", lang).format(
        name=html.escape(mon['name'])))
    mid = m["result"]["message_id"]
    media = title.get("id") if str(title.get("id", "")).startswith("http") else ""
    await launch_download(chat, uid, mid, service=mon["service"], title_id=mon["title_id"],
                           profile=profile, wanted=wanted, quality=q, flags=flags, name=name,
                           src_url=mon["title_id"], source_media=media, send_as=p.get("send_as"),
                           cover=p.get("cover"), is_monitor=True)


async def monitor_menu(chat: int, uid: int, mid: int):
    lang = users.lang(uid)
    if not users.can_monitor(uid):
        return await edit(chat, mid, tr("YOU_DON_HAVE_PERMISSION_3", lang),
                          [[(tr("MENU", lang), "m:main")]])
    mons = monitors.user_monitors(uid)
    rows = [[(f"🔔 {(m.get('name') or m['service'])[:30]}", f"mon:{m['id']}")] for m in mons]
    rows.append([(tr("ADD_MONITOR", lang), "monadd")])
    rows.append([(tr("MENU", lang), "m:main")])
    await edit(chat, mid, tr("AUTO_MONITOR_NEW_EPISODES", lang).format(count=len(mons)),
               rows)


async def monitor_detail(chat: int, uid: int, mid: int, mon_id: str):
    lang = users.lang(uid)
    m = monitors.get(mon_id)
    if not m or m["uid"] != uid:
        return await edit(chat, mid, tr("MONITOR_NOT_FOUND", lang), [[(tr("BACK", lang), "m:mon")]])
    from urllib.parse import urlparse
    host = (urlparse(m["title_id"]).hostname or m["service"]).replace("www.", "")
    iv = _iv_label(m, lang)
    pr = m.get("params", {})
    mode = sel_label(pr.get("tracks") or pr.get("mode"), lang)
    send_lbl = {"video": tr("VIDEO_2", lang), "file": tr("FILE", lang)}.get(
        pr.get("send_as"), tr("AUTOMATIC", lang))
    cover_lbl = tr("YES", lang) if pr.get("cover") else tr("NO", lang)
    lines = [f"🔔 <b>{html.escape(m['name'])}</b>",
             tr("SOURCE", lang).format(host=html.escape(host)),
             tr("SERVICE", lang).format(service=m['service']),
             tr("FREQUENCY_EVERY", lang).format(iv=iv),
             tr("DOWNLOAD_TYPE", lang).format(mode=mode),
             tr("SEND_AS", lang).format(send_lbl=send_lbl),
             tr("FIXED_THUMBNAIL", lang).format(cover_lbl=cover_lbl),
             tr("EPISODES_ALREADY_SEEN", lang).format(count=len(m.get('seen', [])))]
    rows = [[(tr("FREQUENCY", lang), f"mon_eiv:{mon_id}"), (tr("DOWNLOAD_TYPE_2", lang), f"mon_emode:{mon_id}")],
            [(tr("VIDEO_FILE", lang), f"mon_esa:{mon_id}"), (tr("THUMBNAIL_2", lang), f"mon_ecov:{mon_id}")],
            [(tr("DELETE_MONITOR", lang), f"mondel:{mon_id}")], [(tr("BACK", lang), "m:mon")]]
    await edit(chat, mid, "\n".join(lines), rows)


async def monitor_edit_interval(chat: int, uid: int, mid: int, mon_id: str):
    lang = users.lang(uid)
    rows = grid_rows([(tr(lbl, lang), f"mon_setiv:{mon_id}:{sec}") for lbl, sec in _MON_INTERVALS], 3)
    rows.append([(tr("CUSTOM_TIME_RANGE", lang), f"mon_eivc:{mon_id}")])
    rows.append([(tr("FIXED_TIME_DAYS", lang), f"mon_eschedc:{mon_id}")])
    rows.append([(tr("BACK", lang), f"mon:{mon_id}")])
    await edit(chat, mid, tr("PICK_NEW_FREQUENCY", lang), rows)


async def monitor_edit_mode(chat: int, uid: int, mid: int, mon_id: str):
    lang = users.lang(uid)
    m = monitors.get(mon_id) or {}
    sel = to_sel(m.get("params", {}).get("tracks") or m.get("params", {}).get("mode"))
    rows = _track_rows(sel, f"mon_etog:{mon_id}:", lang) + [[(tr("BACK", lang), f"mon:{mon_id}")]]
    await edit(chat, mid, tr("MARK_WHAT_TO_DOWNLOAD_2", lang), rows)


async def monitor_edit_sendas(chat: int, uid: int, mid: int, mon_id: str):
    lang = users.lang(uid)
    rows = [[(tr("AS_VIDEO_PLAYER_THUMBNAIL", lang), f"mon_setsa:{mon_id}:video")],
            [(tr("AS_FILE", lang), f"mon_setsa:{mon_id}:file")],
            [(tr("BACK", lang), f"mon:{mon_id}")]]
    await edit(chat, mid, tr("HOW_TO_SEND_EACH", lang), rows)


async def monitor_edit_cover(chat: int, uid: int, mid: int, mon_id: str):
    lang = users.lang(uid)
    has = bool((monitors.get(mon_id) or {}).get("params", {}).get("cover"))
    rows = [[(tr("SEND_NEW_IMAGE", lang), f"mon_setcov:{mon_id}")]]
    if has:
        rows.append([(tr("REMOVE_THUMBNAIL", lang), f"mon_rmcov:{mon_id}")])
    rows.append([(tr("BACK", lang), f"mon:{mon_id}")])
    await edit(chat, mid, tr("FIXED_THUMBNAIL_FOR_THE", lang), rows)


async def monitor_setup(chat: int, uid: int, url: str):
    """Seed a new monitor from a source URL: list current episodes, then ask track type."""
    lang = users.lang(uid)
    await state.services()
    service = detect_service(url)
    if not service:
        return await send(chat, tr("COULD_NOT_DETECT_SERVICE", lang))
    if not can_use(uid, service):
        return await send(chat, tr("YOU_DON_HAVE_PERMISSION_4", lang).format(
            service=html.escape(service)))
    m = await send(chat, tr("CHECKING_THE_SOURCE", lang))
    mid = m["result"]["message_id"]
    try:
        titles = await engine.list_titles(service, url, profile=str(uid))
    except UnshackleError as e:
        return await user_error(chat, mid, uid, e, back=(tr("BACK", lang), "m:mon"))
    eps = sorted([t for t in titles if t.get("type") == "episode"],
                 key=lambda x: (x.get("season") or 0, x.get("number") or 0))
    if not eps:
        return await edit(chat, mid, tr("NO_EPISODES_FOUND_MONITORING", lang),
                          [[(tr("BACK", lang), "m:mon")]])
    name = eps[0].get("series_title") or eps[0].get("title") or service
    items = [{"key": _ep_key(t),
              "label": f"S{t.get('season') or 1}E{t.get('number') or '?'} {(t.get('name') or '')[:18]}".strip()}
             for t in eps]
    sess(uid)["mon_pending"] = {"service": service, "title_id": url, "name": name, "eps": items,
                                "tracks": ["video", "audio", "subs"]}
    await monitor_ask_tracks(chat, uid, mid)


def _track_rows(sel, toggle_prefix, lang="en"):
    """Checkbox rows for the video/audio/subtitles selector."""
    return [[(("☑️ " if k in sel else "⬜ ") + tr(key, lang), f"{toggle_prefix}{k}")]
            for k, key in (("video", "VIDEO"), ("audio", "AUDIO"), ("subs", "SUBTITLES_2"))]


async def monitor_ask_tracks(chat: int, uid: int, mid: int):
    lang = users.lang(uid)
    p = sess(uid).get("mon_pending") or {}
    sel = set(p.get("tracks") or ["video", "audio", "subs"])
    rows = _track_rows(sel, "mon_ttog:", lang) + [[(tr("CONTINUE", lang), "mon_tgo")],
                                                  [(tr("CANCEL", lang), "m:mon")]]
    await edit(chat, mid, tr("EXISTING_EPISODES_MARK_WHAT", lang).format(name=html.escape(p.get('name', '')), count=len(p.get('eps') or [])), rows)


async def monitor_ask_sendas(chat: int, uid: int, mid: int):
    lang = users.lang(uid)
    p = sess(uid).get("mon_pending") or {}
    rows = [[(tr("AS_VIDEO_PLAYER_THUMBNAIL", lang), "mon_sa:video")],
            [(tr("AS_FILE", lang), "mon_sa:file")],
            [(tr("CANCEL", lang), "m:mon")]]
    await edit(chat, mid, tr("HOW_TO_SEND_EACH_2", lang).format(
        name=html.escape(p.get('name', ''))), rows)


async def monitor_ask_cover(chat: int, uid: int, mid: int):
    lang = users.lang(uid)
    p = sess(uid).get("mon_pending") or {}
    rows = [[(tr("SEND_AN_IMAGE_AS", lang), "mon_cover")],
            [(tr("SKIP_AUTOMATIC_THUMBNAIL_FROM", lang), "mon_nocover")],
            [(tr("CANCEL", lang), "m:mon")]]
    await edit(chat, mid, tr("FIXED_THUMBNAIL_FOR_ALL",
               lang).format(name=html.escape(p.get('name', ''))), rows)


_MON_INTERVALS = [("MON_IV_15MIN", 900), ("MON_IV_30MIN", 1800), ("MON_IV_1H", 3600),
                  ("MON_IV_3H", 10800), ("MON_IV_6H", 21600), ("MON_IV_12H", 43200),
                  ("MON_IV_1D", 86400)]


async def monitor_ask_interval(chat: int, uid: int, mid: int):
    lang = users.lang(uid)
    p = sess(uid).get("mon_pending") or {}
    rows = grid_rows([(tr(lbl, lang), f"mon_int:{sec}") for lbl, sec in _MON_INTERVALS], 3)
    rows.append([(tr("CUSTOM_TIME_RANGE", lang), "mon_intc")])
    rows.append([(tr("FIXED_TIME_DAYS", lang), "mon_sched")])
    rows.append([(tr("CANCEL", lang), "m:mon")])
    await edit(chat, mid, tr("HOW_OFTEN_TO_SCAN", lang).format(
        name=html.escape(p.get('name', ''))), rows)


async def monitor_ask_start(chat: int, uid: int, mid: int):
    lang = users.lang(uid)
    p = sess(uid).get("mon_pending") or {}
    n = len(p.get("eps") or [])
    rows = [[(tr("ONLY_NEW_FROM_NOW", lang), "mon_start:new")],
            [(tr("EVERYTHING_EXISTING_NEW", lang).format(n=n), "mon_start:all")],
            [(tr("FROM_SPECIFIC_EPISODE_ONWARD", lang), "mon_start:pick")],
            [(tr("CANCEL", lang), "m:mon")]]
    await edit(chat, mid, tr("FROM_WHICH_EPISODE_TO", lang).format(
        name=html.escape(p.get('name', ''))), rows)


async def monitor_pick_start(chat: int, uid: int, mid: int, page: int):
    lang = users.lang(uid)
    p = sess(uid).get("mon_pending") or {}
    eps = p.get("eps") or []
    pages = max(1, (len(eps) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = eps[page * PAGE:(page + 1) * PAGE]
    rows = [[(e["label"], f"mon_pick:{page * PAGE + i}")] for i, e in enumerate(chunk)]
    nav = []
    if page > 0:
        nav.append(("◀", f"mon_pickp:{page-1}"))
    nav.append((f"{page+1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(("▶", f"mon_pickp:{page+1}"))
    rows.append(nav)
    rows.append([(tr("CANCEL", lang), "m:mon")])
    await edit(chat, mid, tr("PICK_THE_FIRST_EPISODE", lang), rows)


async def _save_monitor(chat: int, uid: int, mid: int, seen: list):
    lang = users.lang(uid)
    p = sess(uid).get("mon_pending")
    if not p:
        return await edit(chat, mid, tr("EXPIRED_START_AGAIN", lang), [[(tr("BACK", lang), "m:mon")]])
    mon = monitors.add(uid, chat, p["service"], p["title_id"], p["name"],
                       {"tracks": p.get("tracks") or ["video", "audio", "subs"], "quality": "best",
                        "profile": str(uid), "send_as": p.get("send_as"), "cover": p.get("cover")},
                       seen=seen, interval=int(p.get("interval") or MONITOR_INTERVAL),
                       interval_max=int(p.get("interval_max") or 0), schedule=p.get("schedule"),
                       ts=int(time.time()))
    sess(uid).pop("mon_pending", None)
    await edit(chat, mid, tr("MONITOR_ADDED_FOR_EVERY", lang).format(name=html.escape(mon['name']), iv=_iv_label(mon, lang)),
               [[(tr("MONITORS", lang), "m:mon")], [(tr("MENU", lang), "m:main")]])
