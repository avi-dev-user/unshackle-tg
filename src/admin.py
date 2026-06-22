"""Admin users panel: list users, manage status/role, edit per-user service
permissions, and broadcast a message to the audience. on_users_callback is the
self-contained sub-router the main dispatch delegates every u*/upm*/bc-less admin
callback to."""
import asyncio
import html

from . import config, state, users
from .i18n import tr
from .session import sess
from .tg import GRID_N, PAGE, call, edit, grid_rows


def _perm_summary(u: dict, lang: str) -> str:
    mode = u.get("perm_mode", "all")
    svcs = ", ".join(u.get("perm_services") or []) or "-"
    base = {"only": tr("ONLY", lang).format(svcs=svcs),
            "except": tr("ALL_EXCEPT", lang).format(svcs=svcs)}.get(mode, tr("ALL_SERVICES", lang))
    extra = []
    if u.get("block_auth"):
        extra.append(tr("NO_LOGIN_REQUIRED_SERVICES", lang))
    if u.get("block_drm"):
        extra.append(tr("NO_DRM_SERVICES", lang))
    cats = u.get("cats") or []
    if cats:
        names = {"il": "🌍", "free": "🆓", "sub": "💳"}
        extra.append(tr("CATEGORIES", lang) + " ".join(names[c] for c in cats))
    return base + (" · " + " · ".join(extra) if extra else "")


