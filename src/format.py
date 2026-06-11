"""Display formatting helpers: progress blocks, sizes, ETA, phase and language labels.
Shared by the download and upload paths. Language-dependent bits take a `lang` argument."""
from .i18n import tr


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
                     segs_done=None, segs_total=None) -> str:
    """A tidy multi-line progress block, shared by download and upload.
    Optional stats render only when present: 📦 segments (download), 💾 size + ⚡ speed (upload),
    ⏱️ ETA (both) - keeps the bar so it stays in our visual style."""
    pct = max(0, min(100, int(pct or 0)))
    lines = [head, f"{_bar(pct)}  {pct}%"]
    extra = []
    if segs_total:
        extra.append(f"📦 {int(segs_done or 0)}/{int(segs_total)}")
    if done_b is not None and total_b:
        extra.append(f"💾 {_fmt_size(done_b)} / {_fmt_size(total_b)}")
    if speed_bps:
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


def _lang_label(code: str, lang: str = "en") -> str:
    base = (code or "").split("-")[0].lower()
    name = _LANG_NAMES.get(lang, _LANG_NAMES["en"]).get(base)
    return f"{name} ({code})" if name else code
