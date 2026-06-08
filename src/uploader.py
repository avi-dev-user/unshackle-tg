"""
File delivery via Pyrofork as a BOT (MTProto) - sends files up to 2GB.

Why MTProto and not the HTTP Bot API: the cloud Bot API caps uploads at 50MB,
but an MTProto *bot* client (api_id+api_hash+bot_token, NO user/Premium session)
can send up to 2GB. We start it with no_updates=True so it ONLY sends and does
NOT consume updates - the aiohttp frontend keeps owning getUpdates (no conflict).

For >2GB a Premium userbot would be needed (deferred).
"""
import os
import re
import subprocess
from glob import glob

from pyrogram import Client
from pyrogram.enums import ParseMode

from . import config, metadata
from .i18n import tr

_app: Client | None = None          # MTProto BOT client - up to 2GB, no session needed
_premium: Client | None = None      # Premium USER client - up to 4GB (needs PREMIUM_SESSION)

# Telegram measures in GiB (binary). Bot=2 GiB, Premium=4 GiB. Small safety margin.
GiB = 1024 ** 3
BOT_LIMIT = 2 * GiB - 16 * 1024 * 1024       # ≤2 GiB → bot client
PREMIUM_LIMIT = 4 * GiB - 16 * 1024 * 1024   # ≤4 GiB → Premium user client


async def start() -> bool:
    """Start the uploader client(s). Bot client (≤2GB) is primary; if PREMIUM_SESSION
    is set, also start a Premium user client (≤4GB) for big files. Never raises."""
    global _app, _premium
    if not (config.API_ID and config.API_HASH and config.BOT_TOKEN):
        return False
    if _app is None or not _app.is_connected:
        client = Client(
            "unshackle_uploader",
            api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN,
            no_updates=True,                 # send-only; don't steal updates from the frontend
            workdir=str(config.STATE_DIR),   # persistent session file (no re-auth → no FloodWait)
        )
        try:
            await client.start()
            _app = client
        except Exception as e:
            print(f"uploader start failed ({type(e).__name__}): {e}")
            _app = None
            return False
    # optional Premium user client for >2GB
    if config.PREMIUM_SESSION and (_premium is None or not _premium.is_connected):
        try:
            p = Client("unshackle_premium", api_id=config.API_ID, api_hash=config.API_HASH,
                       session_string=config.PREMIUM_SESSION, no_updates=True, in_memory=True,
                       app_version="unshackle-bot uploader", device_model="unshackle-bot (4GB uploader)",
                       system_version="downloader")
            await p.start()
            _premium = p
            print("premium uploader: on (≤4GB)")
        except Exception as e:
            print(f"premium uploader off ({type(e).__name__}): {e}")
            _premium = None
    return True


def _clean_name(path: str, title: str = "", ext: str = "") -> str:
    """A human filename for Telegram: prefer the metadata title, dots/underscores
    → spaces, real extension. Avoids Telegram's mangled '..._1.mka_1'."""
    ext = ext or os.path.splitext(path)[1] or ".bin"
    stem = (title or os.path.splitext(os.path.basename(path))[0]).strip()
    stem = re.sub(r'[._]+', ' ', stem).strip()
    stem = re.sub(r'[\\/:*?"<>|]', '', stem)[:120] or "file"
    return f"{stem}{ext}"


def _split_file(path: str, hard_limit: int) -> list[str] | None:
    """Split a file too big for one Telegram message into sendable parts.
    Video/audio → playable time-segments (stream-copy, each a standalone file).
    Anything else → binary chunks (rejoin with `cat part.* > file`). Returns the
    part paths (>1) or None if it couldn't split under the limit."""
    target = int(hard_limit * 0.88)          # aim a bit under so segments stay below the cap
    base, ext = os.path.splitext(path)
    kind = metadata.media_kind(path)
    if kind in ("video", "music"):
        info = metadata._ffprobe(path)
        dur = float((info.get("format", {}) or {}).get("duration") or 0)
        size = os.path.getsize(path)
        if dur > 0 and size > 0:
            seg = max(1, int(dur * target / size))
            outpat = f"{base}.part%03d{ext}"
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", path, "-c", "copy", "-map", "0",
                 "-f", "segment", "-segment_time", str(seg), "-reset_timestamps", "1", outpat],
                capture_output=True, timeout=3600)
            parts = sorted(glob(f"{base}.part[0-9]*{ext}"))
            if r.returncode == 0 and len(parts) > 1 and all(os.path.getsize(p) <= hard_limit for p in parts):
                return parts
            for p in parts:                  # clean up a failed/oversized attempt
                try:
                    os.remove(p)
                except OSError:
                    pass
    # fallback: binary split (parts aren't directly playable)
    parts, idx, written, out = [], 1, 0, None
    BLK = 8 * 1024 * 1024
    with open(path, "rb") as f:
        while True:
            b = f.read(BLK)
            if not b:
                break
            if out is None:
                pp = f"{path}.{idx:03d}"
                out = open(pp, "wb")
                parts.append(pp)
            out.write(b)
            written += len(b)
            if written >= target:
                out.close()
                out, written, idx = None, 0, idx + 1
    if out:
        out.close()
    return parts if len(parts) > 1 else None


