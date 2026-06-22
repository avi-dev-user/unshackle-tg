"""
Telegram frontend (raw Bot API over aiohttp - needs only BOT_TOKEN).

Inline-only UX: every navigation/action is a button. The user only ever *types*
content (a URL, a search query) after a button asks for it. The Premium userbot
(uploader, Pyrofork) is separate and added later for >50MB files.

On-demand download over the configured services, with per-user multi-account cookies.
Engine = unshackle REST API (see engine.py).
"""
import asyncio
import html
import json
import os
import re
import time

import aiohttp

from . import admin, auth, config, gofile, monitors, recordings, state, uploader, users
from .catalog_meta import (detect_service, load_cat_overrides, set_cat_override, svc_auth_required,
                           unwrap_url)
from .download import (answer_gofile_ask, download_file, download_file_stream, launch_download,
                       redraw_progress, retry_spec, start_download, to_sel)
from .errors import report_error
from .i18n import tr
from .menus import (_after_account, _search_labels, account_service, accounts_menu, ask_input, cdm_menu,
                    gofile_mode_menu, language_menu, main_menu, my_downloads, pick_account_or_go, picker,
                    settings_menu, service_detail, services_grid, show_dl_cover, show_episodes,
                    show_gofile_folder, show_quality, show_search_results, show_send_as, show_sub_langs,
                    show_titles, show_track_types, show_tracks)
from .monitors_ui import (_mon_iv, _mon_last, _parse_interval, _parse_schedule, _save_monitor,
                          _schedule_label, monitor_ask_cover, monitor_ask_interval,
                          monitor_ask_sendas, monitor_ask_start, monitor_ask_tracks,
                          monitor_detail, monitor_edit_cover, monitor_edit_interval,
                          monitor_edit_mode, monitor_edit_sendas, monitor_loop, monitor_menu,
                          monitor_pick_start, monitor_setup)
from .session import active_jobs, sess
from .state import engine
from .tg import FILE_API, call, edit, send

# reply to a message with one of these → broadcast that message (admin only)
BCAST_TRIGGERS = {"שדר", "שדר!", "/broadcast", "broadcast", "📢"}


def _forward_user(msg: dict) -> dict | None:
    """The original sender of a forwarded message, if Telegram exposed it.
    Supports both the legacy `forward_from` and the newer `forward_origin`."""
    f = msg.get("forward_from")
    if isinstance(f, dict) and f.get("id"):
        return f
    o = msg.get("forward_origin")
    if isinstance(o, dict) and o.get("type") == "user" and isinstance(o.get("sender_user"), dict):
        return o["sender_user"]
    return None


def _is_forward(msg: dict) -> bool:
    return any(k in msg for k in ("forward_from", "forward_origin", "forward_from_chat",
                                  "forward_sender_name", "forward_date"))


