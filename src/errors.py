"""User-facing and admin error reporting: friendly messages for users, full detail for admins."""
import html
import traceback

from . import config, users
from .i18n import tr
from .session import sess
from .tg import call, edit


def _friendly(err: str, lang="en") -> str:
    """Map any raw error to a clean user message (no technical detail), localized via tr."""
    e = (err or "").lower()
    if any(k in e for k in ("available in your country", "not available in your", "available in your region",
                            "geo-block", "geoblock", "blocked in your", "kaltura manifest", "could not obtain")):
        return tr("THIS_CONTENT_IS_BLOCKED", lang)
    if any(k in e for k in ("sign in to confirm", "not a bot", "--cookies", "cookies-from-browser",
                            "login required", "authenticat", "needs auth")):
        return tr("THIS_SERVICE_REQUIRES_LOGIN", lang)
    if "premium" in e:
        return tr("THIS_FILE_REQUIRES_PREMIUM", lang)
    if any(k in e for k in ("no episodes", "no content", "not found", "no audio", "no video track",
                            "not an rss", "no tracks", "could not read", "no match")):
        return tr("NO_MATCHING_CONTENT_WAS", lang)
    if any(k in e for k in ("widevine", "playready", "cdm", "license", "decrypt", " drm", "no key")):
        return tr("THIS_CONTENT_IS_PROTECTED", lang)
    if any(k in e for k in ("429", "too many requests", "rate limit", "temporarily unavailable")):
        return tr("THE_SERVICE_IS_BUSY", lang)
    if any(k in e for k in ("timed out", "timeout", "connection", "network error", "resolve host", "unreachable")):
        return tr("TEMPORARY_NETWORK_ISSUE_TRY", lang)
    return tr("AN_ERROR_OCCURRED_IT", lang)


def _admin_actor(uid) -> str:
    """Rich '👤 Name @user <id> [iOS | Android]' line for admin notifications.
    iOS/Android are tappable deep-links that open the user's chat (per telegram-self-notify)."""
    u = users.get(uid) or {}
    who = (html.escape(u.get("name") or "") + (f" @{html.escape(u['username'])}" if u.get("username") else "")).strip()
    links = (f'[<a href="https://t.me/@id{uid}">iOS</a> | '
             f'<a href="tg://openmessage?user_id={uid}">Android</a>]')
    return f"👤 {who or '?'} <code>{uid}</code> {links}"


async def user_error(chat: int, mid: int, uid, e, back=None, allow_retry: bool = False) -> None:
    """User sees a friendly message; the admin gets the full technical detail + who.
    allow_retry: offer a one-tap 🔁 retry when the failure looks transient (rate-limit, network,
    timeout, or generic) - i.e. cases where re-running the same request might just work. Errors
    with a more specific remedy (login/CDM/geo/premium) get that action instead, not a retry."""
    lang = users.lang(uid)
    msg = str(e)
    for admin in config.ADMIN_IDS:
        try:
            await call("sendMessage", chat_id=admin, parse_mode="HTML", disable_web_page_preview=True,
                       text=f"⚠️ <b>User error</b>\n{_admin_actor(uid)}\n<code>{html.escape(msg[:1800])}</code>")
        except Exception:
            pass
    print(f"[user_error] uid={uid}: {msg[:200]}")
    if back is None:
        back = (tr("MENU", lang), "m:main")
    rows = []
    low = msg.lower()
    svc = sess(uid).get("service")
    # actionable: a cookies/login error → offer a direct button to add the account
    if svc and any(k in low for k in ("sign in to confirm", "not a bot", "cookies",
                                      "authenticat", "login required", "needs auth")):
        rows.append([(tr("ADD_AN_ACCOUNT_FOR", lang).format(svc=svc), f"as:{svc}")])
    elif any(k in low for k in ("widevine", "playready", "cdm", "license", "decrypt", " drm")):
        rows.append([(tr("CDM_FILES", lang), "m:cdm")])
    # transient failure with a saved attempt → re-run it without re-walking the wizard. Skip when the
    # error is one that retrying identically won't fix (geo-block, login, premium, protected, no content).
    elif allow_retry and not any(k in low for k in (
            "available in your", "geo-block", "geoblock", "blocked in your", "premium", "protected",
            "sign in", "authenticat", "login", "needs auth", "cookies", "widevine", "playready",
            "cdm", "license", "decrypt", " drm", "no matching", "no content", "no episodes", "not found",
            "no playable", "entitle", "subscri", "concurrency")):
        rows.append([(tr("TRY_AGAIN", lang), "retry")])
    rows.append([back])
    await edit(chat, mid, _friendly(msg, lang), rows)


async def report_error(where: str, exc: Exception, uid=None) -> None:
    """Catch-all: DM every admin the FULL error (head + complete traceback, chunked so
    nothing is truncated by Telegram's 4096-char limit)."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    head = (f"⚠️ <b>Error</b> · {html.escape(where)}"
            + (f" · user <code>{uid}</code>" if uid else "")
            + f"\n<code>{html.escape(type(exc).__name__)}: {html.escape(str(exc)[:400])}</code>")
    chunks = [tb[i:i + 3500] for i in range(0, len(tb), 3500)] or [""]
    for admin in config.ADMIN_IDS:
        try:
            await call("sendMessage", chat_id=admin, text=head, parse_mode="HTML")
            for c in chunks:
                await call("sendMessage", chat_id=admin, text=f"<pre>{html.escape(c)}</pre>",
                           parse_mode="HTML")
        except Exception:
            pass
    print(f"[error] {where}: {exc!r}")