def _make_video_thumb(path: str, duration: int = 0) -> str | None:
    """Grab a representative frame as the Telegram video thumbnail (JPEG, ≤320px wide).
    Seeks ~10% in to skip black intros. Returns a temp path the caller must delete."""
    import tempfile

    fd, out = tempfile.mkstemp(prefix="thumb_", suffix=".jpg", dir=config.STATE_DIR)
    os.close(fd)
    ts = max(1, int(duration * 0.1)) if duration else 5
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(ts), "-i", path, "-frames:v", "1",
             "-vf", "scale=320:-2", "-q:v", "4", out],
            capture_output=True, timeout=60)
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            return out
    except Exception:
        pass
    if os.path.exists(out):
        os.remove(out)
    return None


async def send(chat_id: int, path: str, caption: str, cover: str | None = None,
               progress=None, force_kind: str | None = None, lang: str = "en") -> None:
    """Send the downloaded file with the right method + caption (+ cover for music).
    force_kind: 'video' → streamable video, 'file' → document (both keep a thumbnail).
    progress(current, total) is Pyrogram's real-time upload callback."""
    if _app is None:
        raise RuntimeError("uploader not started (missing API_ID/API_HASH)")
    # pick the client by size: bot ≤2GB, Premium user up to 4GB
    size = os.path.getsize(path) if os.path.exists(path) else 0
    if size > BOT_LIMIT:
        if _premium is None:
            raise RuntimeError("The file is larger than 2GB - a Premium account (PREMIUM_SESSION) "
                               "is required. Choose a lower quality for now.")
        client = _premium
    else:
        client = _app
    kind = metadata.media_kind(path)
    if force_kind == "file":          # user chose to receive it as a document
        kind = "document"
    elif force_kind == "video" and kind != "music":
        kind = "video"
    info = metadata._ffprobe(path)
    tags = {}
    for st in info.get("streams", []):
        if st.get("codec_type") == "audio":
            tags.update(st.get("tags") or {})
    tags.update((info.get("format", {}) or {}).get("tags") or {})
    title = metadata._expand_se(tags.get("title") or "", lang)
    performer = tags.get("artist") or tags.get("album_artist") or ""
    ext = metadata.audio_ext(path) if kind == "music" else ""
    fname = _clean_name(path, title, ext)
    common = dict(chat_id=chat_id, caption=caption, parse_mode=ParseMode.HTML,
                  file_name=fname, progress=progress)
    if kind == "video":
        # pass real dimensions/duration so Telegram shows correct resolution +
        # a streamable inline player (not a generic file).
        vw = vh = 0
        for st in info.get("streams", []):
            if st.get("codec_type") == "video" and not (st.get("disposition", {}) or {}).get("attached_pic"):
                vw, vh = st.get("width") or 0, st.get("height") or 0
                break
        vdur = int(float((info.get("format", {}) or {}).get("duration") or 0))
        # generate a poster frame so Telegram shows a real thumbnail (not a grey box)
        thumb = cover or _make_video_thumb(path, vdur)
        try:
            await client.send_video(video=path, thumb=thumb, width=vw or None, height=vh or None,
                                  duration=vdur or None, supports_streaming=True, **common)
        finally:
            if thumb and thumb != cover and os.path.exists(thumb):
                os.remove(thumb)
    elif kind == "music":
        # an embedded cover (mjpeg) is a 'video' stream → Telegram shows it as a
        # document. Strip to pure audio and attach the cover separately as thumb.
        send_path, tmp = path, None
        if any(st.get("codec_type") == "video" for st in info.get("streams", [])):
            import tempfile
            fd, tmp = tempfile.mkstemp(prefix="audio_", suffix=(ext or ".mp3"), dir=config.STATE_DIR)
            os.close(fd)                       # unique per call - concurrent uploads can't collide
            r = subprocess.run(["ffmpeg", "-y", "-i", path, "-vn", "-c:a", "copy", tmp],
                               capture_output=True, timeout=300)
            if r.returncode == 0 and os.path.exists(tmp):
                send_path = tmp
        dur = int(float((info.get("format", {}) or {}).get("duration") or 0))
        try:
            await client.send_audio(audio=send_path, thumb=cover, title=title or None,
                                  performer=performer or None, duration=dur, **common)
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
    else:
        # document: still attach a poster thumbnail if the file is actually a video
        thumb, tmp_thumb = cover, None
        if not thumb and any(st.get("codec_type") == "video" for st in info.get("streams", [])):
            vdur = int(float((info.get("format", {}) or {}).get("duration") or 0))
            tmp_thumb = thumb = _make_video_thumb(path, vdur)
        try:
            await client.send_document(document=path, thumb=thumb, **common)
        finally:
            if tmp_thumb and os.path.exists(tmp_thumb):
                os.remove(tmp_thumb)