# --------------------------------------------------------------------------
# Update dispatch
# --------------------------------------------------------------------------
async def on_callback(cq: dict):
    uid = cq["from"]["id"]
    chat = cq["message"]["chat"]["id"]
    mid = cq["message"]["message_id"]
    data = cq["data"]
    await call("answerCallbackQuery", callback_query_id=cq["id"])

    users.touch(cq.get("from") or {})
    if not users.is_allowed(uid):            # unknown/suspended → silently ignore
        return
    lang = users.lang(uid)
    if data == "noop":
        return
    if data in ("m:main",):
        return await main_menu(chat, uid, mid)
    if data == "m:settings":
        return await settings_menu(chat, uid, mid)
    if data == "m:lang":
        return await language_menu(chat, uid, mid)
    if data.startswith("lang:"):
        users.set_lang(uid, data.split(":", 1)[1])
        return await settings_menu(chat, uid, mid)
    if data == "m:gfmode":
        return await gofile_mode_menu(chat, uid, mid)
    if data.startswith("gfmode:"):
        users.set_gofile_mode(uid, data.split(":", 1)[1])
        return await gofile_mode_menu(chat, uid, mid)
    if data.startswith("gfask:"):                    # answer to the "upload to gofile?" prompt
        _, jid, yn = data.split(":", 2)
        return answer_gofile_ask(jid, yn == "y")
    if data.startswith("gfd:"):                      # gofile download folder controls
        gf = sess(uid).get("gfd") or {}
        if data in ("gfd:sa:video", "gfd:sa:file"):
            gf["send_as"] = data.rsplit(":", 1)[1]
            sess(uid)["gfd"] = gf
            return await show_gofile_folder(chat, uid, mid)
        if data == "gfd:cov":
            sess(uid)["step"] = "await_gf_cover"
            return await edit(chat, mid, tr("UPLOAD_THUMBNAIL", lang), [[(tr("BACK", lang), "gfd:back")]])
        if data == "gfd:back":
            return await show_gofile_folder(chat, uid, mid)
        if data == "gfd:go":
            return await _gofile_download_all(chat, uid, mid)
        return
    if data == "m:gfup":                             # send any file -> get a gofile link (granted users)
        if not users.can_gofile_upload(uid):
            return
        sess(uid)["step"] = "await_gofile_file"
        return await edit(chat, mid, "☁️ " + tr("GOFILE_SEND_FILE", lang), [[(tr("MENU", lang), "m:main")]])
    if data == "m:keys":                             # manifest + supplied content keys (granted users)
        if not users.can_keys_download(uid):
            return
        s = sess(uid)
        s.pop("keys_url", None)
        s["step"] = "await_keys_dl"
        return await edit(chat, mid, "🔑 " + tr("KEYS_SEND_PROMPT", lang), [[(tr("MENU", lang), "m:main")]])
    if data == "m:dl":
        sess(uid)["subs_mode"] = False
        return await picker(chat, uid, mid, "recent", 0)
    if data == "m:search":
        sess(uid)["subs_mode"] = False
        return await picker(chat, uid, mid, "recent", 0, search=True)
    if data == "m:subs":                             # subtitle-only: search → pick → pull clear subs
        sess(uid)["subs_mode"] = True
        return await picker(chat, uid, mid, "recent", 0, search=True)
    if data.startswith("qcat:"):                 # search-mode category tabs
        _, c, p = data.split(":", 2)
        return await picker(chat, uid, mid, c, int(p), search=True)
    if data.startswith("srch:"):
        tag = data.split(":", 1)[1]
        s = sess(uid)
        # A subscription service with no connected account: don't start a search. The query
        # would just fail at authenticate, and repeated blind logins risk the account/IP being
        # blocked. Send the user to connect the account first. Optional-auth services (catch-all/
        # free) are not gated - search/list works anonymously.
        if svc_auth_required(tag) and not auth.list_accounts(uid, tag):
            return await account_service(chat, uid, mid, tag)
        s["search_service"] = tag
        s["step"] = "await_search"
        back = "m:subs" if s.get("subs_mode") else "m:search"
        prompt = (tr("TYPE_SERIES_MOVIE_NAME", lang) if s.get("subs_mode")
                  else tr("TYPE_WHAT_TO_SEARCH", lang))
        return await edit(chat, mid, f"{prompt} <b>{tag}</b>:", [[(tr("BACK", lang), back)]])
    if data.startswith("sr:"):
        s = sess(uid)
        results = s.get("search_results") or []
        idx = int(data.split(":", 1)[1])
        if idx >= len(results):
            return await edit(chat, mid, tr("EXPIRED_SEARCH_AGAIN", lang), [[(tr("SEARCH", lang), "m:search")]])
        s["service"] = s.get("search_service")
        return await show_titles(chat, uid, results[idx]["id"])
    if data.startswith("sp:"):                    # search results: page nav
        sess(uid)["search_page"] = int(data.split(":", 1)[1])
        return await show_search_results(chat, uid, mid)
    if data.startswith("sf:"):                    # search results: filter by type label
        s = sess(uid)
        li = int(data.split(":", 1)[1])
        labels = _search_labels(s.get("search_results") or [])
        s["search_filter"] = None if li < 0 or li >= len(labels) else labels[li]
        s["search_page"] = 0
        return await show_search_results(chat, uid, mid)
    if data == "m:acc":
        return await accounts_menu(chat, uid, mid)
    if data == "m:cdm":
        return await cdm_menu(chat, uid, mid)
    if data == "m:dls":
        return await my_downloads(chat, uid, mid)
    if data == "m:mon":
        return await monitor_menu(chat, uid, mid)
    if data == "monadd" and users.can_monitor(uid):
        sess(uid)["step"] = "await_monitor_url"
        return await edit(chat, mid, tr("SEND_URL_OF_SERIES", lang),
                          [[(tr("BACK", lang), "m:mon")]])
    if data.startswith("mondel:") and users.can_monitor(uid):
        monitors.remove(data.split(":", 1)[1])
        return await monitor_menu(chat, uid, mid)
    if data.startswith("mon_ttog:") and users.can_monitor(uid):    # toggle a monitor track checkbox
        if sess(uid).get("mon_pending") is None:
            return await edit(chat, mid, tr("EXPIRED_START_AGAIN", lang), [[(tr("BACK", lang), "m:mon")]])
        k = data.split(":", 1)[1]
        sel = set(sess(uid)["mon_pending"].get("tracks") or ["video", "audio", "subs"])
        sel ^= {k}
        sess(uid)["mon_pending"]["tracks"] = list(sel)
        return await monitor_ask_tracks(chat, uid, mid)
    if data == "mon_tgo" and users.can_monitor(uid):
        p = sess(uid).get("mon_pending")
        if p is None:
            return await edit(chat, mid, tr("EXPIRED_START_AGAIN", lang), [[(tr("BACK", lang), "m:mon")]])
        sel = set(p.get("tracks") or [])
        if not sel:
            return await monitor_ask_tracks(chat, uid, mid)
        if "video" in sel:
            return await monitor_ask_sendas(chat, uid, mid)
        return await monitor_ask_interval(chat, uid, mid)
    if data.startswith("mon_sa:") and users.can_monitor(uid):
        if sess(uid).get("mon_pending") is None:
            return await edit(chat, mid, tr("EXPIRED_START_AGAIN", lang), [[(tr("BACK", lang), "m:mon")]])
        sess(uid)["mon_pending"]["send_as"] = data.split(":", 1)[1]
        return await monitor_ask_interval(chat, uid, mid)
    if data.startswith("mon_int:") and users.can_monitor(uid):
        if sess(uid).get("mon_pending") is None:
            return await edit(chat, mid, tr("EXPIRED_START_AGAIN", lang), [[(tr("BACK", lang), "m:mon")]])
        sess(uid)["mon_pending"]["interval"] = int(data.split(":", 1)[1])
        return await monitor_ask_cover(chat, uid, mid)
    if data == "mon_cover" and users.can_monitor(uid):
        if sess(uid).get("mon_pending") is None:
            return await edit(chat, mid, tr("EXPIRED_START_AGAIN", lang), [[(tr("BACK", lang), "m:mon")]])
        sess(uid)["step"] = "await_mon_cover"
        return await edit(chat, mid, tr("SEND_PHOTO_NOW_AS", lang),
                          [[(tr("SKIP", lang), "mon_nocover")]])
    if data == "mon_nocover" and users.can_monitor(uid):
        sess(uid)["step"] = None
        return await monitor_ask_start(chat, uid, mid)
    if data == "mon_intc" and users.can_monitor(uid):
        if sess(uid).get("mon_pending") is None:
            return await edit(chat, mid, tr("EXPIRED_START_AGAIN", lang), [[(tr("BACK", lang), "m:mon")]])
        sess(uid)["step"] = "await_mon_interval"
        return await edit(chat, mid, tr("HOW_OFTEN_TO_SCAN_2", lang),
                          [[(tr("CANCEL", lang), "m:mon")]])
    if data == "mon_sched" and users.can_monitor(uid):
        if sess(uid).get("mon_pending") is None:
            return await edit(chat, mid, tr("EXPIRED_START_AGAIN", lang), [[(tr("BACK", lang), "m:mon")]])
        sess(uid)["step"] = "await_mon_schedule"
        return await edit(chat, mid, tr("FIXED_TIME_EXAMPLES_22", lang),
                          [[(tr("CANCEL", lang), "m:mon")]])
    if data == "mon_start:new" and users.can_monitor(uid):
        p = sess(uid).get("mon_pending") or {}
        return await _save_monitor(chat, uid, mid, [e["key"] for e in p.get("eps", [])])
    if data == "mon_start:all" and users.can_monitor(uid):
        return await _save_monitor(chat, uid, mid, [])
    if data == "mon_start:pick" and users.can_monitor(uid):
        return await monitor_pick_start(chat, uid, mid, 0)
    if data.startswith("mon_pickp:") and users.can_monitor(uid):
        return await monitor_pick_start(chat, uid, mid, int(data.split(":", 1)[1]))
    if data.startswith("mon_pick:") and users.can_monitor(uid):
        p = sess(uid).get("mon_pending") or {}
        idx = int(data.split(":", 1)[1])
        seen = [e["key"] for e in p.get("eps", [])[:idx]]   # everything BEFORE the chosen episode
        return await _save_monitor(chat, uid, mid, seen)
    # --- monitor detail + edit ---
    if data.startswith("mon_eiv:") and users.can_monitor(uid):
        return await monitor_edit_interval(chat, uid, mid, data.split(":", 1)[1])
    if data.startswith("mon_setiv:") and users.can_monitor(uid):
        _, mon_id, sec = data.split(":", 2)
        monitors.set_interval(mon_id, int(sec))
        _mon_last.pop(mon_id, None)                          # re-check soon with the new interval
        return await monitor_detail(chat, uid, mid, mon_id)
    if data.startswith("mon_eivc:") and users.can_monitor(uid):
        mon_id = data.split(":", 1)[1]
        sess(uid).update(step="await_mon_edit_iv", edit_mon=mon_id)
        return await edit(chat, mid, tr("HOW_OFTEN_TO_SCAN_3", lang),
                          [[(tr("BACK", lang), f"mon:{mon_id}")]])
    if data.startswith("mon_eschedc:") and users.can_monitor(uid):
        mon_id = data.split(":", 1)[1]
        sess(uid).update(step="await_mon_edit_sched", edit_mon=mon_id)
        return await edit(chat, mid, tr("FIXED_TIME_EXAMPLES_22_2", lang), [[(tr("BACK", lang), f"mon:{mon_id}")]])
    if data.startswith("mon_emode:") and users.can_monitor(uid):
        return await monitor_edit_mode(chat, uid, mid, data.split(":", 1)[1])
    if data.startswith("mon_etog:") and users.can_monitor(uid):     # toggle a track on an existing monitor
        _, mon_id, k = data.split(":", 2)
        m = monitors.get(mon_id) or {}
        sel = to_sel(m.get("params", {}).get("tracks") or m.get("params", {}).get("mode"))
        sel ^= {k}
        if sel:                                                     # keep at least one track selected
            monitors.set_param(mon_id, "tracks", list(sel))
        return await monitor_edit_mode(chat, uid, mid, mon_id)
    if data.startswith("mon_esa:") and users.can_monitor(uid):
        return await monitor_edit_sendas(chat, uid, mid, data.split(":", 1)[1])
    if data.startswith("mon_setsa:") and users.can_monitor(uid):
        _, mon_id, sa = data.split(":", 2)
        monitors.set_param(mon_id, "send_as", sa)
        return await monitor_detail(chat, uid, mid, mon_id)
    if data.startswith("mon_ecov:") and users.can_monitor(uid):
        return await monitor_edit_cover(chat, uid, mid, data.split(":", 1)[1])
    if data.startswith("mon_setcov:") and users.can_monitor(uid):
        mon_id = data.split(":", 1)[1]
        sess(uid).update(step="await_mon_edit_cover", edit_mon=mon_id)
        return await edit(chat, mid, tr("SEND_PHOTO_NOW_AS", lang),
                          [[(tr("BACK", lang), f"mon:{mon_id}")]])
    if data.startswith("mon_rmcov:") and users.can_monitor(uid):
        mon_id = data.split(":", 1)[1]
        monitors.set_param(mon_id, "cover", None)
        return await monitor_detail(chat, uid, mid, mon_id)
    if data.startswith("mon:") and users.can_monitor(uid):
        return await monitor_detail(chat, uid, mid, data[4:])
    if data == "cdmadd":
        sess(uid)["step"] = "await_wvd"
        return await edit(chat, mid, tr("SEND_CDM_FILE_NOW", lang),
                          [[(tr("BACK", lang), "m:cdm")]])
    if data.startswith("cdmdel:"):
        auth.remove_wvd(uid, data.split(":", 1)[1])
        return await cdm_menu(chat, uid, mid)
    if data.startswith("cdm:"):                    # chose a CDM for a DRM download
        choice = data.split(":", 1)[1]
        s = sess(uid)
        s["cdm"] = "" if choice == "_default" else (auth.wvd_device(uid, choice) or "")
        return await start_download(chat, uid, mid, s.get("dl_profile", str(uid)))
    if data == "svc:refresh" and users.is_admin(uid):
        await state.refresh()
        return await services_grid(chat, uid, mid, "il", 0)
    if data == "m:svc" and users.is_admin(uid):
        return await services_grid(chat, uid, mid, "il", 0)
    # --- live recording (admin only) ---
    if data == "m:rec" and users.is_admin(uid):
        return await recordings.menu(chat, uid, mid)
    if data.startswith("rec:") and users.is_admin(uid):
        rest = data.split(":", 1)[1]
        if rest == "nokey":                          # finish add-channel wizard with no CENC key
            new = sess(uid).get("rec_new") or {}
            if new.get("name") and new.get("url"):
                recordings.put(new["name"], new["url"], "")
                sess(uid)["step"] = None
                sess(uid).pop("rec_new", None)
                await call("answerCallbackQuery", callback_query_id=cq["id"],
                           text=tr("REC_SAVED", lang).format(name=new["name"]))
            return await recordings.menu(chat, uid, mid)
        if rest == "pause":                          # skip live content until resumed
            recordings.pause(uid)
            return await call("answerCallbackQuery", callback_query_id=cq["id"], text="⏸️")
        if rest == "resume":
            recordings.resume(uid)
            return await call("answerCallbackQuery", callback_query_id=cq["id"], text="▶️")
        if rest == "stop":                           # end a running recording early (keep partial)
            recordings.stop(uid)
            return await call("answerCallbackQuery", callback_query_id=cq["id"],
                              text=tr("REC_STOPPING", lang))
        if rest == "add":
            sess(uid).update(step="await_rec_name", rec_new={})
            return await edit(chat, mid, tr("REC_ADD_NAME", lang), [[(tr("REC_BACK", lang), "m:rec")]])
        if rest.startswith("ch:"):
            return await recordings.channel(chat, uid, mid, rest[3:])
        if rest.startswith("go:"):
            return await recordings.ask_duration(chat, uid, mid, rest[3:])
        if rest.startswith("del:"):
            name = rest[4:]
            recordings.delete(name)
            await call("answerCallbackQuery", callback_query_id=cq["id"],
                       text=tr("REC_DELETED", lang).format(name=name))
            return await recordings.menu(chat, uid, mid)
        if rest.startswith("edit:"):                 # show current details + pick a field to edit
            return await recordings.channel_edit(chat, uid, mid, rest[5:])
        if rest.startswith("eurl:"):
            name = rest[5:]
            sess(uid).update(step="await_rec_edit_url", edit_ch=name)
            return await edit(chat, mid, tr("REC_NEW_URL", lang), [[(tr("REC_BACK", lang), f"rec:edit:{name}")]])
        if rest.startswith("ekeyclear:"):            # clear the key (no encryption)
            name = rest[10:]
            recordings.update_field(name, "key", "")
            sess(uid)["step"] = None
            await call("answerCallbackQuery", callback_query_id=cq["id"], text=tr("REC_UPDATED", lang))
            return await recordings.channel_edit(chat, uid, mid, name)
        if rest.startswith("ekey:"):
            name = rest[5:]
            sess(uid).update(step="await_rec_edit_key", edit_ch=name)
            return await edit(chat, mid, tr("REC_NEW_KEY", lang),
                              [[(tr("REC_SKIP_KEY", lang), f"rec:ekeyclear:{name}")],
                               [(tr("REC_BACK", lang), f"rec:edit:{name}")]])
        if rest.startswith("dur:"):
            _, name, secs = rest.split(":", 2)
            recordings.start(chat, uid, mid, name, int(secs))
            return
    if data.startswith("svcg:") and users.is_admin(uid):
        _, cat, page = data.split(":")
        return await services_grid(chat, uid, mid, cat, int(page))
    # --- users panel (admin only) ---
    if data.startswith(("m:users", "u:", "upm:", "upmode:", "ups:", "upf:", "upc:", "ucc:",
                        "ust:", "uad:", "urm:", "urmc:")) and users.is_admin(uid):
        return await admin.on_users_callback(chat, uid, mid, data)
    # --- broadcast (admin only) ---
    if data == "bc:cancel" and users.is_admin(uid):
        sess(uid).pop("bcast", None)
        return await edit(chat, mid, tr("CANCELLED", lang), [[(tr("MENU", lang), "m:main")]])
    if data.startswith("bc:") and users.is_admin(uid):
        return await admin.do_broadcast(chat, uid, mid, data.split(":", 1)[1])
    if data.startswith("scat:") and users.is_admin(uid):
        _, tag, cat = data.split(":")
        set_cat_override(tag, cat)
        return await service_detail(chat, uid, mid, tag)
    if data.startswith("dcook:") and users.is_admin(uid):     # set shared/default cookies for a service
        svc = data.split(":", 1)[1]
        sess(uid).update(step="await_default_cookies", acc_service=svc)
        return await edit(chat, mid, tr("SEND_DEFAULT_COOKIES", lang).format(svc=svc),
                          [[(tr("BACK", lang), f"svc:{svc}")]])
    if data.startswith("si:"):
        return await service_detail(chat, uid, mid, data.split(":", 1)[1])
    if data.startswith("cat:"):
        _, cat, page = data.split(":")
        return await picker(chat, uid, mid, cat, int(page))
    if data.startswith("svc:"):
        return await ask_input(chat, uid, mid, data.split(":", 1)[1])
    if data.startswith("se:"):
        _, season, page = data.split(":")
        return await show_episodes(chat, uid, mid, int(season), int(page))
    if data.startswith("ep:"):
        _, season, number = data.split(":")
        return await show_tracks(chat, uid, mid, wanted=f"S{int(season):02d}E{int(number):02d}")
    if data.startswith("epall:"):
        season = int(data.split(":")[1])
        return await show_tracks(chat, uid, mid, wanted=f"S{season:02d}")
    if data == "epallseries":                      # all seasons / whole series
        return await show_tracks(chat, uid, mid, wanted=None)
    if data.startswith("tt_tog:"):                          # toggle a track checkbox
        k = data.split(":", 1)[1]
        sel = set(sess(uid).get("tsel") or [])
        sel ^= {k}
        sess(uid)["tsel"] = list(sel)
        return await show_track_types(chat, uid, mid)
    if data == "tt:back":
        return await show_track_types(chat, uid, mid)
    if data == "tt_go":                                     # continue with the checked tracks
        sel = set(sess(uid).get("tsel") or [])
        if not sel:
            return await show_track_types(chat, uid, mid)   # nothing checked → re-show
        sess(uid)["send_as"] = None
        if "video" in sel:                                  # video → choose quality, then send-as
            return await show_quality(chat, uid, mid)
        if sel == {"subs"}:                                 # subtitles only → maybe pick a language
            sess(uid)["s_lang"] = None
            if len(sess(uid).get("sub_langs") or []) > 1:
                return await show_sub_langs(chat, uid, mid)
        return await pick_account_or_go(chat, uid, mid, "best")   # no video → no resolution
    if data == "slother":
        sess(uid).update(step="await_sublang", mode="subs")
        return await edit(chat, mid, tr("TYPE_LANGUAGE_CODE_TO", lang), [[(tr("BACK", lang), "tt:back")]])
    if data.startswith("sl:"):
        choice = data.split(":", 1)[1]
        sess(uid)["s_lang"] = None if choice == "all" else [choice]
        sess(uid)["sub_extra_lang"] = None
        return await pick_account_or_go(chat, uid, mid, "best")
    if data.startswith("q:"):
        sess(uid)["quality"] = data.split(":", 1)[1]
        if "video" in to_sel(sess(uid).get("tsel")):         # video selected → 🎬 video vs 📄 file
            return await show_send_as(chat, uid, mid)
        return await pick_account_or_go(chat, uid, mid, sess(uid)["quality"])
    if data.startswith("sa:"):
        sess(uid)["send_as"] = data.split(":", 1)[1]         # "video" | "file"
        return await show_dl_cover(chat, uid, mid)
    if data == "dlcov:auto":
        sess(uid)["cover"] = None
        return await pick_account_or_go(chat, uid, mid, sess(uid).get("quality", "best"))
    if data == "dlcov:up":
        sess(uid)["step"] = "await_dl_cover"
        return await edit(chat, mid, tr("SEND_PHOTO_NOW_AS_2", lang),
                          [[(tr("AUTOMATIC_INSTEAD", lang), "dlcov:auto")]])
    if data.startswith("ac:"):
        return await accounts_menu(chat, uid, mid, int(data.split(":", 1)[1]))
    if data.startswith("use:"):
        return await _after_account(chat, uid, mid, data.split(":", 1)[1])
    if data.startswith("cxl:"):                      # cancel pressed → confirm before stopping
        job_id = data.split(":", 1)[1]
        if job_id not in active_jobs.get(uid, {}):   # already done/cancelled
            return await edit(chat, mid, tr("CANCELLED", lang), [[(tr("MENU", lang), "m:main")]])
        return await edit(chat, mid, tr("CONFIRM_CANCEL_Q", lang),
                          [[(tr("YES_CANCEL", lang), f"cancel:{job_id}")],
                           [(tr("NO_CONTINUE", lang), f"cxlno:{job_id}")]])
    if data.startswith("cxlno:"):                    # backed out of the confirm → restore progress
        return await redraw_progress(chat, uid, mid, data.split(":", 1)[1])
    if data.startswith("cancel:"):
        job_id = data.split(":", 1)[1]
        if job_id in active_jobs.get(uid, {}) and not job_id.startswith("\x00"):   # caller's own real job
            active_jobs[uid][job_id]["cancelled"] = True   # stop its poll loop (no delivery/retry)
            try:
                await engine.cancel(job_id)
            except Exception:
                pass
        return await edit(chat, mid, tr("CANCELLED", lang), [[(tr("MENU", lang), "m:main")]])
    if data == "retry":                              # one-tap re-run of the last failed download
        spec = retry_spec.get(uid)
        if not spec:                                 # lost (e.g. bot restarted) → send them back to the menu
            return await main_menu(chat, uid, mid)
        limit = users.concurrency_limit(uid)         # same friendly pre-check as start_download, so the
        if len(active_jobs.get(uid, ())) >= limit:   # message never hangs on a silently-gated launch
            return await edit(chat, mid, "⏳ " + tr("YOU_RE_ALREADY_DOWNLOADING", lang).format(limit=limit),
                              [[(tr("MY_DOWNLOADS", lang), "m:dls")], [(tr("MENU", lang), "m:main")]])
        await edit(chat, mid, "⏳ " + tr("STARTING_DOWNLOAD", lang))
        return await launch_download(chat, uid, mid, **spec)
    if data.startswith("as:"):
        return await account_service(chat, uid, mid, data.split(":", 1)[1])
    if data.startswith("aaddc:"):
        svc = data.split(":", 1)[1]
        sess(uid).update(step="await_creds", acc_service=svc)
        return await edit(chat, mid, tr("SEND_USERNAME_AND_PASSWORD", lang).format(svc=svc),
                          [[(tr("BACK", lang), f"as:{svc}")]])
    if data.startswith("aadd:"):
        svc = data.split(":", 1)[1]
        sess(uid).update(step="await_cookies", acc_service=svc)
        return await edit(chat, mid, tr("SEND_THE_COOKIES_TXT", lang).format(svc=svc), [[(tr("BACK", lang), f"as:{svc}")]])
    if data.startswith("adel:"):
        _, svc, profile = data.split(":")
        auth.remove_account(uid, svc, profile)
        return await account_service(chat, uid, mid, svc)