async def users_panel(chat: int, uid: int, mid: int, page: int):
    lang = users.lang(uid)
    us = users.all_users()
    pages = max(1, (len(us) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = us[page * PAGE:(page + 1) * PAGE]
    rows = [[(users.label(u), f"u:{users.key(u)}")] for u in chunk]   # one user per row
    nav = []
    if page > 0:
        nav.append(("◀", f"u:p:{page-1}"))
    nav.append((f"{page+1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(("▶", f"u:p:{page+1}"))
    rows.append(nav)
    rows.append([(tr("ADD_USER", lang), "u:add")])
    rows.append([(tr("MENU", lang), "m:main")])
    legend = tr("ADMIN_ACTIVE_SUSPENDED_AWAITING", lang)
    await edit(chat, mid, tr("USERS_PICK_USER_TO", lang).format(count=len(us), legend=legend), rows)


async def user_detail(chat: int, uid: int, mid: int, k: str):
    lang = users.lang(uid)
    u = users.by_key(k)
    if not u:
        return await edit(chat, mid, tr("USER_NOT_FOUND", lang), [[(tr("BACK", lang), "m:users")]])
    is_super = u.get("id") in config.ADMIN_IDS
    name = html.escape(u.get("name") or (f"@{u['username']}" if u.get("username") else "") or "-")
    lines = [f"<b>{name}</b>"]
    lines.append(f"ID: <code>{u.get('id') or tr("PENDING", lang)}</code>")
    if u.get("username"):
        lines.append(tr("USERNAME", lang).format(username=html.escape(u['username'])))
    lines.append(tr("ROLE", lang).format(role=(tr("ADMIN", lang) if u.get("is_admin") else tr("USER", lang))) + (tr("SUPER", lang) if is_super else ""))
    lines.append(tr("STATUS", lang).format(status=(tr("ACTIVE", lang) if u.get("status") == "active" else tr("SUSPENDED", lang))))
    lines.append(tr("PERMISSIONS", lang).format(summary=html.escape(_perm_summary(u, lang))))
    rows = []
    if u.get("status") == "active":
        rows.append([(tr("SUSPEND_ACCESS", lang), f"ust:{k}")])
    else:
        rows.append([(tr("RESTORE_ACCESS", lang), f"ust:{k}")])
    rows.append([(tr("REMOVE_ADMIN", lang) if u.get("is_admin") else tr("MAKE_ADMIN", lang), f"uad:{k}")])
    rows.append([(tr("SERVICE_PERMISSIONS", lang), f"upm:{k}")])
    if not is_super:
        rows.append([(tr("DELETE_USER", lang), f"urm:{k}")])
    rows.append([(tr("BACK", lang), "m:users")])
    await edit(chat, mid, "\n".join(lines), rows)


async def user_perms(chat: int, uid: int, mid: int, k: str, page: int = 0):
    lang = users.lang(uid)
    u = users.by_key(k)
    if not u:
        return await edit(chat, mid, tr("USER_NOT_FOUND", lang), [[(tr("BACK", lang), "m:users")]])
    mode = u.get("perm_mode", "all")
    dot = lambda m: "🔘" if mode == m else "⚪"
    chk = lambda on: "✅" if on else "⬜"
    rows = [[(f"{dot('all')} " + tr("ALL", lang), f"upmode:{k}:all"),
             (f"{dot('only')} " + tr("ONLY_THESE", lang), f"upmode:{k}:only"),
             (f"{dot('except')} " + tr("EXCEPT_THESE", lang), f"upmode:{k}:except")]]
    # attribute rules (apply on top of the mode rule, AND-combined)
    rows.append([(f"{chk(u.get('block_auth'))} " + tr("BLOCK_LOGIN_SERVICES", lang), f"upf:{k}:block_auth")])
    rows.append([(f"{chk(u.get('block_drm'))} " + tr("BLOCK_DRM_SERVICES", lang), f"upf:{k}:block_drm")])
    rows.append([(f"{chk(u.get('use_default_cdm'))} " + tr("ALLOW_SHARED_CDM_DEFAULT", lang), f"upf:{k}:use_default_cdm")])
    rows.append([(f"{chk(u.get('can_monitor'))} " + tr("AUTO_MONITOR_2", lang), f"upf:{k}:can_monitor"),
                 (tr("CONCURRENCY", lang).format(n=u.get('max_concurrent') or users.DEFAULT_CONCURRENCY), f"ucc:{k}")])
    rows.append([(f"{chk(u.get('can_gofile_upload'))} " + tr("GOFILE_UPLOAD_PERM", lang), f"upf:{k}:can_gofile_upload")])
    rows.append([(f"{chk(u.get('can_keys_download'))} " + tr("KEYS_DOWNLOAD_PERM", lang), f"upf:{k}:can_keys_download")])
    cats = set(u.get("cats") or [])
    rows.append([(f"{chk('il' in cats)} 🌍", f"upc:{k}:il"),
                 (f"{chk('free' in cats)} 🆓", f"upc:{k}:free"),
                 (f"{chk('sub' in cats)} 💳", f"upc:{k}:sub")])
    sel = set(u.get("perm_services") or [])
    if mode in ("only", "except"):
        tags = sorted(s["tag"] for s in await state.services())
        pages = max(1, (len(tags) + GRID_N - 1) // GRID_N)
        page = max(0, min(page, pages - 1))
        chunk = tags[page * GRID_N:(page + 1) * GRID_N]
        rows += grid_rows([(("✅ " if t in sel else "") + t, f"ups:{k}:{t}") for t in chunk], 4)
        nav = []
        if page > 0:
            nav.append(("◀", f"upm:{k}:{page-1}"))
        nav.append((f"{page+1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(("▶", f"upm:{k}:{page+1}"))
        rows.append(nav)
    rows.append([(tr("BACK_TO_USER", lang), f"u:{k}")])
    hint = {"all": tr("STARTS_FROM_ALL_SERVICES", lang),
            "only": tr("ONLY_THE_SERVICES_CHECKED", lang),
            "except": tr("ALL_SERVICES_EXCEPT_THE", lang)}[mode]
    rule = tr("FILTERS_STACK_ALL_MUST", lang)
    await edit(chat, mid, tr("SERVICE_PERMISSIONS_TITLE", lang) + f"\n{hint}{rule}\n\n"
               + tr("CURRENT", lang).format(summary=html.escape(_perm_summary(u, lang))), rows)


async def do_broadcast(chat: int, uid: int, mid: int, audience: str):
    """Copy the stored message (no 'forwarded from' credit) to the chosen audience."""
    lang = users.lang(uid)
    s = sess(uid)
    bc = s.get("bcast")
    if not bc:
        return await edit(chat, mid, tr("NO_MESSAGE_TO_BROADCAST", lang), [[(tr("MENU", lang), "m:main")]])
    targets = users.active_ids() if audience == "active" else users.seen_ids()
    targets = [t for t in dict.fromkeys(targets)]      # de-dupe, keep order
    await edit(chat, mid, tr("BROADCASTING_TO_USERS", lang).format(n=len(targets)))
    sent = failed = 0
    for i, t in enumerate(targets, 1):
        try:
            r = await call("copyMessage", chat_id=t, from_chat_id=bc["from_chat"], message_id=bc["mid"])
            sent += 1 if r.get("ok") else 0
            failed += 0 if r.get("ok") else 1
        except Exception:
            failed += 1
        if i % 20 == 0:
            await edit(chat, mid, tr("BROADCASTING", lang).format(i=i, total=len(targets)))
        await asyncio.sleep(0.05)                       # stay under Telegram's broadcast rate
    s.pop("bcast", None)
    await edit(chat, mid, tr("BROADCAST_FINISHED_SENT_FAILED", lang).format(sent=sent, failed=failed), [[(tr("MENU", lang), "m:main")]])


async def on_users_callback(chat: int, uid: int, mid: int, data: str):
    lang = users.lang(uid)
    if data == "m:users":
        return await users_panel(chat, uid, mid, 0)
    if data.startswith("u:p:"):
        return await users_panel(chat, uid, mid, int(data[4:]))
    if data == "u:add":
        sess(uid)["step"] = "await_adduser"
        return await edit(chat, mid, tr("TO_ADD_USER_SEND", lang),
                          [[(tr("BACK", lang), "m:users")]])
    if data.startswith("upmode:"):
        _, k, mode = data.split(":", 2)
        users.set_perm_mode(k, mode)
        return await user_perms(chat, uid, mid, k, 0)
    if data.startswith("ups:"):
        _, k, tag = data.split(":", 2)
        users.toggle_perm_service(k, tag)
        return await user_perms(chat, uid, mid, k, 0)
    if data.startswith("upf:"):
        _, k, flag = data.split(":", 2)
        users.toggle_flag(k, flag)
        return await user_perms(chat, uid, mid, k, 0)
    if data.startswith("upc:"):
        _, k, cat = data.split(":", 2)
        users.toggle_cat(k, cat)
        return await user_perms(chat, uid, mid, k, 0)
    if data.startswith("ucc:"):                      # cycle the concurrency limit
        k = data[4:]
        u = users.by_key(k)
        cur = (u or {}).get("max_concurrent") or users.DEFAULT_CONCURRENCY
        steps = [1, 2, 3, 5, 10]
        users.set_concurrency(k, steps[(steps.index(cur) + 1) % len(steps)] if cur in steps else 2)
        return await user_perms(chat, uid, mid, k, 0)
    if data.startswith("upm:"):
        parts = data[4:].rsplit(":", 1)
        k, page = (parts[0], int(parts[1])) if len(parts) == 2 and parts[1].isdigit() else (data[4:], 0)
        return await user_perms(chat, uid, mid, k, page)
    if data.startswith("ust:"):
        k = data[4:]
        u = users.by_key(k)
        try:
            users.set_status(k, "suspended" if (u and u.get("status") == "active") else "active")
        except ValueError as e:
            await call("answerCallbackQuery", callback_query_id="", text=str(e))
        return await user_detail(chat, uid, mid, k)
    if data.startswith("uad:"):
        k = data[4:]
        u = users.by_key(k)
        try:
            users.set_admin(k, not (u and u.get("is_admin")))
        except ValueError:
            pass
        return await user_detail(chat, uid, mid, k)
    if data.startswith("urmc:"):                 # confirmed remove
        k = data[5:]
        try:
            users.remove(k)
        except ValueError as e:
            return await edit(chat, mid, f"🔴 {html.escape(str(e))}", [[(tr("BACK", lang), f"u:{k}")]])
        return await users_panel(chat, uid, mid, 0)
    if data.startswith("urm:"):                  # ask to confirm
        k = data[4:]
        return await edit(chat, mid, tr("DELETE_THIS_USER_PERMANENTLY", lang),
                          [[(tr("YES_DELETE", lang), f"urmc:{k}")], [(tr("CANCEL", lang), f"u:{k}")]])
    if data.startswith("u:"):                    # user detail (keep last - broadest)
        return await user_detail(chat, uid, mid, data[2:])
