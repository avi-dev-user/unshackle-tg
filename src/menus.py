"""Inline UI: the main menu, the service picker, the download wizard (titles ->
seasons -> episodes -> tracks -> quality -> send-as -> cover -> account/CDM), the
per-user accounts/CDM screens, and the admin service catalog. These render keyboards
and read/write the shared wizard session; the actual download is handed to download.py."""
import html
import re

from . import auth, state, users
from .catalog_meta import (can_use, categorise, svc_auth_methods, svc_auth_required, svc_desc,
                           svc_link, svc_needs_auth)
from .download import start_download, to_sel
from .engine import UnshackleError
from .errors import user_error
from .format import _lang_label
from .i18n import LANGS, tr
from .session import active_jobs, sess
from .state import engine
from .tg import GRID_N, PAGE, call, edit, grid_rows, send


async def _clear_poster(uid: int) -> None:
    """Remove the standalone poster photo (if one was shown) when the user leaves the title."""
    s = sess(uid)
    mid = s.pop("poster_mid", None)
    s.pop("poster_key", None)
    if mid and s.get("poster_chat"):
        try:
            await call("deleteMessage", chat_id=s["poster_chat"], message_id=mid)
        except Exception:
            pass


async def _show_poster(chat: int, uid: int) -> bool:
    """Show the title's poster (cover_url) as a standalone photo with title + synopsis caption.
    Separate from the single-edited wizard message, so the edit chain is untouched. Sent once per
    title; returns True if a poster is now displayed (so the wizard text can skip the synopsis)."""
    s = sess(uid)
    cover = s.get("cover_url")
    key = (s.get("title_id"), s.get("wanted"))
    if not cover:
        return False
    if s.get("poster_key") == key:                 # already showing this exact selection
        return True
    await _clear_poster(uid)
    cap = [f"<b>🎬 {html.escape(s.get('name') or '')}</b>"]
    desc = (s.get("description") or "").strip()
    if desc:
        cap.append(html.escape(desc[:900]))
    r = await call("sendPhoto", chat_id=chat, photo=cover, parse_mode="HTML",
                   caption="\n\n".join(cap))
    if isinstance(r, dict) and r.get("ok"):
        s["poster_mid"] = r["result"]["message_id"]
        s["poster_chat"] = chat
        s["poster_key"] = key
        return True
    return False


# --------------------------------------------------------------------------
# Menus
# --------------------------------------------------------------------------
async def main_menu(chat: int, uid: int, mid: int = None):
    sess(uid)["subs_mode"] = False                 # leave any subtitle-only flow
    await _clear_poster(uid)                        # drop the title poster when back at the menu
    lang = users.lang(uid)
    items = [(tr("NEW_DOWNLOAD", lang), "m:dl"), (tr("SEARCH", lang), "m:search"),
             (tr("SUBTITLES", lang), "m:subs"), (tr("MY_DOWNLOADS", lang), "m:dls"),
             (tr("MY_ACCOUNTS", lang), "m:acc")]
    if users.can_monitor(uid):
        items.append((tr("AUTO_MONITOR", lang), "m:mon"))
    rows = grid_rows(items, 2)                     # two actions per row (tidy grid, not one column)
    if users.is_admin(uid):
        rows.append([(tr("SERVICES", lang), "m:svc"), (tr("USERS", lang), "m:users")])
        rows.append([(tr("LIVE_RECORDING", lang), "m:rec")])
    rows.append([(tr("SETTINGS", lang), "m:settings")])
    u = users.get(uid) or {}
    first = (u.get("name") or "").strip().split()[0] if u.get("name") else ""
    text = tr("WELCOME_WHAT_WOULD_YOU", lang).format(name=(f" {html.escape(first)}" if first else ""))
    if mid:
        await edit(chat, mid, text, rows)
    else:
        await send(chat, text, rows)


async def settings_menu(chat: int, uid: int, mid: int):
    """Per-user settings: UI language + the gofile download-link preference."""
    lang = users.lang(uid)
    cur_lang = next((name for code, name in LANGS.items() if code == lang), lang)
    mode = users.gofile_mode(uid)
    mode_label = tr(f"GOFILE_MODE_{mode.upper()}", lang)
    rows = [
        [(f"🌐 {tr('LANGUAGE', lang)}: {cur_lang}", "m:lang")],
        [(f"☁️ {tr('GOFILE_SETTING', lang)}: {mode_label}", "m:gfmode")],
        [(tr("MENU", lang), "m:main")],
    ]
    await edit(chat, mid, tr("SETTINGS", lang), rows)