_KEY_PAIR_RE = re.compile(r"([0-9a-fA-F]{32})\s*:\s*([0-9a-fA-F]{32})")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def _parse_manual_titles(text: str) -> list[dict]:
    """Parse a pasted 'manifest + keys' blob into a list of titles. Supports both the simple
    one-title form (URL[s] + keys) and the multi-section form where each feature has a 'Name:'
    header followed by its own manifest URL(s) and key line (the Amazon bonus-features format).
    Each title = {name, urls: [str], keys: {kid: key}}. A title may have several manifests
    (e.g. separate video and audio DASH). Only titles that have at least one URL are returned."""
    titles: list[dict] = []
    cur: dict | None = None
    doc_name = ""
    for raw in (text or "").splitlines():
        line = raw.strip().strip("`")
        if not line:
            continue
        is_header = line.endswith(":") and not line.lower().startswith("http") and not _KEY_PAIR_RE.search(line)
        if is_header:
            cur = {"name": re.sub(r"[._]+", " ", line[:-1]).strip()[:120], "urls": [], "keys": {}}
            titles.append(cur)
            continue
        urls = _URL_RE.findall(line)
        pairs = _KEY_PAIR_RE.findall(line)
        if urls or pairs:
            if cur is None:
                cur = {"name": "", "urls": [], "keys": {}}
                titles.append(cur)
            cur["urls"].extend(urls)
            for kid, key in pairs:
                cur["keys"][kid.lower()] = key.lower()
        elif cur is None and not doc_name:        # a plain leading line = the document/title name
            doc_name = line[:120]
    for t in titles:
        if not t["name"]:
            t["name"] = doc_name or "Download"
    return [t for t in titles if t["urls"]]


