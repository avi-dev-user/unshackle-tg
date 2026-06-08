"""
Build a rich Telegram caption from a downloaded media file (read with ffprobe).

The unshackle API doesn't expose music tags / duration / exact size - but the
DOWNLOADED FILE does. So at upload time we probe the file and fill the caption
templates the user specified (music vs video), and extract embedded cover art
for music.
"""
import json
import os
import re
import subprocess

from .i18n import tr


def _expand_se(text: str, lang: str = "en") -> str:
    """Expand scene 'S01E32' to a readable 'Season 1 Episode 32' (localized) inside a title."""
    def repl(m):
        return tr("SE_SPELLED", lang).format(s=int(m.group(1)), e=int(m.group(2)))
    return re.sub(r'S(\d+)E(\d+)', repl, text or "", flags=re.I)


def _ffprobe(path: str) -> dict:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=60,
        )
        return json.loads(out.stdout or "{}")
    except Exception:
        return {}


def _dur(seconds: float) -> str:
    s = int(float(seconds or 0))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"           # h:mm:ss for content ≥ 1 hour
    return f"{m:02d}:{sec:02d}"                    # mm:ss otherwise


def _tag(tags: dict, *names) -> str:
    for n in names:
        for k, v in tags.items():
            if k.lower() == n.lower() and str(v).strip():
                return str(v).strip()
    return ""


IMAGE_CODECS = {"mjpeg", "png", "bmp", "gif", "webp"}


def _is_real_video(st: dict) -> bool:
    """A genuine video stream - not embedded cover art (mjpeg/png, or attached_pic)."""
    return (st.get("codec_type") == "video"
            and st.get("codec_name") not in IMAGE_CODECS
            and not (st.get("disposition", {}) or {}).get("attached_pic"))


def is_music(info: dict) -> bool:
    """Audio with no real video stream (embedded cover art doesn't count)."""
    if any(_is_real_video(st) for st in info.get("streams", [])):
        return False
    return any(st.get("codec_type") == "audio" for st in info.get("streams", []))


def media_kind(path: str) -> str:
    """Return 'video' | 'music' | 'file' for choosing the Telegram send method."""
    info = _ffprobe(path)
    if any(_is_real_video(st) for st in info.get("streams", [])):
        return "video"
    if any(st.get("codec_type") == "audio" for st in info.get("streams", [])):
        return "music"
    return "file"


def audio_ext(path: str) -> str:
    """Correct file extension from the real format/codec (unshackle may misname mp3
    as .mp4). So Telegram treats it as the right audio type."""
    info = _ffprobe(path)
    fmt = (info.get("format", {}) or {}).get("format_name", "")
    acodec = next((st.get("codec_name") for st in info.get("streams", [])
                   if st.get("codec_type") == "audio"), "")
    by_codec = {"mp3": ".mp3", "aac": ".m4a", "opus": ".opus", "flac": ".flac",
                "vorbis": ".ogg", "alac": ".m4a", "ac3": ".ac3"}
    if "mp3" in fmt:
        return ".mp3"
    return by_codec.get(acodec, os.path.splitext(path)[1] or ".m4a")


def extract_cover(path: str, out_path: str) -> str | None:
    """Extract embedded cover art (attached_pic OR an mjpeg/png image stream)."""
    info = _ffprobe(path)
    has_cover = any(
        (st.get("disposition", {}) or {}).get("attached_pic")
        or (st.get("codec_type") == "video" and st.get("codec_name") in IMAGE_CODECS)
        for st in info.get("streams", [])
    )
    if not has_cover:
        return None
    try:
        # -map the image stream, write a jpeg
        r = subprocess.run(["ffmpeg", "-y", "-i", path, "-an", "-map", "0:v:0", out_path],
                           capture_output=True, timeout=120)
        return out_path if r.returncode == 0 and os.path.exists(out_path) else None
    except Exception:
        return None


