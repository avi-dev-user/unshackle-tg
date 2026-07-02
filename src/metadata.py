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


def _human_size(num_bytes: int) -> str:
    """Human file size: GiB once >= 1 GiB (e.g. '1.31 GiB'), else MB."""
    gib = num_bytes / 1024 / 1024 / 1024
    if gib >= 1:
        return f"{gib:.2f} GiB"
    return f"{round(num_bytes / 1024 / 1024)} MB"


_VCODEC = {"h264": "H.264", "avc1": "H.264", "hevc": "H.265", "h265": "H.265",
           "av1": "AV1", "vp9": "VP9", "vp09": "VP9", "mpeg2video": "MPEG-2"}
_ACODEC = {"aac": "AAC", "eac3": "EAC3", "ac3": "AC3", "opus": "Opus", "mp3": "MP3",
           "flac": "FLAC", "vorbis": "Vorbis", "dts": "DTS", "truehd": "TrueHD"}


def _codec_label(name: str, table: dict) -> str:
    n = (name or "").lower()
    return table.get(n, name.upper() if name else "")


def _channels_label(n) -> str:
    return {1: "Mono", 2: "Stereo", 6: "5.1", 7: "6.1", 8: "7.1"}.get(int(n or 0), f"{int(n)}ch" if n else "")


def _fps_label(st: dict) -> str:
    """Frame rate from ffprobe's 'num/den' string, rounded to a friendly value (24/25/30/50/60)."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        val = st.get(key) or ""
        if "/" in val:
            num, den = val.split("/", 1)
            try:
                f = float(num) / float(den) if float(den) else 0
            except (ValueError, ZeroDivisionError):
                f = 0
            if f > 0:
                return f"{f:.3f}".rstrip("0").rstrip(".") if abs(f - round(f)) > 0.05 else str(round(f))
    return ""


def _hdr_label(st: dict) -> str:
    """Dynamic range from the video stream's colour transfer (only flag HDR, SDR is the norm)."""
    tr_ = (st.get("color_transfer") or "").lower()
    if tr_ == "smpte2084":
        return "HDR10"
    if tr_ == "arib-std-b67":
        return "HLG"
    return ""


def _stream_langs(info: dict, codec_type: str) -> list:
    """Distinct language labels of the file's audio/subtitle streams (from ffprobe tags)."""
    langs = []
    for st in info.get("streams", []):
        if st.get("codec_type") == codec_type:
            la = (st.get("tags") or {}).get("language") or ""
            la = la.strip()
            if la and la.lower() not in ("und", "") and la not in langs:
                langs.append(la)
    return langs


def media_details_block(path: str, lang: str = "en") -> str:
    """Return the expandable technical-details block used in media captions."""
    info = _ffprobe(path)
    fmt = info.get("format", {})
    size = int(fmt.get("size") or (os.path.getsize(path) if os.path.exists(path) else 0))
    dur = _dur(fmt.get("duration"))

    if not any(_is_real_video(st) for st in info.get("streams", [])) \
            and not any(st.get("codec_type") == "audio" for st in info.get("streams", [])):
        if not size:
            return ""
        return (f"<blockquote expandable>💾 {tr('CAP_SIZE', lang)}: "
                f"<code>{size / 1024 / 1024:.2f} MiB</code></blockquote>")

    if is_music(info):
        bitrate = int((fmt.get("bit_rate") or 0)) // 1000
        return (f"<blockquote expandable>⏱️ {tr('CAP_DURATION', lang)}: <code>{dur}</code>\n"
                f"🔊 {tr('CAP_BITRATE', lang)}: <code>{bitrate}kbps</code>\n"
                f"💾 {tr('CAP_SIZE', lang)}: <code>{size / 1024 / 1024:.2f} MiB</code></blockquote>")

    w = h = 0
    vcodec = hdr = fps = ""
    for st in info.get("streams", []):
        if _is_real_video(st):
            w, h = st.get("width") or 0, st.get("height") or 0
            vcodec = _codec_label(st.get("codec_name"), _VCODEC)
            hdr = _hdr_label(st)
            fps = _fps_label(st)
            break
    acodec = ""
    for st in info.get("streams", []):
        if st.get("codec_type") == "audio":
            acodec = " ".join(p for p in (_codec_label(st.get("codec_name"), _ACODEC),
                                          _channels_label(st.get("channels"))) if p)
            break
    quality = " · ".join(p for p in (f"{h}p" if h else "?", hdr, vcodec,
                                     (f"{fps}fps" if fps else "")) if p)
    mode = tr("CAP_LANDSCAPE", lang) if w >= h else tr("CAP_PORTRAIT", lang)
    audio_langs = _stream_langs(info, "audio")
    sub_langs = _stream_langs(info, "subtitle")
    block = [
        f"<b>{tr('CAP_DURATION', lang)}: </b><code>{dur}</code>, "
        f"<b>{tr('CAP_SIZE', lang)}: </b><code>{_human_size(size)}</code>",
        f"<b>{tr('CAP_QUALITY', lang)}: </b><code>{quality}</code>, "
        f"<b>{tr('CAP_DIMENSION', lang)}: </b><code>{w}x{h}</code>, "
        f"<b>{tr('CAP_MODE', lang)}: </b><code>{mode}</code>",
    ]
    audio_bits = [b for b in (acodec, ", ".join(audio_langs)) if b]
    if audio_bits:
        line = f"🔊 <b>{tr('CAP_AUDIO', lang)}: </b><code>{' · '.join(audio_bits)}</code>"
        if sub_langs:
            line += f"  📝 <b>{tr('CAP_SUBTITLES_CAP', lang)}: </b><code>{', '.join(sub_langs)}</code>"
        block.append(line)
    return "<blockquote expandable>" + "\n".join(block) + "</blockquote>"