async def _keys_download(chat: int, uid: int, titles: list[dict]):
    """Build a JSON catalog (export-v2) from titles of {name, urls, keys} and run it through the
    JSON service - downloads + decrypts from the supplied keys, no license server. A title with
    several manifest URLs (e.g. split video/audio) is parsed and merged by the JSON service."""
    lang = users.lang(uid)
    m = await send(chat, "🔑 " + tr("KEYS_PREPARING", lang))
    mid = m["result"]["message_id"]
    try:
        export = {"version": 2, "service": "JSON", "region": "IL", "titles": {}}
        for i, t in enumerate(titles):
            urls = t["urls"]
            mtype = "HLS" if any(".m3u8" in u.lower() for u in urls) else "DASH"
            export["titles"][str(i)] = {
                "meta": {"type": "movie", "name": t["name"] or "Download", "language": "he"},
                "manifest_urls": urls,
                "manifest_type": mtype,
                "tracks": {"1": {"keys": t["keys"]}},
            }
        if not export["titles"]:
            return await edit(chat, mid, tr("KEYS_BAD_INPUT", lang), [[(tr("MENU", lang), "m:main")]])
        cat_dir = config.STATE_DIR / "catalogs"
        cat_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        cat_path = cat_dir / f"{uid}_{ts}.json"
        cat_path.write_text(json.dumps(export, ensure_ascii=False), encoding="utf-8")
        sess(uid)["service"] = "JSON"
        await edit(chat, mid, tr("CATALOG_LOADED_TITLES_LOADING", lang).format(n=len(export["titles"])))
        return await show_titles(chat, uid, str(cat_path))
    except Exception as e:
        await report_error("keys download", e, uid)
        return await edit(chat, mid, tr("KEYS_BAD_INPUT", lang), [[(tr("MENU", lang), "m:main")]])