def _read_tags(src: str) -> dict:
    """All tags (audio-stream then format-level) from a file path OR a remote URL."""
    info = _ffprobe(src)
    tags = {}
    for st in info.get("streams", []):
        if st.get("codec_type") == "audio":
            tags.update(st.get("tags") or {})
    tags.update((info.get("format", {}) or {}).get("tags") or {})
    return tags


def build_caption(path: str, service_name: str = "", source_url: str = "", media_url: str = "",
                  lang: str = "en") -> str:
    """Return an HTML caption (music / video template), localized to `lang`.
    media_url (the direct source file) is used to ENRICH music tags that the
    .mka remux dropped (artist/album/genre/composer) - ffprobe reads them from
    the source without a full download."""
    info = _ffprobe(path)
    fmt = info.get("format", {})
    tags = _read_tags(path)
    if media_url:                       # fill any missing tags from the original source
        for k, v in _read_tags(media_url).items():
            tags.setdefault(k, v)
    size = int(fmt.get("size") or (os.path.getsize(path) if os.path.exists(path) else 0))
    dur = _dur(fmt.get("duration"))
    bitrate = int((fmt.get("bit_rate") or 0)) // 1000
    src = f'<a href="{source_url}"><b>{service_name or "Source"}</b></a>' if source_url else f"<b>{service_name}</b>"

    if is_music(info):
        title = _expand_se(_tag(tags, "title") or os.path.splitext(os.path.basename(path))[0], lang)
        artist = _tag(tags, "artist", "album_artist")
        album = _tag(tags, "album")
        track = _tag(tags, "track")            # may be "8" or "8/17"
        total = _tag(tags, "tracktotal", "totaltracks")
        if total and "/" not in track:
            track = f"{track}/{total}"
        genre = _tag(tags, "genre")
        composer = _tag(tags, "composer")
        lines = [f"<b>🎵 {title}</b>"]
        if artist:
            lines.append(f"<b>👤 {artist}</b>")
        lines.append("")
        if album:
            lines.append(f"<b>💿 </b>{tr('CAP_ALBUM', lang)}: <b>{album}</b>")
        if track:
            lines.append(f"📑 {tr('CAP_TRACK', lang)}: <b>{track}</b>")
        if genre:
            lines.append(f"🎼 {tr('CAP_GENRE', lang)}: <b>{genre}</b>")
        lines.append(f"\n<blockquote expandable>⏱️ {tr('CAP_DURATION', lang)}: <code>{dur}</code>\n"
                     f"🔊 {tr('CAP_BITRATE', lang)}: <code>{bitrate}kbps</code>\n"
                     f"💾 {tr('CAP_SIZE', lang)}: <code>{size / 1024 / 1024:.2f} MiB</code></blockquote>")
        if composer:
            lines.append(f"✍️ {tr('CAP_COMPOSER', lang)}: <b>{composer}</b>")
        lines.append(f"\n🔗 {tr('CAP_DOWNLOADED_FROM', lang)} {src}")
        return "\n".join(lines)

    # ---- video ----
    title = _expand_se(_tag(tags, "title") or os.path.splitext(os.path.basename(path))[0], lang)
    date = _tag(tags, "creation_time", "date")
    w = h = 0
    for st in info.get("streams", []):
        if st.get("codec_type") == "video":
            w, h = st.get("width") or 0, st.get("height") or 0
            break
    quality = f"{h}p" if h else "?"
    lines = [f"🎬 <b>{title}</b>"]
    if date:
        lines.append(f"📅 <code>{date}</code>")
    lines.append(f"⏱️ <b>{tr('CAP_DURATION', lang)}:</b> {dur}")
    lines.append(f"🎥 <b>{tr('CAP_QUALITY', lang)}:</b> {quality} ({w}x{h})")
    lines.append(f"📦 <b>{tr('CAP_SIZE', lang)}:</b> {round(size / 1024 / 1024)}MB")
    if source_url:
        lines.append(f"\n🔗 {src}")
    return "\n".join(lines)
