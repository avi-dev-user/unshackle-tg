"""Live recording: capture a configured live channel (e.g. a 4K HEVC feed) in real time,
decrypting CENC/ClearKey on the fly, and deliver it. Small captures go to Telegram via the
uploader; large ones (4K is ~8GB/h, over Telegram's cap) are published behind an unguessable
download link that a cleanup job expires.

Channels are stored as JSON so they can be added/edited/removed from the bot with no code change:
{ "<name>": {"url": "<live manifest>", "key": "<cenc key hex, optional>"} }.
"""
import asyncio
import html
import json
import os
import secrets
import shutil
import time

from . import config, uploader, users
from .errors import report_error
from .i18n import tr
from .session import sess
from .tg import call, edit, send

CHANNELS_FILE = config.STATE_DIR / "kan_channels.json"
REC_DIR = os.environ.get("REC_DIR", "/data/recordings")          # nginx serves this (token dirs)
REC_URL_BASE = os.environ.get("REC_URL_BASE", "https://rec.avidev.net").rstrip("/")
REC_PROXY = os.environ.get("REC_PROXY", "http://127.0.0.1:8889")  # IL proxy for the live edge
TG_LIMIT = 2 * 1024 * 1024 * 1024                                 # send via Telegram below this
DURATIONS = [("30m", 1800), ("1h", 3600), ("90m", 5400), ("2h", 7200), ("3h", 10800)]
active = {}                                                       # uid -> running recording label


# --------------------------------------------------------------------------
# channel store
# --------------------------------------------------------------------------
def load() -> dict:
    try:
        with open(CHANNELS_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save(chans: dict) -> None:
    CHANNELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(CHANNELS_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(chans, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CHANNELS_FILE)                               # atomic; survives restarts (hostPath)


def put(name: str, url: str, key: str = "") -> None:
    chans = load()
    chans[name] = {"url": url, "key": key}
    save(chans)


def delete(name: str) -> None:
    chans = load()
    chans.pop(name, None)
    save(chans)


# --------------------------------------------------------------------------
# recording + delivery
# --------------------------------------------------------------------------
def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:40] or "rec"


async def _capture(uid: int, url: str, key: str, seconds: int, out: str) -> int:
    """ffmpeg: copy the highest video + audio, decrypting CENC in place. Async (never blocks the
    loop). The proc is tracked so a Stop button can end it early - ffmpeg finalizes a valid file
    on SIGTERM, so the partial capture is still playable and gets delivered."""
    env = dict(os.environ)
    env["http_proxy"] = env["https_proxy"] = REC_PROXY
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if key:
        cmd += ["-cenc_decryption_key", key]
    cmd += ["-i", url, "-t", str(seconds), "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", out]
    proc = await asyncio.create_subprocess_exec(*cmd, env=env,
                                                stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.DEVNULL)
    active[uid]["proc"] = proc
    await proc.wait()
    return proc.returncode or 0


def stop(uid: int) -> bool:
    """End a running recording early (graceful SIGTERM → ffmpeg finalizes the file)."""
    rec = active.get(uid) or {}
    proc = rec.get("proc")
    if proc and proc.returncode is None:
        try:
            proc.terminate()
            return True
        except Exception:
            pass
    return False


async def _deliver(chat: int, uid: int, mid: int, path: str, title: str, lang: str) -> None:
    """Telegram for small files; an expiring download link for big ones (4K over the cap)."""
    size = os.path.getsize(path) if os.path.exists(path) else 0
    if 0 < size <= TG_LIMIT:
        try:
            await edit(chat, mid, "⬆️ " + tr("UPLOADING", lang))
            await uploader.deliver(chat, path, service="Live", lang=lang, display_title=title)
            os.remove(path)
            return await edit(chat, mid, "🎉 " + tr("SENT", lang), [[(tr("MENU", lang), "m:main")]])
        except Exception:
            pass                                                 # fall through to a link on upload failure
    # large (or upload failed): publish behind a random token dir + send the link
    token = secrets.token_urlsafe(18)
    dest_dir = os.path.join(REC_DIR, token)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(path))
    shutil.move(path, dest)
    url = f"{REC_URL_BASE}/{token}/{os.path.basename(dest)}"
    gb = size / 1024 / 1024 / 1024
    await edit(chat, mid, "✅ " + tr("REC_READY_LINK", lang).format(size=f"{gb:.2f} GB")
               + f"\n\n🔗 {url}\n\n" + tr("REC_LINK_EXPIRES", lang),
               [[(tr("MENU", lang), "m:main")]])