async def deliver(chat_id: int, path: str, service: str = "", source_url: str = "",
                  media_url: str = "", progress=None, force_kind: str | None = None,
                  cover_path: str | None = None, lang: str = "en") -> None:
    """Build caption + cover, send, then delete the local file (and empty parent dir).
    media_url = the direct source file (e.g. the episode's mp3) → enriches music
    tags/cover that the .mka remux dropped. progress = real-time upload callback."""
    if not await start():            # lazy (re)start - e.g. after a FLOOD_WAIT cleared
        raise RuntimeError("uploader unavailable (FloodWait or missing API_ID/HASH) - try again shortly")
    caption = metadata.build_caption(path, service_name=service, source_url=source_url,
                                     media_url=media_url, lang=lang)
    # a per-monitor fixed cover wins; otherwise pull an embedded cover for music
    cover = cover_path if (cover_path and os.path.exists(cover_path)) else None
    if cover is None and metadata.media_kind(path) == "music":
        import tempfile
        fd, cov = tempfile.mkstemp(prefix="cover_", suffix=".jpg", dir=config.STATE_DIR)
        os.close(fd)                           # unique per call - concurrent uploads can't collide
        cover = metadata.extract_cover(path, cov) or (
            metadata.extract_cover(media_url, cov) if media_url else None)
        if not cover and os.path.exists(cov):
            os.remove(cov)                     # no cover extracted -> drop the empty temp file
    # if the file is over the biggest client's cap, split it into sendable parts
    max_limit = PREMIUM_LIMIT if _premium is not None else BOT_LIMIT
    size = os.path.getsize(path) if os.path.exists(path) else 0
    parts = _split_file(path, max_limit) if size > max_limit else None
    try:
        if parts:
            n = len(parts)
            for i, part in enumerate(parts, 1):
                cap = f"{caption}\n\n📦 " + tr("CAP_PART", lang).format(i=i, n=n)
                await send(chat_id, part, cap, cover, progress=progress, force_kind=force_kind, lang=lang)
        else:
            if size > max_limit:             # couldn't split → tell the user clearly
                raise RuntimeError(
                    f"The file ({size / 1024**3:.1f}GiB) is larger than the allowed "
                    f"({4 if _premium else 2}GiB) and could not be split automatically - choose a lower quality.")
            await send(chat_id, path, caption, cover, progress=progress, force_kind=force_kind, lang=lang)
    finally:
        if cover and os.path.exists(cover):
            os.remove(cover)
        for part in (parts or []):
            try:
                os.remove(part)
            except OSError:
                pass
    # cleanup: remove the file and its parent dir if now empty (don't keep it locally)
    try:
        os.remove(path)
        parent = os.path.dirname(path)
        if parent and os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)
    except OSError:
        pass
