"""The download subsystem: turn a track selection into engine flags, submit the job,
poll it to completion, and hand the files to the uploader. Shared by both the
interactive wizard (start_download) and the auto-monitor (build_flags + launch_download).

This is a lower layer than the menus/monitor UI: it depends on the engine, uploader and
state, but never calls back into them. Progress and errors go out through tg/errors."""
import asyncio
import html
import os
import shutil
import time

import aiohttp

from . import auth, config, state, uploader, users
from .catalog_meta import can_use
from .engine import UnshackleError
from .errors import report_error, user_error
from .format import _fmt_eta, _fmt_size, _lang_label, _phase, _render_progress
from .i18n import tr
from .session import active_jobs, sess
from .state import engine
from .tg import FILE_API, call, edit

# uid -> the kwargs of the user's last interactive launch_download, so a transient failure can
# offer a one-tap "try again" without re-walking the whole wizard. In-memory only (a bot restart
# clears it, which is fine - the user just re-navigates). Monitors are never stored here.
retry_spec: dict[int, dict] = {}


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
                s_lang=None, sub_extra_lang=None, cdm=None):
    """Build the unshackle download flags + quality list for a track selection (any combo of
    video/audio/subs). Shared by the wizard (start_download) and the auto-monitor."""
    sel = to_sel(sel)
    q = None if (quality == "best" or "video" not in sel) else [int(quality)]
    if sel == {"audio"}:                       # audio only → keep the clean original (no remux)
        flags = {"audio_only": True, "no_video": True, "no_mux": True}
    elif sel == {"subs"}:                      # subtitles only
        flags = {"subs_only": True, "no_video": True, "no_audio": True, "no_mux": True, "sub_format": "SRT"}
    else:                                      # any combo: drop what isn't selected
        flags = {}
        if "video" not in sel:
            flags["no_video"] = True
        if "audio" not in sel:
            flags["no_audio"] = True
        if "subs" not in sel:
            flags["no_subs"] = True
    if "subs" in sel:
        if s_lang:
            flags["s_lang"] = s_lang
        if sub_extra_lang:
            flags["sub_lang"] = sub_extra_lang
    cred = auth.get_credential(uid, service, profile)   # user:pass account → credential
    if cred:
        flags["credential"] = cred
    if cdm:                                             # per-user wvd device ("" = shared default)
        flags["cdm"] = cdm
    # Use the region proxy ONLY for the geo-gated manifest/API; download the media segments
    # directly (usually faster than routing bulk traffic through the proxy). No effect on
    # services without a proxy. Services whose segments are themselves geo-locked (e.g. MAKO's
    # CloudFront 900p) must route segments through the proxy too - list them in SEGMENT_PROXY_SERVICES.
    if service not in config.SEGMENT_PROXY_SERVICES:
        flags["no_proxy_download"] = True
    flags["skip_subtitle_errors"] = True   # a failed subtitle never aborts the download
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
    flags, q = build_flags(uid, s["service"], profile, s.get("tsel"), s.get("quality"),
                           s.get("s_lang"), s.get("sub_extra_lang"), s.get("cdm"))
    await edit(chat, mid, "⏳ " + tr("STARTING_DOWNLOAD", lang))
    users.note_recent(uid, s["service"])           # remember for the 🕘 אחרונים tab
    # snapshot the source now (not at completion) so a new wizard mid-download can't corrupt it
    await launch_download(chat, uid, mid, service=s["service"], title_id=s["title_id"],
                          profile=profile, wanted=s.get("wanted"), quality=q, flags=flags,
                          name=s.get("name") or s.get("service", ""), send_as=s.get("send_as"),
                          cover=s.get("cover"), src_url=s.get("title_id"),
                          source_media=s.get("source_media", ""),
                          description=s.get("description", ""), upload_date=s.get("upload_date", ""),
                          cover_url=s.get("cover_url", ""))


async def download_file(file_id: str, dest: str) -> None:
    """Download a Telegram file (by file_id) to a local path."""
    f = await call("getFile", file_id=file_id)
    async with aiohttp.ClientSession() as cs:
        async with cs.get(f"{FILE_API}/{f['result']['file_path']}") as r:
            blob = await r.read()
    with open(dest, "wb") as fp:
        fp.write(blob)


_resv_seq = 0   # monotonic id for synchronous concurrency-slot reservations


