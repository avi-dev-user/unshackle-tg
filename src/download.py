"""The download subsystem: turn a track selection into engine flags, submit the job,
poll it to completion, and hand the files to the uploader. Shared by both the
interactive wizard (start_download) and the auto-monitor (build_flags + launch_download).

This is a lower layer than the menus/monitor UI: it depends on the engine, uploader and
state, but never calls back into them. Progress and errors go out through tg/errors."""
import asyncio
import html
import os
import re
import secrets
import shutil
import time
from urllib.parse import quote

import aiohttp

from . import auth, config, gofile, keys_extract, metadata, state, uploader, users
from .catalog_meta import can_use
from .engine import UnshackleError
from .errors import report_error, user_error
from .format import _fmt_eta, _fmt_size, _lang_label, _phase, _render_progress
from .i18n import tr
from .session import active_jobs, sess
from .state import engine
from .tg import FILE_API, call, edit, send, send_document

# uid -> the kwargs of the user's last interactive launch_download, so a transient failure can
# offer a one-tap "try again" without re-walking the whole wizard. In-memory only (a bot restart
# clears it, which is fine - the user just re-navigates). Monitors are never stored here.
retry_spec: dict[int, dict] = {}

# job_id -> (Event, answer) for the "upload to gofile?" prompt in 'ask' mode. The callback
# handler resolves it; the poll loop waits on it. In-memory only (a restart just defaults to no).
_gfask: dict[str, asyncio.Event] = {}
_gfask_ans: dict[str, bool] = {}


def answer_gofile_ask(job_id: str, yes: bool) -> None:
    """Called from the callback handler when the user taps yes/no on the gofile prompt."""
    _gfask_ans[job_id] = yes
    ev = _gfask.get(job_id)
    if ev is not None:
        ev.set()


# Self-hosted download-link delivery (an expiring link served by nginx, instead of a Telegram
# upload). Reuses the same dir + URL base as live recordings, so the existing cleanup CronJob
# expires these too. Same /data filesystem as the job out dir -> publishing is an instant move.
REC_DIR = os.environ.get("REC_DIR", "/data/recordings")
REC_URL_BASE = os.environ.get("REC_URL_BASE", "").rstrip("/")

# job_id -> Event/answer for the "Telegram or link?" prompt in delivery 'ask' mode (see _gfask).
_lnk: dict[str, asyncio.Event] = {}
_lnk_ans: dict[str, bool] = {}


def answer_link_ask(job_id: str, link: bool) -> None:
    """Called from the callback handler when the user picks Telegram vs link for this job."""
    _lnk_ans[job_id] = link
    ev = _lnk.get(job_id)
    if ev is not None:
        ev.set()


async def _decide_link(chat: int, uid: int, mid: int, job_id: str, head_name: str,
                       lang: str, is_monitor: bool) -> bool:
    """Whether to deliver this job as a self-hosted download link instead of uploading to
    Telegram. Honours the user's delivery_mode (telegram/link/ask). 'ask' prompts per download;
    automated monitor runs can't be prompted, so they fall back to Telegram."""
    mode = users.delivery_mode(uid)
    if mode == "link":
        return True
    if mode == "telegram" or is_monitor:
        return False
    ev = asyncio.Event()
    _lnk[job_id] = ev
    _lnk_ans.pop(job_id, None)
    rows = [[(tr("DELIVER_TELEGRAM", lang), f"lnk:{job_id}:t"),
             (tr("DELIVER_LINK", lang), f"lnk:{job_id}:l")]]
    await edit(chat, mid, f"🎬 {head_name}\n📦 " + tr("ASK_DELIVERY", lang), rows)
    try:
        await asyncio.wait_for(ev.wait(), timeout=180)
    except asyncio.TimeoutError:
        pass
    _lnk.pop(job_id, None)
    return bool(_lnk_ans.pop(job_id, None))     # no answer in time -> Telegram (the default)