def build_caption(path: str, service_name: str = "", source_url: str = "", media_url: str = "",
                  lang: str = "en", display_title: str = "", description: str = "",
                  upload_date: str = "") -> str:
    """Return an HTML caption (music / video template), localized to `lang`.
    display_title / description / upload_date come from the service's title metadata (when
    available); everything in the expandable block (duration, size, dimensions, quality, audio
    /subtitle langs) is read from the downloaded file by ffprobe, so it is always accurate.
    media_url (the direct source file) is used to ENRICH music tags that the .mka remux dropped
    (artist/album/genre/composer) - ffprobe reads them from the source without a full download."""
    # Some services (e.g. an app-only service like STING) pass their internal content ID as
    # source_url, not a real page - only render the source link when it's an actual http(s) URL.
    if source_url and not source_url.startswith(("http://", "https://")):
        source_url = ""

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

    # ---- generic file (image / document / archive): no video/audio, so skip the media fields
    # (duration/dimensions/codec) that would otherwise render as 0:00 / 0x0 for e.g. a photo ----
    if not any(_is_real_video(st) for st in info.get("streams", [])) \
            and not any(st.get("codec_type") == "audio" for st in info.get("streams", [])):
        title = _expand_se(display_title or os.path.splitext(os.path.basename(path))[0], lang)
        lines = [f"<b>📄 {title}</b>", ""]
        lines.append(f"<blockquote expandable>💾 {tr('CAP_SIZE', lang)}: "
                     f"<code>{size / 1024 / 1024:.2f} MiB</code></blockquote>")
        lines.append(f"\n🔗 {tr('CAP_DOWNLOADED_FROM', lang)} {src}")
        return "\n".join(lines)

    # ---- video ----
    title = _expand_se(display_title or _tag(tags, "title") or os.path.splitext(os.path.basename(path))[0], lang)
    w = h = 0
    vcodec = hdr = fps = ""
    for st in info.get("streams", []):
        if _is_real_video(st):                       # ignore embedded cover art
            w, h = st.get("width") or 0, st.get("height") or 0
            vcodec = _codec_label(st.get("codec_name"), _VCODEC)
            hdr = _hdr_label(st)
            fps = _fps_label(st)
            break
    # main audio stream → codec + channel layout (e.g. "EAC3 5.1")
    acodec = ""
    for st in info.get("streams", []):
        if st.get("codec_type") == "audio":
            acodec = " ".join(p for p in (_codec_label(st.get("codec_name"), _ACODEC),
                                          _channels_label(st.get("channels"))) if p)
            break
    quality = " · ".join(p for p in (f"{h}p" if h else "?", hdr, vcodec,
                                     (f"{fps}fps" if fps else "")) if p)
    mode = tr("CAP_LANDSCAPE", lang) if w >= h else tr("CAP_PORTRAIT", lang)
    audio_langs = _stream_langs(info, "audio")
    sub_langs = _stream_langs(info, "subtitle")

    lines = [f"<b>🎬 {title}</b>"]
    if description:
        lines.append(f"\n<b>{description}</b>")
    if upload_date:
        lines.append(f"\n<b>{tr('CAP_UPLOAD_DATE', lang)}: </b><code>{upload_date}</code>")
    if source_url:
        lines.append(f"🔗 <a href=\"{source_url}\"><b>{tr('CAP_ORIGINAL_POST', lang)}</b></a>")

    # expandable block - all values read from the file (always accurate)
    block = [
        f"<b>{tr('CAP_DURATION', lang)}: </b><code>{dur}</code>, "
        f"<b>{tr('CAP_SIZE', lang)}: </b><code>{_human_size(size)}</code>",
        f"<b>{tr('CAP_QUALITY', lang)}: </b><code>{quality}</code>, "
        f"<b>{tr('CAP_DIMENSION', lang)}: </b><code>{w}x{h}</code>, "
        f"<b>{tr('CAP_MODE', lang)}: </b><code>{mode}</code>",
    ]
    audio_bits = [b for b in (acodec, ", ".join(audio_langs)) if b]   # "EAC3 5.1 · Hebrew, English"
    if audio_bits:
        line = f"🔊 <b>{tr('CAP_AUDIO', lang)}: </b><code>{' · '.join(audio_bits)}</code>"
        if sub_langs:
            line += f"  📝 <b>{tr('CAP_SUBTITLES_CAP', lang)}: </b><code>{', '.join(sub_langs)}</code>"
        block.append(line)
    lines.append("\n<blockquote expandable>" + "\n".join(block) + "</blockquote>")
    return "\n".join(lines)
