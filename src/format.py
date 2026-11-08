"""Display formatting helpers: progress blocks, sizes, ETA, phase and language labels.
Shared by the download and upload paths. Language-dependent bits take a `lang` argument."""
from .i18n import tr


def flatten_tracks(tracks) -> dict:
    """Normalise a list-tracks reply to one {video, audio, subtitles, title} view.

    One episode returns {video, audio, subtitles, title}. A whole season or series returns
    {"episodes": [{title, video, audio, subtitles}, ...]} and has no top-level track lists, so
    reading it the single-episode way finds nothing and the wizard says "no tracks". For a
    multi-episode reply the track lists are merged, de-duplicated by (language, codec, height),
    so the pickers show what the episodes offer. `title` is the first episode's.
    """
    if not isinstance(tracks, dict):
        return {"video": [], "audio": [], "subtitles": [], "title": {}}
    eps = tracks.get("episodes")
    if not eps:
        return tracks
    out = {"video": [], "audio": [], "subtitles": [], "title": {}}
    seen = {"video": set(), "audio": set(), "subtitles": set()}
    for ep in eps:
        if not isinstance(ep, dict):
            continue
        if not out["title"] and isinstance(ep.get("title"), dict):
            out["title"] = ep["title"]
        for kind in ("video", "audio", "subtitles"):
            for t in ep.get(kind) or []:
                key = (t.get("language"), t.get("codec"), t.get("height"), t.get("bitrate"))
                if key not in seen[kind]:
                    seen[kind].add(key)
                    out[kind].append(t)
    return out


def _bar(pct, width: int = 14) -> str:
    pct = max(0, min(100, int(pct or 0)))
    filled = round(pct * width / 100)
    return "█" * filled + "░" * (width - filled)


def _fmt_size(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} GB"


def _fmt_eta(secs, lang: str = "en") -> str:
    secs = int(max(0, secs or 0))
    if secs < 60:
        return tr("ETA_SEC", lang).format(n=secs)
    m, s = divmod(secs, 60)
    if m < 60:
        return tr("ETA_MIN", lang).format(t=f"{m}:{s:02d}")
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _render_progress(*, head: str, pct, done_b=None, total_b=None, speed_bps=None, eta=None,
                     segs_done=None, segs_total=None, speed_str=None) -> str:
    """A tidy multi-line progress block, shared by download and upload.
    Optional stats render only when present: 📦 segments + ⚡ speed (download), 💾 size + ⚡ speed
    (upload), ⏱️ ETA (both) - keeps the bar so it stays in our visual style."""
    pct = max(0, min(100, int(pct or 0)))
    lines = [head, f"{_bar(pct)}  {pct}%"]
    extra = []
    if segs_total:
        extra.append(f"📦 {int(segs_done or 0)}/{int(segs_total)}")
    if done_b is not None and total_b:
        extra.append(f"💾 {_fmt_size(done_b)} / {_fmt_size(total_b)}")
    if speed_str:                                # download: unshackle already formats it ("3.2 MB/s")
        extra.append(f"⚡ {speed_str}")
    elif speed_bps:                              # upload: bytes/sec we format ourselves
        extra.append(f"⚡ {_fmt_size(speed_bps)}/s")
    if eta is not None and eta != "":
        extra.append(f"⏱️ {eta}")
    if extra:
        lines.append(" · ".join(extra))
    return "\n".join(lines)


def _phase(phase: str, lang: str = "en") -> str:
    """The engine's phase ('downloading video 1080p') for display. English as-is; Hebrew translated."""
    if lang != "he":
        return phase or "downloading"
    if not phase:
        return "מוריד"
    return (phase.replace("downloading video", "מוריד וידאו")
                 .replace("downloading audio", "מוריד אודיו")
                 .replace("downloading subtitle", "מוריד כתוביות")
                 .replace("downloading", "מוריד")
                 .replace("muxing", "ממזג").replace("merging", "ממזג"))


# subtitle/language code -> display name, per UI language (fallback: the raw code)
_LANG_NAMES = {
    "en": {"he": "Hebrew", "iw": "Hebrew", "en": "English", "ar": "Arabic", "ru": "Russian",
           "fr": "French", "es": "Spanish", "de": "German", "und": "Original"},
    "he": {"he": "עברית", "iw": "עברית", "en": "אנגלית", "ar": "ערבית", "ru": "רוסית",
           "fr": "צרפתית", "es": "ספרדית", "de": "גרמנית", "und": "מקורי"},
}


def _lang_label(code, lang: str = "en") -> str:
    # unshackle reports skipped subtitles as {"id", "language", "title"} dicts, not bare codes
    if isinstance(code, dict):
        code = code.get("language") or code.get("id") or ""
    code = str(code or "")
    base = code.split("-")[0].lower()
    name = _LANG_NAMES.get(lang, _LANG_NAMES["en"]).get(base)
    return f"{name} ({code})" if name else code
