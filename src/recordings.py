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


async def _segment(uid: int, url: str, key: str, seconds: int, out: str) -> None:
    """Record ONE segment with ffmpeg (-t seconds), copying the highest video+audio and decrypting
    CENC in place. The proc is tracked so Pause/Stop can SIGTERM it (ffmpeg finalizes a valid file)."""
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


def _signal(uid: int):
    proc = (active.get(uid) or {}).get("proc")
    if proc and proc.returncode is None:
        try:
            proc.terminate()                       # ffmpeg finalizes the current segment
        except Exception:
            pass


def pause(uid: int) -> None:
    """Pause: end the current segment now. The live edge moves on during the pause (that content
    is intentionally skipped); resuming starts a fresh segment that is concatenated at the end."""
    rec = active.get(uid)
    if rec and not rec.get("paused"):
        rec["paused"] = True
        _signal(uid)


def resume(uid: int) -> None:
    rec = active.get(uid)
    if rec:
        rec["paused"] = False                      # the run loop starts the next segment


def stop(uid: int) -> bool:
    rec = active.get(uid)
    if not rec:
        return False
    rec["stop"] = True
    _signal(uid)
    return True


async def _concat(segments: list, final: str) -> str:
    """Join the recorded segments (same codec → -c copy) into one file. One segment → just rename."""
    segments = [s for s in segments if os.path.exists(s) and os.path.getsize(s) > 0]
    if not segments:
        return ""
    if len(segments) == 1:
        os.replace(segments[0], final)
        return final
    listf = final + ".txt"
    with open(listf, "w", encoding="utf-8") as fh:
        for s in segments:
            fh.write(f"file '{s}'\n")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
        "-i", listf, "-c", "copy", final,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    for s in segments:
        try:
            os.remove(s)
        except OSError:
            pass
    try:
        os.remove(listf)
    except OSError:
        pass
    return final if (os.path.exists(final) and os.path.getsize(final) > 0) else ""


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


async def _status(chat: int, mid: int, name: str, paused: bool, lang: str) -> None:
    if paused:
        body = f"⏸️ {html.escape(name)}\n" + tr("REC_PAUSED", lang)
        rows = [[(tr("REC_RESUME", lang), "rec:resume")], [(tr("REC_STOP", lang), "rec:stop")]]
    else:
        body = f"🔴 {html.escape(name)}\n⏺️ " + tr("REC_RECORDING_LIVE", lang)
        rows = [[(tr("REC_PAUSE", lang), "rec:pause")], [(tr("REC_STOP", lang), "rec:stop")]]
    try:
        await edit(chat, mid, body, rows)
    except Exception:
        pass


async def _run(chat: int, uid: int, mid: int, name: str, seconds: int):
    lang = users.lang(uid)
    ch = load().get(name) or {}
    if not ch.get("url"):
        return await edit(chat, mid, "🔴 " + tr("REC_NO_SUCH_CHANNEL", lang), [[(tr("MENU", lang), "m:main")]])
    os.makedirs(REC_DIR, exist_ok=True)
    base = os.path.join(REC_DIR, f"{_safe(name)}_{int(time.time())}")
    active[uid] = {"name": name, "paused": False, "stop": False, "proc": None}
    segments, total, idx = [], 0.0, 0
    try:
        while not active[uid]["stop"] and total < seconds - 1:
            if active[uid]["paused"]:
                await _status(chat, mid, name, True, lang)
                while active[uid]["paused"] and not active[uid]["stop"]:
                    await asyncio.sleep(1)          # content during the pause is intentionally skipped
                continue
            await _status(chat, mid, name, False, lang)
            seg = f"{base}_{idx}.mkv"
            idx += 1
            t0 = time.time()
            await _segment(uid, ch["url"], ch.get("key", ""), int(seconds - total), seg)
            total += time.time() - t0
            if os.path.exists(seg) and os.path.getsize(seg) > 0:
                segments.append(seg)
            if not active[uid]["paused"] and not active[uid]["stop"]:
                break                               # segment hit its time cap → natural finish
        await edit(chat, mid, "🧩 " + tr("REC_FINALIZING", lang))
        final = await _concat(segments, base + ".mkv")
        if not final:
            return await edit(chat, mid, "🔴 " + tr("REC_FAILED", lang), [[(tr("MENU", lang), "m:main")]])
        await _deliver(chat, uid, mid, final, name, lang)
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