async def gofile_mode_menu(chat: int, uid: int, mid: int):
    """Pick how the extra gofile download link is handled: ask each time / always / never."""
    lang = users.lang(uid)
    cur = users.gofile_mode(uid)
    rows = [[(("✅ " if m == cur else "") + tr(f"GOFILE_MODE_{m.upper()}", lang), f"gfmode:{m}")]
            for m in users.GOFILE_MODES]
    rows.append([(tr("BACK", lang), "m:settings")])
    await edit(chat, mid, f"☁️ {tr('GOFILE_SETTING_EXPLAIN', lang)}", rows)


async def language_menu(chat: int, uid: int, mid: int):
    """Per-user UI language switch (English default)."""
    lang = users.lang(uid)
    rows = [[(("✅ " if code == lang else "") + name, f"lang:{code}")] for code, name in LANGS.items()]
    rows.append([(tr("BACK", lang), "m:settings")])
    await edit(chat, mid, tr("CHOOSE_YOUR_LANGUAGE", lang), rows)


async def picker(chat: int, uid: int, mid: int, cat: str, page: int, search: bool = False):
    await state.services()
    lang = users.lang(uid)

    def usable(t: str) -> bool:
        # in search mode only show services that actually support search()
        return can_use(uid, t) and (not search or state.meta(t).get("has_search"))

    if cat == "recent":
        tags = {s["tag"] for s in state.services_cached()}
        svcs = [t for t in users.recent(uid) if t in tags and usable(t)]  # keep recency order
        if not svcs:                              # new user / nothing yet → default to Israeli
            return await picker(chat, uid, mid, "il", 0, search)
    else:
        svcs = sorted([s["tag"] for s in state.services_cached()
                       if categorise(s["tag"]) == cat and usable(s["tag"])])
    pick_cb = "srch" if search else "svc"        # search → query prompt; else → download flow
    tab_cb = "qcat" if search else "cat"
    pages = max(1, (len(svcs) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = svcs[page * PAGE:(page + 1) * PAGE]
    rows = grid_rows([(t, f"{pick_cb}:{t}") for t in chunk], 4)
    nav = []
    if page > 0:
        nav.append(("◀", f"{tab_cb}:{cat}:{page-1}"))
    nav.append((f"{page+1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(("▶", f"{tab_cb}:{cat}:{page+1}"))
    rows.append(nav)
    rows.append([(tr("RECENT", lang), f"{tab_cb}:recent:0"), (tr("REGIONAL", lang), f"{tab_cb}:il:0")])
    rows.append([(tr("FREE", lang), f"{tab_cb}:free:0"), (tr("SUBSCRIPTION", lang), f"{tab_cb}:sub:0")])
    rows.append([(tr("MENU", lang), "m:main")])
    title = {"recent": tr("RECENT", lang), "il": tr("REGIONAL", lang),
             "free": tr("FREE", lang), "sub": tr("SUBSCRIPTION", lang)}[cat]
    subs = search and sess(uid).get("subs_mode")
    head_label = ((tr("SUBTITLES_PICK_SERVICE", lang) if subs
                   else tr("SEARCH_PICK_SERVICE", lang)) if search else tr("PICK_SERVICE", lang))
    if search and not chunk:
        lines = [f"<b>{head_label}</b> - {title}\n\n" + tr("NO_SEARCHABLE_SERVICES_HERE", lang)]
        return await edit(chat, mid, "\n".join(lines), rows)
    # legend: each service on this page with its description + link + auth badge
    lines = [f"<b>{head_label}</b> - {title}\n"]
    for t in chunk:
        desc = html.escape(svc_desc(t))
        link = svc_link(t)
        badge = "🔐" if svc_needs_auth(t) else "🔓"
        head = f'<a href="{link}">{t}</a>' if link else f"<b>{t}</b>"
        lines.append(f"{badge} {head} - {desc}" if desc else f"{badge} {head}")
    lines.append("\n" + tr("NEEDS_AN_ACCOUNT_FREE", lang))
    await edit(chat, mid, "\n".join(lines), rows)


# --------------------------------------------------------------------------
# Download wizard
# --------------------------------------------------------------------------
async def ask_input(chat: int, uid: int, mid: int, service: str):
    s = sess(uid)
    lang = users.lang(uid)
    if not can_use(uid, service):
        return await edit(chat, mid, tr("YOU_DON_HAVE_PERMISSION", lang),
                          [[(tr("MENU", lang), "m:main")]])
    s.update(step="await_input", service=service)
    await edit(chat, mid, tr("SERVICE_NOW_SEND_LINK", lang).format(service=service),
               [[(tr("CANCEL", lang), "m:main")]])


SEARCH_PAGE = 8                                        # results per page


def _search_labels(results: list) -> list:
    """Distinct service-provided result labels (e.g. series / movie), first-seen order."""
    seen = []
    for r in results:
        lab = r.get("label")
        if lab and lab not in seen:
            seen.append(lab)
    return seen


async def show_search_results(chat: int, uid: int, mid: int):
    """Render stored search results: type-filter chips + a paginated list of hits.
    Reads session keys set by the search handler (search_results / search_query /
    search_filter / search_page) so paging and filtering just re-call this."""
    s = sess(uid)
    lang = users.lang(uid)
    results = s.get("search_results") or []
    query = s.get("search_query", "")
    flt = s.get("search_filter")                       # None = all, else a label string
    labels = _search_labels(results)
    if flt is not None and flt not in labels:          # stale filter (results changed underneath)
        flt = s["search_filter"] = None
    view = [(i, r) for i, r in enumerate(results) if flt is None or r.get("label") == flt]
    pages = max(1, (len(view) + SEARCH_PAGE - 1) // SEARCH_PAGE)
    page = max(0, min(s.get("search_page", 0), pages - 1))
    s["search_page"] = page

    rows = []
    if len(labels) >= 2:                               # only worth a filter when >1 kind is present
        chips = [((("• " if flt is None else "") + tr("FILTER_ALL", lang)), "sf:-1")]
        for li, lab in enumerate(labels):
            chips.append(((("• " if flt == lab else "") + lab)[:24], f"sf:{li}"))
        rows.extend(grid_rows(chips, 3))
    for i, r in view[page * SEARCH_PAGE:(page + 1) * SEARCH_PAGE]:
        label = (f"{r['label']}: " if r.get("label") else "") + (r.get("title") or str(r.get("id")))
        yr = f" ({r['description']})" if r.get("description") else ""
        rows.append([((label + yr)[:62], f"sr:{i}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(("◀", f"sp:{page - 1}"))
        nav.append((f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(("▶", f"sp:{page + 1}"))
        rows.append(nav)
    rows.append([(tr("MENU", lang), "m:main")])
    return await edit(chat, mid, tr("RESULTS_FOR", lang).format(q=html.escape(query), n=len(view)), rows)


async def show_titles(chat: int, uid: int, title_id: str):
    s = sess(uid)
    lang = users.lang(uid)
    service = s["service"]
    if not can_use(uid, service):
        return await send(chat, tr("YOU_DON_HAVE_PERMISSION_2", lang).format(service=html.escape(service)),
                          [[(tr("MENU", lang), "m:main")]])
    await _clear_poster(uid)                        # a fresh title: drop any previous poster
    m = await send(chat, tr("LOADING", lang))
    mid = m["result"]["message_id"]
    # subscription service with no connected account: send the user to connect it rather
    # than letting list_titles fail at authenticate (and risk a blocked login). Optional-auth
    # services (catch-all/free) are NOT gated - we try anonymously, cookies are a fallback.
    if svc_auth_required(service) and not auth.list_accounts(uid, service):
        return await account_service(chat, uid, mid, service)
    try:
        titles = await engine.list_titles(service, title_id, profile=str(uid),
                                          credential=auth.first_credential(uid, service))
    except UnshackleError as e:
        return await user_error(chat, mid, uid, e)
    if not titles:
        return await edit(chat, mid, tr("NO_CONTENT_FOUND_AT", lang), [[(tr("MENU", lang), "m:main")]])
    s.update(title_id=title_id, titles=titles, mid=mid)

    episodes = [t for t in titles if t.get("type") == "episode"]
    if not episodes:                       # movie / single
        s["wanted"] = None
        return await show_tracks(chat, uid, mid, wanted=None)
    seasons = sorted({t["season"] for t in episodes})
    if len(seasons) == 1:
        return await show_episodes(chat, uid, mid, seasons[0], 0)
    series = episodes[0].get("series_title") or service
    rows = grid_rows([(tr("SEASON", lang).format(n=n), f"se:{n}:0") for n in seasons], 4)
    rows.append([(tr("WHOLE_SERIES", lang), "epallseries")])     # download every episode in all seasons
    rows.append([(tr("MENU", lang), "m:main")])
    head = (f"📺 <b>{html.escape(series)}</b>\n"
            + tr("SEASONS_EPISODES_TOTAL_PICK", lang).format(seasons=len(seasons), episodes=len(episodes)))
    await edit(chat, mid, head, rows)


async def show_episodes(chat: int, uid: int, mid: int, season: int, page: int):
    s = sess(uid)
    lang = users.lang(uid)
    all_eps = [t for t in s["titles"] if t.get("type") == "episode"]
    total_seasons = len({t["season"] for t in all_eps})
    eps = [t for t in all_eps if t["season"] == season]
    eps.sort(key=lambda t: t["number"], reverse=True)   # newest → oldest
    series = (eps[0].get("series_title") if eps else "") or s.get("service", "")
    pages = max(1, (len(eps) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = eps[page * PAGE:(page + 1) * PAGE]
    # compact episode-number buttons in a grid; the names go in the text legend above
    rows = grid_rows([(f"E{t['number']:02d}", f"ep:{season}:{t['number']}") for t in chunk], 4)
    nav = []
    if page > 0:
        nav.append(("◀", f"se:{season}:{page-1}"))
    nav.append((f"{page+1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(("▶", f"se:{season}:{page+1}"))
    rows.append(nav)
    rows.append([(tr("WHOLE_SEASON", lang), f"epall:{season}")])
    rows.append([(tr("MENU", lang), "m:main")])
    season_lbl = (tr("SEASON_OF", lang).format(season=season, total=total_seasons)
                  if total_seasons > 1 else tr("SEASON_2", lang).format(season=season))
    lines = [f"📺 <b>{html.escape(series)}</b>",
             tr("EPISODES", lang).format(season_lbl=season_lbl, count=len(eps)), ""]
    for t in chunk:
        nm = (t.get("name") or "").strip()
        lines.append(f"<b>E{t['number']:02d}</b>" + (f" · {html.escape(nm[:48])}" if nm else ""))
    lines.append("\n" + tr("PICK_AN_EPISODE", lang))
    await edit(chat, mid, "\n".join(lines), rows)


def _wiz_head(s: dict, lang: str) -> str:
    """Title block shared by the post-selection wizard screens: the title plus the
    episode context ('Series · Season X · Episode Y') so every step says what you're on."""
    name = html.escape(s.get("name") or s.get("service") or "")
    ctx = html.escape(s.get("ctx") or "")
    return f"<b>{name}</b>\n{ctx}" if ctx else f"<b>{name}</b>"


async def show_tracks(chat: int, uid: int, mid: int, wanted):
    s = sess(uid)
    lang = users.lang(uid)
    s["wanted"] = wanted
    s["cover"] = None                              # fresh per download (custom thumbnail set later)
    # remember the selected title's direct media URL (for POD it's the mp3) so we can
    # enrich music metadata/cover from the source. Only for a single selected episode.
    s["source_media"] = ""
    titles = s.get("titles", [])
    m = re.match(r"S(\d+)E(\d+)", str(wanted)) if wanted else None
    if m:
        se, ep = int(m.group(1)), int(m.group(2))
        cur = next((t for t in titles if t.get("season") == se and t.get("number") == ep), None)
    else:
        cur = titles[0] if len(titles) == 1 else None
    if cur and str(cur.get("id", "")).startswith("http"):
        s["source_media"] = cur["id"]
    await edit(chat, mid, tr("CHECKING_TRACKS", lang))
    try:
        tracks = await engine.list_tracks(s["service"], s["title_id"], wanted=wanted, profile=str(uid),
                                          credential=auth.first_credential(uid, s["service"]))
    except UnshackleError as e:
        return await user_error(chat, mid, uid, e)
    videos = tracks.get("video", []) if isinstance(tracks, dict) else []
    s["heights"] = sorted({v["height"] for v in videos if v.get("height")}, reverse=True)
    # per resolution: the richest variant (highest bitrate) - codec / dynamic-range / bitrate for the
    # quality picker, so the choice isn't blind. All real values straight from the engine.
    vinfo = {}
    for v in sorted(videos, key=lambda v: v.get("bitrate") or 0):
        if v.get("height"):
            vinfo[v["height"]] = {"codec": v.get("codec_display") or v.get("codec") or "",
                                  "range": v.get("range_display") or v.get("range") or "",
                                  "bitrate": v.get("bitrate")}
    s["vinfo"] = vinfo
    audios = tracks.get("audio", []) if isinstance(tracks, dict) else []
    s["nv"] = len(videos)
    s["na"] = len(audios)
    s["audio_langs"] = sorted({a.get("language") for a in audios if a.get("language")})
    subs = tracks.get("subtitles", []) if isinstance(tracks, dict) else []
    s["ns"] = len(subs)
    s["sub_langs"] = sorted({x.get("language") for x in subs if x.get("language")})
    _t = tracks.get("title", {}) if isinstance(tracks, dict) else {}
    s["name"] = _t.get("name", "")
    s["description"] = _t.get("description") or ""   # synopsis + air date for the rich caption
    s["upload_date"] = _t.get("date") or ""
    s["cover_url"] = _t.get("cover_url") or ""        # service poster -> used as thumbnail if no custom cover
    # rich context line shown on every downstream wizard screen
    se_m = re.match(r"S(\d+)(?:E(\d+))?$", str(wanted)) if wanted else None
    series = (titles[0].get("series_title") if titles else "") or ""
    if se_m and se_m.group(2):
        span = tr("SEASON_EPISODE", lang).format(season=int(se_m.group(1)), episode=int(se_m.group(2)))
    elif se_m:
        span = tr("SEASON_ALL_EPISODES", lang).format(season=int(se_m.group(1)))
    elif wanted is None and any(t.get("type") == "episode" for t in titles):
        span = tr("WHOLE_SERIES_2", lang)
    else:
        span = ""
    s["ctx"] = " · ".join(p for p in (series, span) if p)
    if not (s["nv"] or s["na"] or s["ns"]):
        return await edit(chat, mid, tr("NO_TRACKS_FOUND", lang), [[(tr("MENU", lang), "m:main")]])
    if s.get("subs_mode"):                    # 📝 subtitle tab → pull only the clear subtitles
        if not s["ns"]:
            return await edit(chat, mid, tr("NO_SUBTITLES_FOUND_FOR", lang), [[(tr("MENU", lang), "m:main")]])
        s["tsel"] = ["subs"]
        s["s_lang"] = None
        s["sub_extra_lang"] = None
        if len(s["sub_langs"]) > 1:            # let the user pick which language
            return await show_sub_langs(chat, uid, mid)
        return await pick_account_or_go(chat, uid, mid, "best")
    s.pop("tsel", None)                       # fresh selection (default all) per new title
    await show_track_types(chat, uid, mid)


async def show_track_types(chat: int, uid: int, mid: int):
    """Pick what to download via checkboxes - any combo of video/audio/subtitles (default all)."""
    s = sess(uid)
    lang = users.lang(uid)
    if "tsel" not in s:                       # default: everything available, all checked
        s["tsel"] = [k for k, a in (("video", s["nv"]), ("audio", s["na"]), ("subs", s["ns"])) if a]
    sel = set(s["tsel"])
    rows = []
    for k, label, avail in (("video", tr("VIDEO", lang), s["nv"]),
                            ("audio", tr("AUDIO", lang), s["na"]),
                            ("subs", tr("SUBTITLES_2", lang), s["ns"])):
        if avail:
            rows.append([(("☑️ " if k in sel else "⬜ ") + label, f"tt_tog:{k}")])
    rows.append([(tr("CONTINUE", lang), "tt_go")])
    rows.append([(tr("MENU", lang), "m:main")])

    def _count(emoji: str, n: int, langs: list) -> str:   # "🎧 2 (Hebrew, English)" when languages known
        label = f"{emoji} {n}"
        if langs:
            label += " (" + ", ".join(_lang_label(la, lang) for la in langs) + ")"
        return label
    info = " · ".join((f"🎬 {s['nv']}",
                       _count("🎧", s["na"], s.get("audio_langs") or []),
                       _count("💬", s["ns"], s.get("sub_langs") or [])))
    # Poster on top: the first time we reach this screen for a title that has a cover, drop the
    # text wizard message, post the poster (with synopsis caption), and re-send the controls as a
    # fresh message below it. On back-navigation (same poster_key) we just edit in place - no flicker.
    key = (s.get("title_id"), s.get("wanted"))
    reposition = bool(s.get("cover_url")) and s.get("poster_key") != key
    syn = ""                                     # synopsis inline only when no poster carries it
    if not s.get("cover_url") and (s.get("description") or "").strip():
        syn = "\n" + html.escape(s["description"].strip()[:300])
    text = f"{_wiz_head(s, lang)}\n{info}{syn}\n\n" + tr("MARK_WHAT_TO_DOWNLOAD", lang)
    if reposition:
        try:
            await call("deleteMessage", chat_id=chat, message_id=mid)
        except Exception:
            pass
        await _show_poster(chat, uid)            # photo above; sets poster_key so we don't repeat
        m = await send(chat, text, rows)
        s["mid"] = m["result"]["message_id"]
    else:
        await edit(chat, mid, text, rows)


async def show_quality(chat: int, uid: int, mid: int):
    s = sess(uid)
    lang = users.lang(uid)
    heights = s.get("heights") or []
    if not heights:                       # no resolutions → skip quality
        s["quality"] = "best"
        return await show_send_as(chat, uid, mid)
    vinfo = s.get("vinfo") or {}

    def qlabel(h: int) -> str:            # "1080p · HDR10 · H.265 · 8.5M" - real values, no faked size
        vi = vinfo.get(h) or {}
        parts = [f"{h}p"]
        rng = vi.get("range") or ""
        if rng and rng.upper() != "SDR":
            parts.append(rng)
        if vi.get("codec"):
            parts.append(vi["codec"])
        if vi.get("bitrate"):
            parts.append(f"{vi['bitrate'] / 1000:.1f}M")
        return " · ".join(parts)

    if vinfo:                             # one per row - the enriched labels need the width
        rows = [[(qlabel(h), f"q:{h}")] for h in heights]
    else:
        rows = grid_rows([(f"{h}p", f"q:{h}") for h in heights], 3)
    rows.append([(tr("BEST", lang), "q:best")])
    rows.append([(tr("BACK", lang), "tt:back")])
    await edit(chat, mid, f"{_wiz_head(s, lang)}\n" + tr("PICK_QUALITY", lang), rows)


async def show_send_as(chat: int, uid: int, mid: int):
    """Choose how the video is delivered to Telegram: as a streamable video, or as a file."""
    lang = users.lang(uid)
    rows = [[(tr("AS_VIDEO_PLAYER_THUMBNAIL", lang), "sa:video")],
            [(tr("AS_FILE", lang), "sa:file")],
            [(tr("BACK", lang), "tt:back")]]
    await edit(chat, mid, f"{_wiz_head(sess(uid), lang)}\n" + tr("HOW_TO_SEND", lang), rows)


async def show_dl_cover(chat: int, uid: int, mid: int):
    """Optional custom thumbnail for this download (else one is taken from the video)."""
    lang = users.lang(uid)
    rows = [[(tr("AUTOMATIC_THUMBNAIL", lang), "dlcov:auto")],
            [(tr("UPLOAD_THUMBNAIL", lang), "dlcov:up")],
            [(tr("BACK", lang), "tt:back")]]
    await edit(chat, mid, f"{_wiz_head(sess(uid), lang)}\n" + tr("THUMBNAIL", lang), rows)


async def show_sub_langs(chat: int, uid: int, mid: int):
    """Pick a subtitle language when several exist."""
    s = sess(uid)
    lang = users.lang(uid)
    langs = s.get("sub_langs") or []
    rows = grid_rows([(_lang_label(la, lang), f"sl:{la}") for la in langs], 4)
    rows.append([(tr("OTHER_LANGUAGE", lang), "slother"), (tr("ALL_LANGUAGES", lang), "sl:all")])
    rows.append([(tr("BACK", lang), "tt:back")])
    await edit(chat, mid, f"{_wiz_head(s, lang)}\n" + tr("PICK_SUBTITLE_LANGUAGE", lang), rows)


async def pick_account_or_go(chat: int, uid: int, mid: int, quality):
    """If the user has >1 account for the service, ask which; else proceed."""
    s = sess(uid)
    lang = users.lang(uid)
    s["quality"] = quality
    accounts = auth.list_accounts(uid, s["service"])
    if len(accounts) > 1:
        rows = [[(a["label"], f"use:{a['profile']}")] for a in accounts]
        rows.append([(tr("MENU", lang), "m:main")])
        return await edit(chat, mid, tr("WHICH_ACCOUNT_SHOULD_USE", lang), rows)
    profile = accounts[0]["profile"] if accounts else str(uid)  # default profile = own id
    await _after_account(chat, uid, mid, profile)


async def _after_account(chat: int, uid: int, mid: int, profile: str):
    """For DRM services, resolve which CDM to use (own wvd / shared default / blocked)
    before downloading. Non-DRM services skip straight to the download."""
    s = sess(uid)
    lang = users.lang(uid)
    s["cdm"] = None
    if to_sel(s.get("tsel")) == {"subs"}:                          # clear subtitles → never need a CDM
        return await start_download(chat, uid, mid, profile)
    if not state.meta(s["service"]).get("has_drm"):
        return await start_download(chat, uid, mid, profile)        # no CDM needed
    wvds = auth.list_wvd(uid)
    if len(wvds) == 1:
        s["cdm"] = wvds[0]["device"]
        return await start_download(chat, uid, mid, profile)
    if len(wvds) > 1:                                               # let the user pick a CDM
        s["dl_profile"] = profile
        rows = [[(f"🔑 {w['label']}", f"cdm:{w['profile']}")] for w in wvds]
        if users.can_use_default_cdm(uid):
            rows.append([(tr("DEFAULT_SHARED", lang), "cdm:_default")])
        rows.append([(tr("MENU", lang), "m:main")])
        return await edit(chat, mid, tr("WHICH_CDM_FILE_SHOULD", lang), rows)
    # no personal wvd
    if users.can_use_default_cdm(uid):
        s["cdm"] = ""                                              # engine uses the shared default
        return await start_download(chat, uid, mid, profile)
    return await edit(chat, mid, tr("THIS_SERVICE_REQUIRES_CDM", lang),
                      [[(tr("MENU", lang), "m:main")]])


# --------------------------------------------------------------------------
# Accounts (per-user, multi-account cookies)
# --------------------------------------------------------------------------
async def accounts_menu(chat: int, uid: int, mid: int, page: int = 0):
    await state.services()                       # ensure meta loaded
    lang = users.lang(uid)
    mine = auth.user_services(uid)
    # services that require auth, OR accept optional cookies (e.g. FreeTV: cookies unlock 1080p),
    # plus any the user already set up
    svcs = sorted({s["tag"] for s in state.services_cached()
                   if svc_needs_auth(s["tag"]) or svc_auth_methods(s["tag"])} | set(mine))
    pages = max(1, (len(svcs) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = svcs[page * PAGE:(page + 1) * PAGE]
    pairs = []
    for svc in chunk:
        n = len(mine.get(svc, []))
        pairs.append((f"{svc}" + (f" ✅{n}" if n else ""), f"as:{svc}"))
    rows = grid_rows(pairs, 4)
    nav = []
    if page > 0:
        nav.append(("◀", f"ac:{page-1}"))
    nav.append((f"{page+1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(("▶", f"ac:{page+1}"))
    rows.append(nav)
    nwvd = len(auth.list_wvd(uid))
    rows.append([(tr("CDM_FILES_DRM", lang) + (f" ✅{nwvd}" if nwvd else ""), "m:cdm")])
    rows.append([(tr("MENU", lang), "m:main")])
    await edit(chat, mid, tr("MY_ACCOUNTS_SERVICES_THAT", lang), rows)


async def cdm_menu(chat: int, uid: int, mid: int):
    """Per-user CDM (.wvd) devices, used for DRM services."""
    lang = users.lang(uid)
    wvds = auth.list_wvd(uid)
    rows = [[(f"🔑 {w['label']}", "noop"), ("🗑️", f"cdmdel:{w['profile']}")] for w in wvds]
    rows.append([(tr("UPLOAD_CDM_FILE_WVD", lang), "cdmadd")])
    rows.append([(tr("BACK", lang), "m:acc")])
    note = (tr("YOU_HAVE_SHARED_DEFAULT", lang) if users.can_use_default_cdm(uid)
            else tr("YOU_DON_HAVE_ACCESS", lang))
    await edit(chat, mid, tr("CDM_FILES_WIDEVINE_PLAYREADY", lang).format(count=len(wvds), note=note), rows)


async def my_downloads(chat: int, uid: int, mid: int):
    """The user's active downloads (concurrency slots), with cancel buttons."""
    lang = users.lang(uid)
    # skip internal reservation placeholders (keyed with a \x00 prefix) - not real jobs
    jobs = {k: v for k, v in active_jobs.get(uid, {}).items() if not k.startswith("\x00")}
    lines = [tr("MY_DOWNLOADS_2", lang).format(n=len(jobs), limit=users.concurrency_limit(uid))]
    rows = []
    if not jobs:
        lines.append("\n" + tr("NO_ACTIVE_DOWNLOADS_RIGHT", lang))
    for jid, info in jobs.items():
        try:
            j = await engine.job(jid)
            st, pr = j.get("status", "?"), int(j.get("progress") or 0)
        except Exception:
            st, pr = "?", 0
        name = (info.get("name") or jid)[:28]
        lines.append(f"• {html.escape(name)} - {st} {pr}%")
        rows.append([(tr("CANCEL_2", lang).format(name=name[:18]), f"cancel:{jid}")])
    rows.append([(tr("REFRESH", lang), "m:dls"), (tr("MENU", lang), "m:main")])
    await edit(chat, mid, "\n".join(lines), rows)


async def account_service(chat: int, uid: int, mid: int, svc: str):
    lang = users.lang(uid)
    accounts = auth.list_accounts(uid, svc)
    icon = {"creds": "🔑", "cookies": "👤"}
    rows = [[(f"{icon.get(a.get('kind'), '👤')} {a['label']}", "noop"),
             ("🗑️", f"adel:{svc}:{a['profile']}")] for a in accounts]
    methods = svc_auth_methods(svc)            # auto: what THIS service accepts
    if "cookies" in methods or not methods:
        rows.append([(tr("COOKIES_FILE", lang), f"aadd:{svc}")])
    if "credentials" in methods:
        rows.append([(tr("USERNAME_PASSWORD", lang), f"aaddc:{svc}")])
    rows.append([(tr("BACK", lang), "m:acc")])
    how = " · ".join(m for m in ((tr("COOKIES", lang) if "cookies" in methods else ""),
                                 (tr("USERNAME_PASSWORD_2", lang) if "credentials" in methods else "")) if m) or "-"
    txt = tr("ACCOUNTS_SUPPORTED_SIGN_IN", lang).format(svc=svc, count=len(accounts), how=how)
    await edit(chat, mid, txt, rows)


# --------------------------------------------------------------------------
# Services catalog (admin): inline grid + per-service detail
# --------------------------------------------------------------------------
async def services_grid(chat: int, uid: int, mid: int, cat: str, page: int):
    await state.services()
    lang = users.lang(uid)
    tags = sorted(t for t in (s["tag"] for s in state.services_cached()) if categorise(t) == cat)
    pages = max(1, (len(tags) + GRID_N - 1) // GRID_N)
    page = max(0, min(page, pages - 1))
    chunk = tags[page * GRID_N:(page + 1) * GRID_N]
    rows = grid_rows([(t, f"si:{t}") for t in chunk], 4)  # 4/row
    nav = []
    if page > 0:
        nav.append(("◀", f"svcg:{cat}:{page-1}"))
    nav.append((f"{page+1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(("▶", f"svcg:{cat}:{page+1}"))
    rows.append(nav)
    rows.append([("🌍", "svcg:il:0"), ("🆓", "svcg:free:0"), ("💳", "svcg:sub:0"), ("🔄", "svc:refresh")])
    rows.append([(tr("MENU", lang), "m:main")])
    title = {"il": tr("REGIONAL", lang), "free": tr("FREE", lang), "sub": tr("SUBSCRIPTION", lang)}[cat]
    await edit(chat, mid, tr("SERVICES_TAP_SERVICE_FOR", lang).format(title=title, count=len(tags)), rows)


async def service_detail(chat: int, uid: int, mid: int, tag: str):
    await state.services()
    lang = users.lang(uid)
    m = state.meta(tag)
    cat = {"il": tr("REGIONAL", lang), "free": tr("FREE", lang), "sub": tr("SUBSCRIPTION", lang)}[categorise(tag)]
    svc_auth = tr("REQUIRES_AN_ACCOUNT", lang) if svc_needs_auth(tag) else tr("FREE_2", lang)
    lines = [f"<b>{tag}</b>"]
    if svc_desc(tag):
        lines.append(html.escape(svc_desc(tag)))
    lines.append("\n" + tr("CATEGORY_ACCESS", lang).format(cat=cat, auth=svc_auth))
    if m.get("has_search"):
        lines.append(tr("SUPPORTS_SEARCH", lang))
    if m.get("has_drm"):
        lines.append(tr("DRM_WIDEVINE_PLAYREADY_NEEDS", lang))
    if m.get("aliases"):
        lines.append(tr("ALIASES", lang).format(aliases=', '.join(m['aliases'])))
    if m.get("geofence"):
        lines.append(tr("REGION", lang).format(region=', '.join(m['geofence'])))
    if svc_link(tag):
        lines.append(f'🔗 <a href="{svc_link(tag)}">{svc_link(tag)}</a>')
    cur = categorise(tag)
    cat_names = {"il": tr("REGIONAL", lang), "free": tr("FREE", lang), "sub": tr("SUBSCRIPTION", lang)}
    lines.append("\n" + tr("CURRENT_TAB_YOU_CAN", lang).format(cat=cat_names[cur]))
    move = [(emoji, f"scat:{tag}:{k}") for k, emoji in (("il", "🌍"), ("free", "🆓"), ("sub", "💳")) if k != cur]
    rows = [[(tr("DOWNLOAD_FROM_HERE", lang), f"svc:{tag}")]]
    if "cookies" in svc_auth_methods(tag):          # admin: shared cookies used when a user has none
        has = auth.has_default_cookies(tag)
        rows.append([((tr("DEFAULT_COOKIES_SET", lang) if has else tr("SET_DEFAULT_COOKIES", lang)),
                      f"dcook:{tag}")])
    rows.append(move)
    rows.append([(tr("BACK", lang), f"svcg:{cur}:0")])
    await edit(chat, mid, "\n".join(lines), rows)