def _write_download_index(dest_dir: str, title: str, items: list[dict], lang: str) -> None:
    file_word = "קבצים" if lang == "he" else "Files"
    expires = tr("REC_LINK_EXPIRES", lang)
    rows = []
    for item in items:
        name = html.escape(str(item.get("name") or "download"))
        url = quote(str(item.get("name") or "download"))
        kind = html.escape(_download_link_kind(str(item.get("name") or ""), lang))
        size = html.escape(_fmt_size(item.get("size") or 0))
        details = str(item.get("details_html") or "")
        rows.append(
            "<article>"
            f"<div class=\"meta\">{kind} · {size}</div>"
            f"<a class=\"file\" href=\"{url}\" download>{name}</a>"
            f"{details}"
            "</article>"
        )
    direction = "rtl" if lang == "he" else "ltr"
    page = f"""<!doctype html>
<html lang="{html.escape(lang)}" dir="{direction}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title or file_word)}</title>
<style>
:root {{ color-scheme: dark light; font-family: system-ui, -apple-system, Segoe UI, sans-serif; }}
body {{ margin: 0; background: #111827; color: #f9fafb; }}
main {{ max-width: 900px; margin: 0 auto; padding: 28px 18px 42px; }}
h1 {{ font-size: 24px; margin: 0 0 8px; }}
.hint {{ color: #cbd5e1; margin: 0 0 22px; }}
article {{ border: 1px solid #334155; border-radius: 8px; padding: 14px; margin: 12px 0; background: #1f2937; }}
.meta {{ color: #cbd5e1; font-size: 14px; margin-bottom: 8px; }}
.file {{ color: #93c5fd; font-weight: 700; overflow-wrap: anywhere; }}
blockquote {{ border-inline-start: 3px solid #64748b; margin: 12px 0 0; padding-inline-start: 10px; color: #e5e7eb; }}
code {{ color: #fef3c7; }}
</style>
</head>
<body><main>
<h1>{html.escape(title or file_word)}</h1>
<p class="hint">{html.escape(expires)}</p>
{''.join(rows)}
</main></body></html>
"""
    with open(os.path.join(dest_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


def publish_link(files: list[str], title: str = "", items: list[dict] | None = None,
                 lang: str = "en") -> dict:
    """Move the job's files into one fresh token dir under REC_DIR (nginx-served) and return
    their public URLs. The move is same-filesystem (instant) since out and REC_DIR share /data."""
    if not REC_URL_BASE:
        return {"links": [], "page_url": ""}
    token = secrets.token_urlsafe(18)
    dest_dir = os.path.join(REC_DIR, token)
    os.makedirs(dest_dir, exist_ok=True)
    urls = []
    published = []
    for path in files:
        if not os.path.exists(path):
            continue
        name = os.path.basename(path)
        shutil.move(path, os.path.join(dest_dir, name))
        urls.append(f"{REC_URL_BASE}/{token}/{quote(name)}")
        if items:
            published.append(next((i for i in items if i.get("path") == path), {"name": name}))
    if published:
        _write_download_index(dest_dir, title, published, lang)
    return {"links": urls, "page_url": f"{REC_URL_BASE}/{token}/" if published else ""}


def _download_link_kind(name: str, lang: str) -> str:
    ext = os.path.splitext(name.lower())[1]
    if ext in {".mkv", ".mp4", ".mov", ".avi", ".ts", ".webm"}:
        return "🎬 וידאו" if lang == "he" else "🎬 Video"
    if ext in {".srt", ".vtt", ".ass", ".ssa", ".ttml", ".stpp", ".wvtt"}:
        return "💬 כתוביות" if lang == "he" else "💬 Subtitles"
    if ext in {".m4a", ".mka", ".mp3", ".aac", ".flac", ".opus", ".wav"}:
        return "🎧 אודיו" if lang == "he" else "🎧 Audio"
    return "📄 קובץ" if lang == "he" else "📄 File"


def _format_download_links(items: list[dict], links: list[str], lang: str,
                           page_url: str = "") -> str:
    if not items or not links:
        return ""
    file_label = "קובץ" if lang == "he" else "File"
    files_label = "קבצים" if lang == "he" else "Files"
    page_label = "עמוד הורדה מסודר" if lang == "he" else "Download page"
    heading = file_label if len(links) == 1 else files_label
    lines = [f"📦 {heading}:"]
    if page_url and len(links) > 1:
        lines.append(f'\n🔗 <a href="{html.escape(page_url, quote=True)}">{page_label}</a>')
    for item, url in list(zip(items, links))[:12]:
        name = html.escape(str(item.get("name") or "download"))
        href = html.escape(url, quote=True)
        size = item.get("size") or 0
        kind = _download_link_kind(str(item.get("name") or ""), lang)
        meta = kind
        if size:
            meta += f" · 💾 {_fmt_size(size)}"
        lines.append(f'\n{meta}\n<a href="{href}">{name}</a>')
        details = str(item.get("details_html") or "")
        if details and len(links) <= 3:
            lines.append(details)
    if len(links) > 12:
        lines.append(f"\n... +{len(links) - 12}")
    return "\n".join(lines)


def _format_file_summary(items: list[dict], lang: str, details_limit: int = 3) -> str:
    if not items:
        return ""
    file_label = "קובץ" if lang == "he" else "File"
    files_label = "קבצים" if lang == "he" else "Files"
    heading = file_label if len(items) == 1 else files_label
    lines = [f"📦 {heading}:"]
    for item in items[:12]:
        name = html.escape(str(item.get("name") or "download"))
        kind = _download_link_kind(str(item.get("name") or ""), lang)
        size = item.get("size") or 0
        meta = kind
        if size:
            meta += f" · 💾 {_fmt_size(size)}"
        lines.append(f"\n{meta}\n<code>{name}</code>")
        details = str(item.get("details_html") or "")
        if details and len(items) <= details_limit:
            lines.append(details)
    if len(items) > 12:
        more = len(items) - 12
        lines.append(f"\n... +{more}")
    return "\n".join(lines)


async def _decide_gofile(chat: int, uid: int, mid: int, job_id: str, head_name: str,
                         lang: str, is_monitor: bool, has_big: bool) -> bool:
    """Whether to also publish this job's files to a gofile folder link. Honours the user's
    setting (ask/always/never). An over-cap file has no Telegram path, so it forces gofile on
    regardless of the preference (otherwise it could not be delivered at all)."""
    if has_big:                         # no Telegram path -> gofile is the only way; don't bother asking
        return True
    mode = users.gofile_mode(uid)
    if mode == "always":
        return True
    if mode == "never":
        return False
    if is_monitor:                      # automated runs can't be prompted
        return False
    ev = asyncio.Event()
    _gfask[job_id] = ev
    _gfask_ans.pop(job_id, None)
    rows = [[(tr("YES_GOFILE", lang), f"gfask:{job_id}:y"),
             (tr("NO_TG_ONLY", lang), f"gfask:{job_id}:n")]]
    await edit(chat, mid, f"🎬 {head_name}\n☁️ " + tr("ASK_GOFILE", lang), rows)
    try:
        await asyncio.wait_for(ev.wait(), timeout=180)
    except asyncio.TimeoutError:
        pass
    _gfask.pop(job_id, None)
    ans = _gfask_ans.pop(job_id, None)
    return bool(ans)                    # no answer in time -> no (Telegram-only)


# --------------------------------------------------------------------------
# Job output helpers
# --------------------------------------------------------------------------
def job_outdir(uid: int) -> str:
    """A unique, isolated output dir per job - we pass it to unshackle as output_dir,
    so afterwards we list exactly the files it produced (no extension/mtime guessing)."""
    d = config.STATE_DIR / "out" / f"{uid}_{int(time.time() * 1000)}"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def list_output_files(outdir: str) -> list[str]:
    """Every file unshackle wrote into the job's isolated output dir."""
    out = []
    for root, _, files in os.walk(outdir):
        out += [os.path.join(root, f) for f in files]
    return sorted(out, key=os.path.getmtime)


def _chunk_lines(text: str, limit: int = 3500) -> list[str]:
    """Split text into chunks of at most `limit` chars without breaking a line (a single
    over-long line is emitted on its own). Keeps each Telegram message under the 4096 cap."""
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        piece = (cur + "\n" + line) if cur else line
        if len(piece) > limit and cur:
            chunks.append(cur)
            cur = line
        else:
            cur = piece
    if cur:
        chunks.append(cur)
    return chunks


async def _deliver_keys(chat: int, uid: int, mid: int, j: dict, lang: str):
    """Deliver a keys-only job: parse the engine's export, then send the shareable text block(s)
    plus a JSON file ({title: {manifest, keys}}). No media was downloaded."""
    entries = keys_extract.parse_export(j.get("keys_export") or {})
    if not entries:
        return await edit(chat, mid, "🔑 " + tr("KEYS_NONE_FOUND", lang),
                          [[(tr("MENU", lang), "m:main")]])
    nkeys = sum(len(e["keys"]) for e in entries)
    header = "🔑 " + tr("KEYS_READY", lang).format(n=nkeys)
    await edit(chat, mid, header)
    for chunk in _chunk_lines(keys_extract.format_text(entries)):
        await send(chat, f"<pre>{html.escape(chunk)}</pre>")
    fname = (re.sub(r"[^\w.-]+", "_", entries[0]["name"]).strip("_") or "keys")[:50] + ".json"
    await send_document(chat, fname, keys_extract.format_json_str(entries).encode("utf-8"),
                        caption=header, rows=[[(tr("MENU", lang), "m:main")]])


# --------------------------------------------------------------------------
# Track selection ↔ engine flags
# --------------------------------------------------------------------------
# track selection ↔ legacy "mode" string (for back-compat with monitors saved before checkboxes)
_MODE_SEL = {"full": {"video", "audio"}, "fullsubs": {"video", "audio", "subs"},
             "video": {"video"}, "audio": {"audio"}, "subs": {"subs"}}
_TRACK_LABEL = {"video": "TRACK_VIDEO", "audio": "TRACK_AUDIO", "subs": "TRACK_SUBS"}


def to_sel(val) -> set:
    """Normalize a track selection: a list/set passes through; a legacy mode string maps."""
    if isinstance(val, (list, set, tuple)):
        return set(val) or {"video", "audio"}
    return set(_MODE_SEL.get(val, {"video", "audio"}))


def sel_label(val, lang: str = "en") -> str:
    sel = to_sel(val)
    return " + ".join(tr(_TRACK_LABEL[k], lang) for k in ("video", "audio", "subs") if k in sel) or "?"


def build_flags(uid: int, service: str, profile: str, sel, quality,
                s_lang=None, sub_extra_lang=None, cdm=None, a_lang=None,
                vcodec=None, keys_only=False):
    """Build the unshackle download flags + quality list for a track selection (any combo of
    video/audio/subs). Shared by the wizard (start_download) and the auto-monitor.

    keys_only adds skip_dl + export: the engine licenses the tracks (so we get the content keys)
    but writes no media, and returns the manifest + keys on the job as `keys_export`."""
    sel = to_sel(sel)
    q = None if (quality == "best" or "video" not in sel) else [int(quality)]
    if sel == {"audio"}:                       # audio only → keep the clean original (no remux)
        flags = {"audio_only": True, "no_video": True, "no_mux": True}
    elif sel == {"subs"}:                      # subtitles only
        flags = {"subs_only": True, "no_video": True, "no_audio": True, "no_mux": True}
    else:                                      # any combo: drop what isn't selected
        flags = {}
        if "video" not in sel:
            flags["no_video"] = True
        if "audio" not in sel:
            flags["no_audio"] = True
        if "subs" not in sel:
            flags["no_subs"] = True
    if "subs" in sel:
        flags["sub_format"] = "SRT"
        if s_lang:
            flags["s_lang"] = s_lang
        if sub_extra_lang:
            flags["sub_lang"] = sub_extra_lang
    if a_lang and "audio" in sel:          # chosen audio language(s); ["all"] keeps every language
        flags["a_lang"] = a_lang
    if vcodec and "video" in sel:          # chosen video codec(s); None lets the engine choose
        flags["vcodec"] = vcodec
    cred = auth.get_credential(uid, service, profile)   # user:pass account → credential
    if cred:
        flags["credential"] = cred
    if cdm:                                             # per-user wvd device ("" = shared default)
        flags["cdm"] = cdm
    if utag := users.tag_pref(uid):                     # per-user release-group tag ("" = server default)
        flags["tag"] = utag
    # Use the region proxy ONLY for the geo-gated manifest/API; download the media segments
    # directly (usually faster than routing bulk traffic through the proxy). No effect on
    # services without a proxy. Services whose segments are themselves geo-locked (e.g. MAKO's
    # CloudFront 900p) must route segments through the proxy too - list them in SEGMENT_PROXY_SERVICES.
    if service not in config.SEGMENT_PROXY_SERVICES:
        flags["no_proxy_download"] = True
    else:
        # The engine worker subprocess runs with proxy_providers=[] so GEOFENCE never auto-resolves
        # the proxy. Pass it explicitly so the worker sets it into session.proxies from the start.
        # The service's authenticate() saves it as _cdn_proxy and clears session.proxies for API
        # calls; get_tracks() restores it so track.download() picks it up for segment fetching.
        flags["proxy"] = config.SEGMENT_PROXY_URL
    flags["skip_subtitle_errors"] = True   # a failed subtitle never aborts the download
    if keys_only:                          # license only: fetch keys, write nothing, return the export
        flags["skip_dl"] = True
        flags["export"] = True
    return flags, q


# --------------------------------------------------------------------------
# Submit + poll
# --------------------------------------------------------------------------
async def start_download(chat: int, uid: int, mid: int, profile: str):
    s = sess(uid)
    lang = users.lang(uid)
    if not can_use(uid, s.get("service", "")):
        return await edit(chat, mid, "🚫 " + tr("YOU_DON_HAVE_ACCESS_2", lang),
                          [[(tr("MENU", lang), "m:main")]])
    limit = users.concurrency_limit(uid)           # pre-check for a friendly message; launch_download
    if len(active_jobs.get(uid, ())) >= limit:     # is the authoritative atomic gate
        return await edit(chat, mid, "⏳ " + tr("YOU_RE_ALREADY_DOWNLOADING", lang).format(limit=limit), [[(tr("MY_DOWNLOADS", lang), "m:dls")],
                          [(tr("MENU", lang), "m:main")]])
    keys_only = bool(s.get("keys_only"))
    if not keys_only:
        name = s.get("name") or s.get("service", "") or tr("DOWNLOAD", lang)
        svc = s.get("service", "")
        head_name = html.escape(str(name))[:48] + (f" · 📺 {html.escape(svc)}" if svc else "")
        delivery_mode = users.delivery_mode(uid)
        if not s.get("_preflight_delivery_done"):
            s["dl_profile"] = profile
            if delivery_mode == "ask":
                rows = [[(tr("DELIVER_TELEGRAM", lang), "predlv:t"),
                         (tr("DELIVER_LINK", lang), "predlv:l")],
                        [(tr("DELIVER_GOFILE", lang), "predlv:g")]]
                return await edit(chat, mid, f"🎬 {head_name}\n📦 " + tr("ASK_DELIVERY", lang), rows)
            s["delivery_link"] = delivery_mode == "link"
            s["gofile_only"] = False
            s["_preflight_delivery_done"] = True
        if s.get("delivery_link") or s.get("gofile_only"):
            s["_preflight_gofile_done"] = True
            s["gofile"] = bool(s.get("gofile_only"))
        elif not s.get("_preflight_gofile_done"):
            gofile_mode = users.gofile_mode(uid)
            s["dl_profile"] = profile
            if gofile_mode == "ask":
                rows = [[(tr("YES_GOFILE", lang), "pregf:y"),
                         (tr("NO_TG_ONLY", lang), "pregf:n")]]
                return await edit(chat, mid, f"🎬 {head_name}\n☁️ " + tr("ASK_GOFILE", lang), rows)
            s["gofile"] = gofile_mode == "always"
            s["_preflight_gofile_done"] = True
    delivery_link = bool(s.get("delivery_link")) if not keys_only else False
    gofile_choice = bool(s.get("gofile")) if not keys_only else False
    gofile_only = bool(s.get("gofile_only")) if not keys_only else False
    flags, q = build_flags(uid, s["service"], profile, s.get("tsel"), s.get("quality"),
                           s.get("s_lang"), s.get("sub_extra_lang"), s.get("cdm"), a_lang=s.get("a_lang"),
                           vcodec=s.get("vcodec"), keys_only=keys_only)
    await edit(chat, mid, ("🔑 " + tr("KEYS_EXTRACTING", lang)) if keys_only
               else "⏳ " + tr("STARTING_DOWNLOAD", lang))
    users.note_recent(uid, s["service"])           # remember for the 🕘 אחרונים tab
    # snapshot the source now (not at completion) so a new wizard mid-download can't corrupt it
    await launch_download(chat, uid, mid, service=s["service"], title_id=s["title_id"],
                          profile=profile, wanted=s.get("wanted"), quality=q, flags=flags,
                          name=s.get("name") or s.get("service", ""), send_as=s.get("send_as"),
                          cover=s.get("cover"), src_url=s.get("title_id"),
                          source_media=s.get("source_media", ""),
                          description=s.get("description", ""), upload_date=s.get("upload_date", ""),
                          cover_url=s.get("cover_url", ""), keys_only=keys_only,
                          delivery_link=delivery_link, gofile_upload=gofile_choice,
                          gofile_only=gofile_only)
    for k in ("_preflight_delivery_done", "_preflight_gofile_done", "_preflight_sendas_done",
              "_sub_lang_chosen", "_vcodec_chosen", "delivery_link", "gofile", "gofile_only", "vcodec"):
        s.pop(k, None)


async def download_file(file_id: str, dest: str) -> None:
    """Download a Telegram file (by file_id) to a local path."""
    f = await call("getFile", file_id=file_id)
    async with aiohttp.ClientSession() as cs:
        async with cs.get(f"{FILE_API}/{f['result']['file_path']}") as r:
            blob = await r.read()
    with open(dest, "wb") as fp:
        fp.write(blob)


async def download_file_stream(file_id: str, dest: str) -> None:
    """Download a Telegram file to disk in chunks - safe for large files (no full-size buffer
    in memory). The local Bot API server serves files up to its 2GB cap by path."""
    f = await call("getFile", file_id=file_id)
    timeout = aiohttp.ClientTimeout(total=7200)
    async with aiohttp.ClientSession(timeout=timeout) as cs:
        async with cs.get(f"{FILE_API}/{f['result']['file_path']}") as r:
            r.raise_for_status()
            with open(dest, "wb") as fp:
                async for chunk in r.content.iter_chunked(1 << 20):
                    fp.write(chunk)


_resv_seq = 0   # monotonic id for synchronous concurrency-slot reservations


async def launch_download(chat: int, uid: int, mid: int, *, service, title_id, profile, wanted,
                          quality, flags, name, src_url=None, source_media="", retried=False,
                          send_as=None, cover=None, is_monitor=False, gate=True,
                          description="", upload_date="", cover_url="", keys_only=False,
                          delivery_link=None, gofile_upload=None, gofile_only=None):
    """Submit a download to the engine and start polling it. The single submit seam shared by
    the wizard and the auto-monitor: validates the profile, atomically reserves a concurrency
    slot (gate=True), and carries a dl_spec for the geofence proxy retry (which passes gate=False
    since it continues an existing job)."""
    # default-deny a forged profile (cookie files are keyed by profile -> another user's cookies)
    if profile not in ({str(uid)} | {a["profile"] for a in auth.list_accounts(uid, service)}):
        profile = str(uid)
    # fall back to admin-provided shared cookies when the user has no account of their own for this
    # service (e.g. YouTube catch-all downloads that otherwise hit "confirm you're not a bot")
    if not auth.list_accounts(uid, service) and (auth.has_default_cookies(service) or auth.has_default_credential(service)):
        profile = auth.DEFAULT_PROFILE
        # Credential-based default services (e.g. STING): the credential must travel in flags, but
        # build_flags resolved it against the user's own (empty) profile before this fallback ran, so
        # flags has none. Re-resolve for the default profile now, else the engine authenticates with
        # nothing and the service errors "login required" (cookie defaults load by profile, so unaffected).
        if "credential" not in flags:
            cred = auth.get_credential(uid, service, auth.DEFAULT_PROFILE)
            if cred:
                flags["credential"] = cred
    if not is_monitor:                              # remember this attempt for a one-tap retry on failure
        retry_spec[uid] = dict(service=service, title_id=title_id, profile=profile, wanted=wanted,
                               quality=quality, flags=flags, name=name, src_url=src_url,
                               source_media=source_media, send_as=send_as, cover=cover,
                               description=description, upload_date=upload_date, cover_url=cover_url,
                               keys_only=keys_only, delivery_link=delivery_link,
                               gofile_upload=gofile_upload, gofile_only=gofile_only)
    jobs = active_jobs.setdefault(uid, {})
    resv = None
    if gate:                                       # atomic check + reserve, no await in between
        if len(jobs) >= users.concurrency_limit(uid):
            return                                 # lost a concurrency race (pre-check messaged the user)
        global _resv_seq
        _resv_seq += 1
        resv = f"\x00resv{_resv_seq}"
        jobs[resv] = {"name": name, "chat": chat}  # hold the slot across the engine call
    outdir = job_outdir(uid)
    started = False
    try:
        try:
            resp = await engine.download(service, title_id, profile=profile, wanted=wanted,
                                         quality=quality, output_dir=outdir, **flags)
        except UnshackleError as e:
            return await user_error(chat, mid, uid, e, allow_retry=not is_monitor)
        job_id = (resp.get("job_id") or resp.get("id")) if isinstance(resp, dict) else None
        if not job_id:
            return await user_error(chat, mid, uid, f"no job_id from API: {resp}", allow_retry=not is_monitor)
        jobs[job_id] = {"name": name, "chat": chat}
        started = True                              # the poll task now owns the slot + outdir
        spec = {"service": service, "title_id": title_id, "profile": profile, "wanted": wanted,
                "quality": quality, "flags": flags, "name": name, "retried": retried,
                "send_as": send_as, "cover": cover, "is_monitor": is_monitor,
                "description": description, "upload_date": upload_date, "cover_url": cover_url,
                "keys_only": keys_only, "delivery_link": delivery_link,
                "gofile_upload": gofile_upload, "gofile_only": gofile_only}
        asyncio.create_task(poll_job(chat, uid, mid, job_id, outdir, src_url=src_url,
                                     source_media=source_media, dl_spec=spec, is_monitor=is_monitor))
    finally:
        if resv:
            jobs.pop(resv, None)                    # always release the reservation placeholder
        if not started:
            shutil.rmtree(outdir, ignore_errors=True)   # no poll task -> don't leak the job dir


async def poll_job(chat: int, uid: int, mid: int, job_id: str, outdir: str, src_url=None,
                   source_media="", dl_spec=None, is_monitor=False):
    lang = users.lang(uid)
    try:
        await _poll_job(chat, uid, mid, job_id, outdir, src_url, source_media, dl_spec, is_monitor)
    except Exception as e:
        await report_error("poll_job", e, uid)
        try:
            await edit(chat, mid, "❌ " + tr("UNEXPECTED_ERROR_DURING_DOWNLOAD", lang), [[(tr("MENU", lang), "m:main")]])
        except Exception:
            pass
    finally:
        active_jobs.get(uid, {}).pop(job_id, None)   # free the user's concurrency slot
        shutil.rmtree(outdir, ignore_errors=True)    # never leak the job dir (idempotent)


async def redraw_progress(chat: int, uid: int, mid: int, job_id: str) -> None:
    """Restore the live progress view after the user backs out of the cancel confirmation
    ('keep going'). Best-effort: the owning poll loop keeps refreshing it on its own cadence."""
    lang = users.lang(uid)
    if job_id not in active_jobs.get(uid, {}):       # already finished/cancelled - nothing to resume
        return await edit(chat, mid, tr("CANCELLED", lang), [[(tr("MENU", lang), "m:main")]])
    try:
        j = await engine.job(job_id)
    except Exception:
        j = {}
    name = (active_jobs.get(uid, {}).get(job_id) or {}).get("name") or sess(uid).get("name") \
        or tr("DOWNLOAD", lang)
    svc = sess(uid).get("service") or ""
    head_name = html.escape(str(name))[:48] + (f" · 📺 {html.escape(svc)}" if svc else "")
    line = _render_progress(head=f"🎬 {head_name}\n⬇️ {_phase(j.get('phase'), lang)}",
                            pct=int(j.get("progress") or 0), segs_done=j.get("segments_done"),
                            segs_total=j.get("segments_total"), speed_str=j.get("speed"))
    await edit(chat, mid, line, [[(tr("CANCEL_3", lang), f"cxl:{job_id}")]])


async def _poll_job(chat: int, uid: int, mid: int, job_id: str, outdir: str, src_url=None,
                    source_media="", dl_spec=None, is_monitor=False):
    last = None
    t_start = time.time()                     # wall-clock, for the "done in M:SS" closing stat
    lang = users.lang(uid)
    tag = ("🤖 " + tr("MONITOR", lang) + " · ") if is_monitor else ""
    name = (active_jobs.get(uid, {}).get(job_id) or {}).get("name") or sess(uid).get("name") \
        or tr("DOWNLOAD", lang)
    svc = (dl_spec or {}).get("service") or sess(uid).get("service") or ""
    head_name = tag + html.escape(str(name))[:48] + (f" · 📺 {html.escape(svc)}" if svc else "")
    hist = []                                 # (time, pct) samples → ETA estimate
    stall = {"p": -1, "s": None, "t": time.time()}   # watchdog: last (pct, segments, change-time)
    STALL_SECS = 75                           # no progress this long (early) → fall back to proxy
    PROXY_STALL_SECS = 120                    # proxy downloads stalling longer → fail with network error

    def _cancelled() -> bool:
        return bool((active_jobs.get(uid, {}).get(job_id) or {}).get("cancelled"))

    fails = 0                                 # consecutive engine.job() failures
    for _ in range(7200):                     # up to ~8h (4s poll); bails earlier on the cases below
        await asyncio.sleep(4)
        if _cancelled():                      # user cancelled: stop, don't deliver/retry
            shutil.rmtree(outdir, ignore_errors=True)
            return
        try:
            j = await engine.job(job_id)
            fails = 0
        except Exception:
            fails += 1
            if fails >= 15:                   # engine unreachable ~1min → stop, tell the user
                shutil.rmtree(outdir, ignore_errors=True)
                return await user_error(chat, mid, uid, "lost contact with the download engine",
                                        allow_retry=not is_monitor)
            continue
        status = j.get("status")
        prog = int(j.get("progress") or 0)
        if status in ("queued", "starting"):
            line = f"🎬 {head_name}\n⏳ " + tr("STARTING", lang)
        elif status in ("downloading", "running"):
            now = time.time()
            sd, st_ = j.get("segments_done"), j.get("segments_total")
            hist.append((now, prog))
            hist[:] = [(t, p) for (t, p) in hist if now - t <= 30] or hist[-1:]
            eta = ""
            if len(hist) >= 2 and prog > 0:
                dt, dp = hist[-1][0] - hist[0][0], hist[-1][1] - hist[0][1]
                if dt > 0 and dp > 0:
                    eta = tr("LEFT", lang).format(t=_fmt_eta((100 - prog) * dt / dp, lang))
            # stall watchdog: geo-locked segments downloaded directly HANG (no progress) rather than
            # failing, so the failed-retry below never fires. After STALL_SECS with no advance early
            # in a direct geofenced download, cancel and fall back to the proxy (same as a failure).
            if prog != stall["p"] or sd != stall["s"]:
                stall.update(p=prog, s=sd, t=now)
            elif (now - stall["t"] > STALL_SECS and prog < 50 and dl_spec
                  and not dl_spec.get("retried") and not dl_spec.get("keys_only")
                  and dl_spec["flags"].get("no_proxy_download")
                  and state.meta(dl_spec["service"]).get("geofence")):
                try:
                    await engine.cancel(job_id)
                except Exception:
                    pass
                await edit(chat, mid, f"🎬 {head_name}\n⏳ " + tr("SWITCHING_TO_DOWNLOAD_VIA", lang),
                           [[(tr("CANCEL_3", lang), f"cxl:{job_id}")]])
                return await launch_download(
                    chat, uid, mid, service=dl_spec["service"], title_id=dl_spec["title_id"],
                    profile=dl_spec["profile"], wanted=dl_spec["wanted"], quality=dl_spec["quality"],
                    flags={**dl_spec["flags"], "no_proxy_download": False}, name=dl_spec["name"],
                    src_url=src_url, source_media=source_media, retried=True,
                    send_as=dl_spec.get("send_as"), cover=dl_spec.get("cover"), is_monitor=is_monitor,
                    gate=False, description=dl_spec.get("description", ""),
                    upload_date=dl_spec.get("upload_date", ""), cover_url=dl_spec.get("cover_url", ""),
                    delivery_link=dl_spec.get("delivery_link"),
                    gofile_upload=dl_spec.get("gofile_upload"),
                    gofile_only=dl_spec.get("gofile_only"))
            elif (now - stall["t"] > PROXY_STALL_SECS and prog < 50 and dl_spec
                  and not dl_spec.get("keys_only")
                  and not dl_spec["flags"].get("no_proxy_download")):
                # proxy download is stalling (CDN unreachable through proxy, silent hang)
                try:
                    await engine.cancel(job_id)
                except Exception:
                    pass
                await user_error(chat, mid, uid, "timed out connecting to CDN through proxy",
                                 allow_retry=not is_monitor)
                return
            line = _render_progress(head=f"🎬 {head_name}\n⬇️ {_phase(j.get('phase'), lang)}",
                                    pct=prog, eta=eta, segs_done=sd, segs_total=st_,
                                    speed_str=j.get("speed"))
        else:
            line = f"🎬 {head_name}\n⏳ {status}"
        if line != last:
            last = line
            await edit(chat, mid, line, [[(tr("CANCEL_3", lang), f"cxl:{job_id}")]])
        if status == "completed":
            if _cancelled():                         # cancelled after the engine finished, before upload
                shutil.rmtree(outdir, ignore_errors=True)
                return
            if (dl_spec or {}).get("keys_only"):     # keys-only job: no files, deliver the export
                shutil.rmtree(outdir, ignore_errors=True)
                return await _deliver_keys(chat, uid, mid, j, lang)
            files = list_output_files(outdir)        # exactly what unshackle wrote here
            if not files:
                shutil.rmtree(outdir, ignore_errors=True)
                await edit(chat, mid, "🎉 " + tr("DONE_BUT_NO_FILE", lang), [[(tr("MENU", lang), "m:main")]])
                return
            total_bytes = sum(os.path.getsize(f) for f in files if os.path.exists(f))   # before upload/cleanup
            from urllib.parse import urlparse
            src_name = (urlparse(src_url).hostname or "").replace("www.", "") or src_url or "?"
            total = len(files)
            # Primary delivery choice: a self-hosted, expiring download link instead of a Telegram
            # upload (telegram/link/ask per the user's setting). 'link' is also the natural path for
            # over-cap files (no Telegram size limit on a direct link).
            if dl_spec and dl_spec.get("delivery_link") is not None:
                use_link = bool(dl_spec.get("delivery_link"))
            else:
                use_link = await _decide_link(chat, uid, mid, job_id, head_name, lang, is_monitor)
            if use_link:
                link_items = [
                    {
                        "path": f,
                        "name": os.path.basename(f),
                        "size": os.path.getsize(f),
                        "details_html": metadata.media_details_block(f, lang),
                    }
                    for f in files
                    if os.path.exists(f)
                ]
                published = publish_link([item["path"] for item in link_items],
                                         title=head_name, items=link_items, lang=lang)
                links = published.get("links") or []
                shutil.rmtree(outdir, ignore_errors=True)
                if not links:
                    await edit(chat, mid, "🎉 " + tr("DONE_BUT_NO_FILE", lang),
                               [[(tr("MENU", lang), "m:main")]])
                    return
                msg = "✅ " + tr("LINK_READY", lang)
                stats = []
                if total_bytes:
                    stats.append(f"💾 {_fmt_size(total_bytes)}")
                el = time.time() - t_start
                if el >= 1:
                    stats.append(f"⏱️ {_fmt_eta(el, lang)}")
                if stats:
                    msg += "\n" + " · ".join(stats)
                link_block = _format_download_links(link_items, links, lang,
                                                    page_url=published.get("page_url") or "")
                if link_block:
                    msg += "\n\n" + link_block
                msg += "\n\n" + tr("REC_LINK_EXPIRES", lang)
                await edit(chat, mid, msg, [[(tr("MENU", lang), "m:main")]])
                return
            # Optional extra: publish the whole job (all files) into ONE gofile folder -> one link.
            # Honours the user's ask/always/never setting; an over-cap file forces it on (no TG path).
            has_big = any(os.path.getsize(f) > uploader.max_cap() for f in files if os.path.exists(f))
            gofile_only = bool(dl_spec and dl_spec.get("gofile_only"))
            if has_big:
                want_gf = True
            elif gofile_only:
                want_gf = True
            elif dl_spec and dl_spec.get("gofile_upload") is not None:
                want_gf = bool(dl_spec.get("gofile_upload"))
            else:
                want_gf = await _decide_gofile(chat, uid, mid, job_id, head_name, lang, is_monitor, has_big)
            gf_sess = gofile.Session() if want_gf else None
            file_items = [
                {
                    "path": f,
                    "name": os.path.basename(f),
                    "size": os.path.getsize(f),
                    "details_html": metadata.media_details_block(f, lang),
                }
                for f in files
                if os.path.exists(f)
            ]
            for idx, path in enumerate(files, 1):
                if _cancelled():                     # cancelled mid multi-file upload
                    shutil.rmtree(outdir, ignore_errors=True)
                    return
                # show the content title (what we're uploading) + the actual file name
                fname = html.escape(os.path.basename(path))
                count = f" {idx}/{total}" if total > 1 else ""
                head = f"🎬 {head_name}\n⬆️ " + tr("UPLOADING", lang) + f"{count}\n<code>{fname}</code>"
                await edit(chat, mid, _render_progress(head=head, pct=0))
                seen = {"p": -10, "t": 0.0, "t0": time.time(), "cur": 0, "tot": 0, "spd": 0}

                async def on_up(cur, tot, _head=head):           # real-time upload progress
                    pct = int(cur * 100 / tot) if tot else 0
                    now = time.time()
                    # throttle by BOTH step and time (Telegram limits edits to ~1/sec)
                    if pct >= 100 or (pct - seen["p"] >= 3 and now - seen["t"] >= 2.5):
                        seen["p"], seen["t"] = pct, now
                        el = now - seen["t0"]
                        spd = cur / el if el > 0 else 0
                        seen.update(cur=cur, tot=tot, spd=spd)
                        eta = tr("LEFT", lang).format(t=_fmt_eta((tot - cur) / spd, lang)) \
                            if (spd > 0 and tot) else ""
                        try:
                            await edit(chat, mid, _render_progress(
                                head=_head, pct=pct, done_b=cur, total_b=tot, speed_bps=spd, eta=eta))
                        except Exception:
                            pass

                async def on_phase(_head=head):                  # Bot API: localhost buffer done,
                    try:                                         # the real upload to Telegram begins
                        tot = seen.get("tot") or (os.path.getsize(path) if os.path.exists(path) else 0)
                        line = _render_progress(
                            head=_head, pct=100, done_b=tot, total_b=tot,
                            speed_bps=seen.get("spd") or None)
                        await edit(chat, mid, f"{line}\n⏳ " + tr("UPLOADING_TO_TELEGRAM", lang),
                                   [[(tr("CANCEL_3", lang), f"cxl:{job_id}")]])
                    except Exception:
                        pass

                size = os.path.getsize(path) if os.path.exists(path) else 0
                too_big = size > uploader.max_cap()
                # gofile first (it needs the file on disk - uploader.deliver deletes it). All the
                # job's files land in gf_sess's single folder, so we surface one link at the end.
                if gf_sess and size:
                    try:
                        await edit(chat, mid, f"{head}\n☁️ " + tr("UPLOADING_GOFILE", lang),
                                   [[(tr("CANCEL_3", lang), f"cxl:{job_id}")]])
                        await gf_sess.add(path, progress=on_up)
                    except Exception as ge:
                        print(f"gofile upload failed ({os.path.basename(path)}): {ge}")
                        if too_big:                              # no Telegram path and gofile failed
                            await gf_sess.aclose()
                            shutil.rmtree(outdir, ignore_errors=True)
                            await user_error(chat, mid, uid,
                                             f"file too large for Telegram and gofile upload failed: {ge}",
                                             allow_retry=not is_monitor)
                            return
                if too_big or gofile_only:                       # delivered via the gofile link only
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    continue

                try:
                    await uploader.deliver(chat, path, service=src_name, source_url=src_url,
                                           media_url=source_media, progress=on_up, phase_cb=on_phase,
                                           force_kind=(dl_spec or {}).get("send_as"),
                                           cover_path=(dl_spec or {}).get("cover"), lang=lang,
                                           display_title=(dl_spec or {}).get("name") or "",
                                           description=(dl_spec or {}).get("description") or "",
                                           upload_date=(dl_spec or {}).get("upload_date") or "",
                                           cover_url=(dl_spec or {}).get("cover_url") or "")
                except Exception as e:
                    if gf_sess:
                        await gf_sess.aclose()
                    shutil.rmtree(outdir, ignore_errors=True)
                    await user_error(chat, mid, uid, f"upload failed ({os.path.basename(path)}): {e}",
                                     allow_retry=not is_monitor)
                    return
            gf_link = gf_sess.link if gf_sess else None
            if gf_sess:
                await gf_sess.aclose()
            shutil.rmtree(outdir, ignore_errors=True)   # remove the now-empty job dir
            if gofile_only:
                msg = "✅ " + tr("GOFILE_READY", lang)
            else:
                msg = ("🎉 " + tr("SENT_FILES", lang).format(total=total)) if total > 1 \
                    else "🎉 " + tr("SENT", lang)
            stats = []                                   # a small closing line: total size + elapsed
            if total_bytes:
                stats.append(f"💾 {_fmt_size(total_bytes)}")
            elapsed = time.time() - t_start
            if elapsed >= 1:
                stats.append(f"⏱️ {_fmt_eta(elapsed, lang)}")
            if stats:
                msg += "\n" + " · ".join(stats)
            skipped = j.get("skipped_subtitles") or []   # subtitles that weren't available
            if skipped:
                msg += "\n⚠️ " + tr("SUBTITLES_THAT_WEREN_AVAILABLE", lang) \
                    + ", ".join(_lang_label(s, lang) for s in skipped)
            if gf_link:                                  # one folder link for the whole job
                summary = _format_file_summary(file_items, lang)
                if summary:
                    msg += "\n\n" + summary
                msg += "\n\n🔗 " + tr("GOFILE_READY", lang) + f"\n{gf_link}"
            await edit(chat, mid, msg, [[(tr("MENU", lang), "m:main")]])
            return
        if status == "failed":
            shutil.rmtree(outdir, ignore_errors=True)
            if _cancelled():                         # cancelled - don't retry, don't error
                return
            detail = " | ".join(str(x) for x in (j.get("error"), j.get("worker_stderr"),
                                                 j.get("message")) if x) or "download failed"
            # Graceful fallback: a direct (no-proxy) download failed. Retry once through the proxy.
            # Two triggers: (a) service is formally declared geofenced in the engine metadata,
            # (b) the failure is a network/timeout error - likely geo-locked segments even if the
            # service isn't explicitly marked (e.g. yesplus CDN timing out from a foreign IP).
            is_net_err = any(k in detail.lower() for k in
                             ("timed out", "timeout", "connection", "network error", "resolve host", "unreachable"))
            if (dl_spec and not dl_spec.get("retried") and dl_spec["flags"].get("no_proxy_download")
                    and (state.meta(dl_spec["service"]).get("geofence") or is_net_err)):
                await edit(chat, mid, f"🎬 {head_name}\n⏳ "
                           + tr("SWITCHING_TO_DOWNLOAD_VIA", lang),
                           [[(tr("CANCEL_3", lang), f"cxl:{job_id}")]])
                return await launch_download(
                    chat, uid, mid, service=dl_spec["service"], title_id=dl_spec["title_id"],
                    profile=dl_spec["profile"], wanted=dl_spec["wanted"], quality=dl_spec["quality"],
                    flags={**dl_spec["flags"], "no_proxy_download": False}, name=dl_spec["name"],
                    src_url=src_url, source_media=source_media, retried=True,
                    send_as=dl_spec.get("send_as"), cover=dl_spec.get("cover"), is_monitor=is_monitor,
                    gate=False, description=dl_spec.get("description", ""),   # continues the slot, don't re-gate
                    upload_date=dl_spec.get("upload_date", ""), cover_url=dl_spec.get("cover_url", ""),
                    keys_only=dl_spec.get("keys_only", False),
                    delivery_link=dl_spec.get("delivery_link"),
                    gofile_upload=dl_spec.get("gofile_upload"),
                    gofile_only=dl_spec.get("gofile_only"))
            await user_error(chat, mid, uid, detail, allow_retry=not is_monitor)
            return
    shutil.rmtree(outdir, ignore_errors=True)
    await user_error(chat, mid, uid, "the download did not finish in time", allow_retry=not is_monitor)