async def _run(chat: int, uid: int, mid: int, name: str, seconds: int):
    lang = users.lang(uid)
    ch = load().get(name) or {}
    if not ch.get("url"):
        return await edit(chat, mid, "🔴 " + tr("REC_NO_SUCH_CHANNEL", lang), [[(tr("MENU", lang), "m:main")]])
    os.makedirs(REC_DIR, exist_ok=True)
    out = os.path.join(REC_DIR, f"{_safe(name)}_{int(time.time())}.mkv")
    active[uid] = {"name": name}
    try:
        await edit(chat, mid, f"🔴 {html.escape(name)}\n⏺️ " + tr("REC_RECORDING", lang).format(
            min=seconds // 60), [[(tr("REC_STOP", lang), "rec:stop")]])
        await _capture(uid, ch["url"], ch.get("key", ""), seconds, out)
        # deliver whenever there is a non-empty file: an early Stop (SIGTERM) yields rc!=0 but a
        # valid partial capture; only a truly empty/missing file is a failure.
        if not (os.path.exists(out) and os.path.getsize(out) > 0):
            if os.path.exists(out):
                os.remove(out)
            return await edit(chat, mid, "🔴 " + tr("REC_FAILED", lang), [[(tr("MENU", lang), "m:main")]])
        await _deliver(chat, uid, mid, out, name, lang)
    except Exception as e:
        await report_error("recording", e, uid)
        try:
            await edit(chat, mid, "🔴 " + tr("REC_FAILED", lang), [[(tr("MENU", lang), "m:main")]])
        except Exception:
            pass
    finally:
        active.pop(uid, None)


def start(chat: int, uid: int, mid: int, name: str, seconds: int) -> None:
    """Kick off a detached recording (returns immediately; the loop stays responsive)."""
    asyncio.create_task(_run(chat, uid, mid, name, seconds))


# --------------------------------------------------------------------------
# UI (admin)
# --------------------------------------------------------------------------
async def menu(chat: int, uid: int, mid: int):
    lang = users.lang(uid)
    chans = load()
    rows = [[(f"📡 {n}", f"rec:ch:{n}")] for n in sorted(chans)]
    rows.append([(tr("REC_ADD_CHANNEL", lang), "rec:add")])
    rows.append([(tr("MENU", lang), "m:main")])
    head = tr("REC_TITLE", lang)
    if active.get(uid):
        head += "\n⏺️ " + tr("REC_IN_PROGRESS", lang).format(name=html.escape(active[uid].get("name", "")))
    await edit(chat, mid, head, rows)


async def channel(chat: int, uid: int, mid: int, name: str):
    lang = users.lang(uid)
    if name not in load():
        return await menu(chat, uid, mid)
    rows = [[(tr("REC_RECORD", lang), f"rec:go:{name}")],
            [(tr("REC_EDIT", lang), f"rec:edit:{name}"), (tr("REC_DELETE", lang), f"rec:del:{name}")],
            [(tr("REC_BACK", lang), "m:rec")]]
    await edit(chat, mid, f"📡 <b>{html.escape(name)}</b>", rows)


async def ask_duration(chat: int, uid: int, mid: int, name: str):
    lang = users.lang(uid)
    rows = [[(lbl, f"rec:dur:{name}:{secs}")] for lbl, secs in DURATIONS]
    rows.append([(tr("REC_BACK", lang), f"rec:ch:{name}")])
    await edit(chat, mid, tr("REC_PICK_DURATION", lang).format(name=html.escape(name)), rows)