async def launch_download(chat: int, uid: int, mid: int, *, service, title_id, profile, wanted,
                          quality, flags, name, src_url=None, source_media="", retried=False,
                          send_as=None, cover=None, is_monitor=False, gate=True,
                          description="", upload_date="", cover_url=""):
    """Submit a download to the engine and start polling it. The single submit seam shared by
    the wizard and the auto-monitor: validates the profile, atomically reserves a concurrency
    slot (gate=True), and carries a dl_spec for the geofence proxy retry (which passes gate=False
    since it continues an existing job)."""
    # default-deny a forged profile (cookie files are keyed by profile -> another user's cookies)
    if profile not in ({str(uid)} | {a["profile"] for a in auth.list_accounts(uid, service)}):
        profile = str(uid)
    if not is_monitor:                              # remember this attempt for a one-tap retry on failure
        retry_spec[uid] = dict(service=service, title_id=title_id, profile=profile, wanted=wanted,
                               quality=quality, flags=flags, name=name, src_url=src_url,
                               source_media=source_media, send_as=send_as, cover=cover,
                               description=description, upload_date=upload_date, cover_url=cover_url)
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
                "description": description, "upload_date": upload_date, "cover_url": cover_url}
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
                  and not dl_spec.get("retried") and dl_spec["flags"].get("no_proxy_download")
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
                    upload_date=dl_spec.get("upload_date", ""), cover_url=dl_spec.get("cover_url", ""))
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
            files = list_output_files(outdir)        # exactly what unshackle wrote here
            if not files:
                shutil.rmtree(outdir, ignore_errors=True)
                await edit(chat, mid, "🎉 " + tr("DONE_BUT_NO_FILE", lang), [[(tr("MENU", lang), "m:main")]])
                return
            total_bytes = sum(os.path.getsize(f) for f in files if os.path.exists(f))   # before upload/cleanup
            from urllib.parse import urlparse
            src_name = (urlparse(src_url).hostname or "").replace("www.", "") or src_url or "?"
            total = len(files)
            for idx, path in enumerate(files, 1):
                if _cancelled():                     # cancelled mid multi-file upload
                    shutil.rmtree(outdir, ignore_errors=True)
                    return
                # show the content title (what we're uploading) + the actual file name
                fname = html.escape(os.path.basename(path))
                count = f" {idx}/{total}" if total > 1 else ""
                head = f"🎬 {head_name}\n⬆️ " + tr("UPLOADING", lang) + f"{count}\n<code>{fname}</code>"
                await edit(chat, mid, _render_progress(head=head, pct=0))
                seen = {"p": -10, "t": 0.0, "t0": time.time()}

                async def on_up(cur, tot, _head=head):           # real-time upload progress
                    pct = int(cur * 100 / tot) if tot else 0
                    now = time.time()
                    # throttle by BOTH step and time (Telegram limits edits to ~1/sec)
                    if pct >= 100 or (pct - seen["p"] >= 3 and now - seen["t"] >= 2.5):
                        seen["p"], seen["t"] = pct, now
                        el = now - seen["t0"]
                        spd = cur / el if el > 0 else 0
                        eta = tr("LEFT", lang).format(t=_fmt_eta((tot - cur) / spd, lang)) \
                            if (spd > 0 and tot) else ""
                        try:
                            await edit(chat, mid, _render_progress(
                                head=_head, pct=pct, done_b=cur, total_b=tot, speed_bps=spd, eta=eta))
                        except Exception:
                            pass

                async def on_phase(_head=head):                  # Bot API: localhost buffer done,
                    try:                                         # the real upload to Telegram begins
                        await edit(chat, mid, f"{_head}\n⏳ " + tr("UPLOADING_TO_TELEGRAM", lang),
                                   [[(tr("CANCEL_3", lang), f"cxl:{job_id}")]])
                    except Exception:
                        pass

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
                    shutil.rmtree(outdir, ignore_errors=True)
                    await user_error(chat, mid, uid, f"upload failed ({os.path.basename(path)}): {e}",
                                     allow_retry=not is_monitor)
                    return
            shutil.rmtree(outdir, ignore_errors=True)   # remove the now-empty job dir
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
            await edit(chat, mid, msg, [[(tr("MENU", lang), "m:main")]])
            return
        if status == "failed":
            shutil.rmtree(outdir, ignore_errors=True)
            if _cancelled():                         # cancelled - don't retry, don't error
                return
            # Graceful fallback: a geofenced service tried a direct (no-proxy) download. If it
            # failed (e.g. geo-locked segments), retry once through the proxy instead of erroring.
            if (dl_spec and not dl_spec.get("retried") and dl_spec["flags"].get("no_proxy_download")
                    and state.meta(dl_spec["service"]).get("geofence")):
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
                    upload_date=dl_spec.get("upload_date", ""), cover_url=dl_spec.get("cover_url", ""))
            detail = " | ".join(str(x) for x in (j.get("error"), j.get("worker_stderr"),
                                                 j.get("message")) if x) or "download failed"
            await user_error(chat, mid, uid, detail, allow_retry=not is_monitor)
            return
    # loop exhausted without a terminal status (engine stuck) - clean up and tell the user
    shutil.rmtree(outdir, ignore_errors=True)
    await user_error(chat, mid, uid, "the download did not finish in time", allow_retry=not is_monitor)