async def _gofile_resolve(chat: int, uid: int, url: str):
    """Resolve a gofile folder in a headless browser and show its files + options."""
    lang = users.lang(uid)
    m = await send(chat, "☁️ " + tr("GOFILE_RESOLVING", lang))
    mid = m["result"]["message_id"]
    try:
        info = await gofile.resolve(url)
    except Exception as e:
        await report_error("gofile resolve", e, uid)
        return await edit(chat, mid, "🔴 " + tr("GOFILE_RESOLVE_FAILED", lang).format(err=html.escape(str(e))),
                          [[(tr("MENU", lang), "m:main")]])
    if not info.get("files"):
        return await edit(chat, mid, tr("GOFILE_EMPTY", lang), [[(tr("MENU", lang), "m:main")]])
    sess(uid)["gfd"] = {"url": url, "folder": info["folder"], "token": info.get("token") or "",
                        "files": info["files"], "subfolders": info.get("subfolders", 0),
                        "send_as": "video", "cover": None}
    return await show_gofile_folder(chat, uid, mid)


async def _gofile_download_all(chat: int, uid: int, mid: int):
    """Download every file in the resolved gofile folder and deliver each to Telegram, applying
    the chosen send-as (video/file) and thumbnail to videos. Big files that exceed the Telegram
    cap fall back to the original public gofile link."""
    lang = users.lang(uid)
    gf = sess(uid).get("gfd") or {}
    files, token = gf.get("files") or [], gf.get("token") or ""
    if not files:
        return await edit(chat, mid, tr("GOFILE_EMPTY", lang), [[(tr("MENU", lang), "m:main")]])
    up_dir = config.STATE_DIR / "gfdl" / str(uid)
    up_dir.mkdir(parents=True, exist_ok=True)
    total, sent, errs = len(files), 0, []
    for idx, f in enumerate(files, 1):
        up_dir.mkdir(parents=True, exist_ok=True)    # deliver() rmdir's it once empty - recreate per file
        name = f["name"]
        safe = re.sub(r'[\\/:*?"<>|]', "", name).strip() or f"file_{idx}"
        dest = str(up_dir / safe)
        head = f"☁️ {html.escape(gf.get('folder') or 'gofile')}\n⬇️ {idx}/{total} <code>{html.escape(name)}</code>"
        await edit(chat, mid, head)
        st = {"p": -10, "t": 0.0}

        async def _prog(done, tot, _h=head, _phase="⬇️"):
            pct = int(done * 100 / tot) if tot else 0
            now = time.time()
            if pct >= 100 or (pct - st["p"] >= 5 and now - st["t"] >= 2.0):
                st["p"], st["t"] = pct, now
                try:
                    await edit(chat, mid, f"{_h}\n{_phase} {pct}%")
                except Exception:
                    pass

        try:
            await gofile.fetch_file(f["link"], token, dest, progress=_prog)
        except Exception as e:
            errs.append(f"{name} ({tr('GOFILE_DL_ERR', lang)})")
            print(f"gofile fetch failed ({name}): {e}")
            continue
        force = ("video" if gf.get("send_as", "video") == "video" else "file") if f.get("is_video") else "file"
        st["p"], st["t"] = -10, 0.0

        async def _uprog(done, tot, _h=head):
            await _prog(done, tot, _h, "⬆️")

        try:
            await uploader.deliver(chat, dest, service="gofile", source_url=gf.get("url") or "",
                                   lang=lang, display_title=os.path.splitext(name)[0],
                                   force_kind=force, cover_path=gf.get("cover"), progress=_uprog)
            sent += 1
        except Exception as e:
            errs.append(f"{name} ({tr('GOFILE_TOO_BIG', lang)})")
            print(f"gofile deliver failed ({name}): {e}")
        finally:
            try:
                os.remove(dest)
            except OSError:
                pass
    try:
        if up_dir.is_dir() and not any(up_dir.iterdir()):
            up_dir.rmdir()
    except OSError:
        pass
    msg = "🎉 " + tr("GOFILE_DL_DONE", lang).format(sent=sent, total=total)
    if errs:
        msg += "\n\n⚠️ " + tr("GOFILE_DL_SOME_FAILED", lang) + "\n" + "\n".join(f"• {html.escape(e)}" for e in errs[:10])
        msg += "\n\n🔗 " + (gf.get("url") or "")
    await edit(chat, mid, msg, [[(tr("MENU", lang), "m:main")]])


async def on_message(msg: dict):
    uid = msg["from"]["id"]
    chat = msg["chat"]["id"]
    s = sess(uid)

    users.see(msg.get("from") or {})        # record EVERY contact (for broadcast), then gate
    users.touch(msg.get("from") or {})
    if not users.is_allowed(uid):            # unknown/suspended → silently ignore
        return
    lang = users.lang(uid)

    # admin: reply to ANY message with a broadcast trigger → choose audience, copy to all
    if users.is_admin(uid) and msg.get("reply_to_message") and \
            (msg.get("text") or "").strip().lower() in BCAST_TRIGGERS:
        s["bcast"] = {"from_chat": chat, "mid": msg["reply_to_message"]["message_id"]}
        return await send(chat, tr("BROADCAST_MESSAGE_BROADCAST_TO", lang),
                          [[(tr("TO_ALL_APPROVED", lang), "bc:active")],
                           [(tr("TO_EVERYONE_WHO_EVER", lang), "bc:all")],
                           [(tr("CANCEL", lang), "bc:cancel")]])

    # admin adding a new user: by id / @username (text) or by forwarding a message from them
    if s.get("step") == "await_adduser":
        fwd = _forward_user(msg)
        text = (msg.get("text") or "").strip()
        if not fwd and _is_forward(msg):          # forwarded, but the sender hid their account
            s["step"] = None
            return await send(chat, tr("THE_USER_HID_THEIR", lang))
        if not (fwd or text):                     # not valid input → keep waiting
            return
        s["step"] = None
        if not users.is_admin(uid):
            return
        try:
            u = users.add(str(fwd["id"]) if fwd else text, by=uid, ts=int(time.time()))
            if fwd:
                users.touch(fwd)                  # capture the forwarded user's name/username
            await send(chat, tr("ADDED", lang).format(name=html.escape(users.label(u)))
                       + ("" if u.get("id") is not None else tr("WILL_BE_ACTIVATED_AUTOMATICALLY", lang)))
            m = await send(chat, "⏳...")
            return await admin.user_detail(chat, uid, m["result"]["message_id"], users.key(u))
        except ValueError as e:
            return await send(chat, f"🔴 {html.escape(str(e))}")

    # live-recording: add/edit channel wizard (admin only): name -> url -> key
    if s.get("step") in ("await_rec_name", "await_rec_url", "await_rec_key") and users.is_admin(uid):
        text = (msg.get("text") or "").strip()
        if not text:
            return
        new = s.setdefault("rec_new", {})
        if s["step"] == "await_rec_name":
            new["name"] = text
            s["step"] = "await_rec_url"
            return await send(chat, tr("REC_ADD_URL", lang))
        if s["step"] == "await_rec_url":
            new["url"] = text
            s["step"] = "await_rec_key"
            return await send(chat, tr("REC_ADD_KEY", lang), [[(tr("REC_SKIP_KEY", lang), "rec:nokey")]])
        # await_rec_key
        key = "" if text == "-" else text
        recordings.put(new["name"], new["url"], key)
        s["step"] = None
        s.pop("rec_new", None)
        await send(chat, tr("REC_SAVED", lang).format(name=html.escape(new["name"])))
        m = await send(chat, "⏳...")
        return await recordings.menu(chat, uid, m["result"]["message_id"])

    # live-recording: edit a single field (url / key) of an existing channel
    if s.get("step") in ("await_rec_edit_url", "await_rec_edit_key") and users.is_admin(uid):
        text = (msg.get("text") or "").strip()
        if not text:
            return
        name = s.get("edit_ch")
        field = "url" if s["step"] == "await_rec_edit_url" else "key"
        recordings.update_field(name, field, "" if text == "-" else text)
        s["step"] = None
        s.pop("edit_ch", None)
        await send(chat, tr("REC_UPDATED", lang))
        m = await send(chat, "⏳...")
        return await recordings.channel_edit(chat, uid, m["result"]["message_id"], name)

    # cookies upload
    if s.get("step") == "await_gofile_file" and any(k in msg for k in ("document", "video", "audio")):
        s["step"] = None
        if not users.can_gofile_upload(uid):         # gate matches the menu entry
            return
        obj = msg.get("document") or msg.get("video") or msg.get("audio")
        fname = obj.get("file_name") or f"file_{obj['file_id'][:10]}"
        fname = re.sub(r'[\\/:*?"<>|]', "", fname).strip() or "file"
        up_dir = config.STATE_DIR / "gfup"
        up_dir.mkdir(parents=True, exist_ok=True)
        dest = str(up_dir / fname)
        m = await send(chat, "☁️ " + tr("UPLOADING_GOFILE", lang))
        mid = m["result"]["message_id"]
        try:
            await download_file_stream(obj["file_id"], dest)
            url = await gofile.upload(dest)
            await edit(chat, mid, "✅ " + tr("GOFILE_READY", lang) + f"\n{url}",
                       [[(tr("MENU", lang), "m:main")]])
        except Exception as e:
            await edit(chat, mid, "🔴 " + tr("UPLOAD_FAILED_GOFILE", lang).format(err=html.escape(str(e))),
                       [[(tr("MENU", lang), "m:main")]])
        finally:
            try:
                os.remove(dest)
            except OSError:
                pass
        return

    if "document" in msg and s.get("step") in ("await_cookies", "await_wvd", "await_default_cookies"):
        step = s["step"]
        f = await call("getFile", file_id=msg["document"]["file_id"])
        path = f["result"]["file_path"]
        async with aiohttp.ClientSession() as cs:
            async with cs.get(f"{FILE_API}/{path}") as r:
                blob = await r.read()                # bytes (wvd is binary; cookies decode below)
        s["step"] = None
        try:
            if step == "await_default_cookies" and users.is_admin(uid):
                svc = s.get("acc_service")
                auth.set_default_cookies(svc, blob.decode("utf-8", "replace"))
                await send(chat, tr("DEFAULT_COOKIES_SAVED", lang).format(svc=svc))
                m = await send(chat, "⏳...")
                return await service_detail(chat, uid, m["result"]["message_id"], svc)
            if step == "await_wvd":
                fname = (msg["document"].get("file_name") or "").lower()
                ext = ".prd" if fname.endswith(".prd") else ".wvd"
                acct = auth.add_wvd(uid, blob, ext=ext)
                await send(chat, tr("SAVED_CDM_FILE", lang).format(name=html.escape(acct['label'])))
                m = await send(chat, "⏳...")
                return await cdm_menu(chat, uid, m["result"]["message_id"])
            acct = auth.add_cookies(uid, s["acc_service"], blob.decode("utf-8", "replace"))
            await send(chat, tr("SAVED_ACCOUNT_FOR", lang).format(name=html.escape(acct['label']), svc=s['acc_service']))
        except ValueError as e:
            await send(chat, f"🔴 {html.escape(str(e))}")
        return

    # JSON catalog upload → list its titles like any other source (downloads via the JSON service,
    # decrypting from the keys embedded in the catalog - no CDM/login needed).
    if "document" in msg and (msg["document"].get("file_name") or "").lower().endswith(".json"):
        from . import catalog as _catalog
        m = await send(chat, tr("READING_THE_CATALOG", lang))
        mid = m["result"]["message_id"]
        try:
            cat_dir = config.STATE_DIR / "catalogs"
            cat_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            for old in cat_dir.glob("*.json"):       # reap stale catalogs (>24h) so they can't pile up
                try:
                    if ts - old.stat().st_mtime > 86400:
                        old.unlink(missing_ok=True)
                except OSError:
                    pass
            raw_path = cat_dir / f"{uid}_{ts}_raw.json"
            await download_file(msg["document"]["file_id"], str(raw_path))
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            export = _catalog.normalize_catalog(raw)
            n = len(export.get("titles", {}))
            if not n:
                return await edit(chat, mid, tr("NO_TITLES_DETECTED_IN", lang))
            cat_path = cat_dir / f"{uid}_{ts}.json"
            cat_path.write_text(json.dumps(export, ensure_ascii=False), encoding="utf-8")
            raw_path.unlink(missing_ok=True)
            s["service"] = "JSON"
            await edit(chat, mid, tr("CATALOG_LOADED_TITLES_LOADING", lang).format(n=n))
            return await show_titles(chat, uid, str(cat_path))
        except Exception as e:
            await report_error("catalog upload", e, uid)   # full detail to admin, not to the user
            return await edit(chat, mid, tr("ERROR_READING_THE_CATALOG", lang))

    text = (msg.get("text") or "").strip()
    if text == "/start" or text == "/menu":
        s.clear()
        return await main_menu(chat, uid)
    # manifest + supplied content keys: accept the URL and keys together, or the URL then the keys
    if s.get("step") in ("await_keys_dl", "await_keys_only") and users.can_keys_download(uid):
        if not text:
            return
        if s.get("step") == "await_keys_only":       # staged: we stored the URL(s); this msg = keys
            keys = {kid.lower(): key.lower() for kid, key in _KEY_PAIR_RE.findall(text)}
            if not keys:
                return await send(chat, "🔴 " + tr("KEYS_BAD_INPUT", lang))
            titles = [{"name": s.get("keys_name") or "Download", "urls": s.get("keys_urls", []), "keys": keys}]
            s["step"] = None
            s.pop("keys_urls", None)
            s.pop("keys_name", None)
            return await _keys_download(chat, uid, titles)
        titles = _parse_manual_titles(text)         # one paste: URL(s) + keys, or multi-section
        complete = [t for t in titles if t["urls"] and t["keys"]]
        if complete:
            s["step"] = None
            return await _keys_download(chat, uid, complete)
        if len(titles) == 1 and titles[0]["urls"]:  # got the manifest, no keys yet -> ask for them
            s.update(keys_urls=titles[0]["urls"], keys_name=titles[0]["name"], step="await_keys_only")
            return await send(chat, "🔑 " + tr("KEYS_NOW_SEND_KEYS", lang))
        return await send(chat, "🔴 " + tr("KEYS_BAD_INPUT", lang))
    # Pasted a URL
    if text.startswith("http"):
        text = unwrap_url(text)                      # unwrap Branch/app.link smart-links
        if s.get("step") == "await_monitor_url":     # setting up an auto-monitor
            s["step"] = None
            return await monitor_setup(chat, uid, text)
        if s.get("step") == "await_input":           # an explicit service choice wins
            s["step"] = None
            return await show_titles(chat, uid, text)
        if "gofile.io/" in text.lower():             # gofile folder -> our Playwright resolver
            s["step"] = None
            return await _gofile_resolve(chat, uid, text)
        await state.services()                        # ensure regex catalog is loaded
        svc = detect_service(text)                    # known domain, else the catch-all service
        if not svc:
            return await send(chat, tr("COULD_NOT_DETECT_SERVICE_2", lang))
        s["service"] = svc
        s["step"] = None
        return await show_titles(chat, uid, text)
    if s.get("step") == "await_mon_interval" and text:   # typed custom monitor interval (minutes)
        s["step"] = None
        if s.get("mon_pending") is None:
            return await send(chat, tr("EXPIRED_START_AGAIN_FROM", lang))
        lo, hi = _parse_interval(text)
        if not lo:
            s["step"] = "await_mon_interval"
            return await send(chat, tr("COULD_NOT_PARSE_THAT", lang))
        s["mon_pending"]["interval"] = lo
        s["mon_pending"]["interval_max"] = hi
        m = await send(chat, "⏳...")
        return await monitor_ask_cover(chat, uid, m["result"]["message_id"])
    if s.get("step") == "await_gf_cover" and msg.get("photo"):    # custom thumbnail for a gofile download
        s["step"] = None
        sizes = msg["photo"]
        photo = next((p for p in reversed(sizes) if p.get("width", 9999) <= 400), sizes[0])
        cov_dir = os.path.join(config.STATE_DIR, "covers")
        os.makedirs(cov_dir, exist_ok=True)
        cov_path = os.path.join(cov_dir, f"gf_{uid}_{int(time.time())}.jpg")
        gf = s.get("gfd") or {}
        try:
            await download_file(photo["file_id"], cov_path)
            gf["cover"] = cov_path
        except Exception:
            gf["cover"] = None
        s["gfd"] = gf
        m = await send(chat, tr("PHOTO_SAVED", lang) if gf.get("cover") else tr("COULD_NOT_SAVE_IT", lang))
        return await show_gofile_folder(chat, uid, m["result"]["message_id"])
    if s.get("step") == "await_dl_cover" and msg.get("photo"):    # custom thumbnail for this download
        s["step"] = None
        sizes = msg["photo"]
        photo = next((p for p in reversed(sizes) if p.get("width", 9999) <= 400), sizes[0])
        cov_dir = os.path.join(config.STATE_DIR, "covers")
        os.makedirs(cov_dir, exist_ok=True)
        cov_path = os.path.join(cov_dir, f"dl_{uid}_{int(time.time())}.jpg")
        try:
            await download_file(photo["file_id"], cov_path)
            s["cover"] = cov_path
        except Exception:
            s["cover"] = None
        m = await send(chat, tr("PHOTO_SAVED", lang) if s.get("cover") else tr("COULD_NOT_SAVE_IT", lang))
        return await pick_account_or_go(chat, uid, m["result"]["message_id"], s.get("quality", "best"))
    if s.get("step") == "await_mon_cover" and msg.get("photo"):   # fixed cover for a monitor
        s["step"] = None
        if s.get("mon_pending") is None:
            return await send(chat, tr("EXPIRED_START_AGAIN_FROM", lang))
        sizes = msg["photo"]                                      # pick a thumb-sized image (≤320px)
        photo = next((p for p in reversed(sizes) if p.get("width", 9999) <= 400), sizes[0])
        cov_dir = os.path.join(config.STATE_DIR, "covers")
        os.makedirs(cov_dir, exist_ok=True)
        cov_path = os.path.join(cov_dir, f"mon_{uid}_{int(time.time())}.jpg")
        try:
            await download_file(photo["file_id"], cov_path)
            s["mon_pending"]["cover"] = cov_path
        except Exception:
            pass
        m = await send(chat, tr("THUMBNAIL_SAVED", lang) if s["mon_pending"].get("cover") else tr("COULD_NOT_SAVE_IT", lang))
        return await monitor_ask_start(chat, uid, m["result"]["message_id"])
    if s.get("step") == "await_mon_edit_cover" and msg.get("photo"):   # change an existing monitor's cover
        s["step"] = None
        mon_id = s.get("edit_mon")
        sizes = msg["photo"]
        photo = next((p for p in reversed(sizes) if p.get("width", 9999) <= 400), sizes[0])
        cov_dir = os.path.join(config.STATE_DIR, "covers")
        os.makedirs(cov_dir, exist_ok=True)
        cov_path = os.path.join(cov_dir, f"mon_{uid}_{int(time.time())}.jpg")
        try:
            await download_file(photo["file_id"], cov_path)
            monitors.set_param(mon_id, "cover", cov_path)
            txt = tr("THUMBNAIL_UPDATED", lang)
        except Exception:
            txt = tr("COULD_NOT_SAVE_THE", lang)
        m = await send(chat, txt)
        return await monitor_detail(chat, uid, m["result"]["message_id"], mon_id)
    if s.get("step") == "await_mon_edit_iv" and text:    # edit an existing monitor's interval
        s["step"] = None
        mon_id = s.get("edit_mon")
        lo, hi = _parse_interval(text)
        if not lo:
            s["step"] = "await_mon_edit_iv"
            return await send(chat, tr("COULD_NOT_PARSE_THAT", lang))
        monitors.set_interval(mon_id, lo, hi)
        _mon_last.pop(mon_id, None)
        _mon_iv.pop(mon_id, None)                         # re-roll with the new interval
        m = await send(chat, tr("FREQUENCY_UPDATED", lang))
        return await monitor_detail(chat, uid, m["result"]["message_id"], mon_id)
    if s.get("step") == "await_mon_schedule" and text:    # fixed time/days for a NEW monitor
        s["step"] = None
        if s.get("mon_pending") is None:
            return await send(chat, tr("EXPIRED_START_AGAIN_FROM", lang))
        sched = _parse_schedule(text)
        if not sched:
            s["step"] = "await_mon_schedule"
            return await send(chat, tr("COULD_NOT_PARSE_THAT_2", lang))
        s["mon_pending"]["schedule"] = sched
        s["mon_pending"]["interval_max"] = 0
        m = await send(chat, tr("WILL_SCAN", lang).format(when=_schedule_label(sched, lang)))
        return await monitor_ask_cover(chat, uid, m["result"]["message_id"])
    if s.get("step") == "await_mon_edit_sched" and text:  # edit schedule on an existing monitor
        s["step"] = None
        mon_id = s.get("edit_mon")
        sched = _parse_schedule(text)
        if not sched:
            s["step"] = "await_mon_edit_sched"
            return await send(chat, tr("COULD_NOT_PARSE_THAT_2", lang))
        monitors.set_schedule(mon_id, sched)
        _mon_last.pop(mon_id, None)
        m = await send(chat, tr("SCHEDULE_UPDATED_WILL_SCAN", lang).format(when=_schedule_label(sched, lang)))
        return await monitor_detail(chat, uid, m["result"]["message_id"], mon_id)
    if s.get("step") == "await_creds" and text:      # typed user:pass for a service
        svc = s.get("acc_service")
        s["step"] = None
        user, pw = (text.split(":", 1) if ":" in text else (text.split(None, 1) + [""])[:2])
        try:
            await call("deleteMessage", chat_id=chat, message_id=msg["message_id"])  # hide the secret
        except Exception:
            pass
        try:
            acct = auth.add_credential(uid, svc, user.strip(), pw.strip())
            return await send(chat, tr("SAVED_ACCOUNT_FOR_ENCRYPTED", lang).format(name=html.escape(acct['label']), svc=svc))
        except ValueError as e:
            return await send(chat, f"🔴 {html.escape(str(e))}")
    if s.get("step") == "await_sublang" and text:    # typed a subtitle language code
        code = re.sub(r"[^A-Za-z-]", "", text)[:8].lower()
        s["step"] = None
        s["s_lang"] = [code]
        s["sub_extra_lang"] = code
        m = await send(chat, "⏳...")
        return await pick_account_or_go(chat, uid, m["result"]["message_id"], "best")
    if s.get("step") == "await_input" and text:      # non-URL input (e.g. id/search)
        s["step"] = None
        return await show_titles(chat, uid, text)
    if s.get("step") == "await_search" and text:     # top-level search: query -> results
        s["step"] = None
        svc = s.get("search_service")
        back = "m:subs" if s.get("subs_mode") else "m:search"
        m = await send(chat, tr("SEARCHING_ON", lang).format(q=html.escape(text), svc=svc))
        mid = m["result"]["message_id"]
        try:
            results = await engine.search(svc, text, profile=str(uid),
                                          credential=auth.first_credential(uid, svc))
        except Exception as e:
            return await edit(chat, mid, tr("SEARCH_FAILED", lang).format(err=html.escape(str(e))), [[(tr("BACK", lang), back)]])
        s["search_results"] = results
        s["search_query"] = text
        s["search_filter"] = None                 # reset type filter + page for a fresh query
        s["search_page"] = 0
        if not results:
            return await edit(chat, mid, tr("NO_RESULTS_FOUND_FOR", lang).format(q=html.escape(text)), [[(tr("SEARCH_AGAIN", lang), back)]])
        return await show_search_results(chat, uid, mid)


# --------------------------------------------------------------------------
# Long-poll loop
# --------------------------------------------------------------------------
async def _heartbeat_loop():
    """Write a liveness timestamp every 60s. A watchdog (CronJob) reads it from the data volume
    and alerts if it goes stale - catching a dead pod OR a wedged event loop (e.g. CPU starvation),
    which a plain pod-readiness check would miss."""
    hb = config.STATE_DIR / "heartbeat"
    while True:
        try:
            hb.write_text(str(int(time.time())))
        except Exception:
            pass
        await asyncio.sleep(60)


async def run():
    config.ensure_dirs()
    users.load()
    monitors.load()
    load_cat_overrides()
    asyncio.create_task(monitor_loop())            # background auto-monitor scheduler
    asyncio.create_task(_heartbeat_loop())         # liveness beat for the watchdog CronJob
    me = await call("getMe")
    if not me.get("ok"):
        raise SystemExit(f"Bot login failed: {me}")
    print(f"Bot up: @{me['result']['username']}  | admins={config.ADMIN_IDS}")
    up = await uploader.start()
    print("uploader:", "on (MTProto, ≤2GB)" if up else "off - set API_ID/API_HASH for uploads")
    offset = 0
    while True:
        try:
            resp = await call("getUpdates", offset=offset, timeout=25)
        except Exception as e:
            await report_error("getUpdates", e)
            await asyncio.sleep(3)
            continue
        if not resp.get("ok"):
            print("getUpdates not ok:", resp)
            await asyncio.sleep(3)
            continue
        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            try:
                if "callback_query" in upd:
                    await on_callback(upd["callback_query"])
                elif "message" in upd:
                    await on_message(upd["message"])
            except Exception as e:
                src = upd.get("callback_query") or upd.get("message") or {}
                uid = (src.get("from") or {}).get("id")
                await report_error("on_callback" if "callback_query" in upd else "on_message", e, uid)
                # the user only learns an error occurred and was reported - no details
                chat = ((src.get("message") or src).get("chat") or {}).get("id")
                if chat:
                    try:
                        await send(chat, tr("AN_ERROR_OCCURRED_IT_2", users.lang(uid)))
                    except Exception:
                        pass


if __name__ == "__main__":
    asyncio.run(run())
