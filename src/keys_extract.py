"""Format a "keys only" job result into shareable text + JSON.

The engine runs these jobs with skip_dl + export: no media is written, but the license is
still requested so the content keys come back. The serve API surfaces the export document
(manifest URLs + per-track content keys) on the job under `keys_export`. This module turns that
v2 export doc into the two outputs the bot sends: a human-readable text block and a compact
JSON ({title: {manifest, keys}}).

It only reshapes data the engine already produced - no network, no parsing of stdout."""
import json
from typing import Any, Optional


def _display_name(meta: dict) -> str:
    """A title label from the export meta: 'Name (Year)' for movies, 'Series S01E02 - Name'
    for episodes, falling back to whatever name is present."""
    meta = meta or {}
    if meta.get("type") == "episode":
        series = meta.get("series_title") or meta.get("name") or "Download"
        out = series
        season, number = meta.get("season"), meta.get("number")
        if season is not None and number is not None:
            try:
                out += f" S{int(season):02d}E{int(number):02d}"
            except (TypeError, ValueError):
                pass
        ep = meta.get("name")
        if ep and ep != series:
            out += f" - {ep}"
        return out
    name = meta.get("name") or meta.get("series_title") or "Download"
    year = meta.get("year")
    return f"{name} ({year})" if year else str(name)


def _track_label(track: dict, manifest_type: Optional[str]) -> str:
    """A short descriptor for a track: '<range> <codec> <type> - <kbps> kbps'.
    Only fields the export actually carries; SDR is implied and left off."""
    parts: list[str] = []
    rng = track.get("range")
    if rng and str(rng).upper() != "SDR":
        parts.append(str(rng))
    if track.get("codec"):
        parts.append(str(track["codec"]))
    mtype = manifest_type or track.get("descriptor")
    if mtype:
        parts.append(str(mtype))
    label = " ".join(parts) or str(track.get("type") or "track")
    bitrate = track.get("bitrate")          # DASH bandwidth / HLS bitrate, in bits per second
    if bitrate:
        try:
            label += f" - {round(int(bitrate) / 1000)} kbps"
        except (TypeError, ValueError):
            pass
    return label


def parse_export(doc: dict) -> list[dict]:
    """Flatten a v2 export document into per-title entries:
        {name, service, manifest, keys (merged), tracks: [{label, url, keys}]}.
    Titles and tracks that carry no content keys are dropped (nothing to report)."""
    if not isinstance(doc, dict):
        return []
    service = doc.get("service") or ""
    entries: list[dict] = []
    for tinfo in (doc.get("titles") or {}).values():
        if not isinstance(tinfo, dict):
            continue
        manifest = tinfo.get("manifest_url")
        if not manifest:
            urls = tinfo.get("manifest_urls") or []
            manifest = urls[0] if urls else None
        mtype = tinfo.get("manifest_type")
        merged: dict[str, str] = {}
        tracks_out: list[dict] = []
        for track in (tinfo.get("tracks") or {}).values():
            if not isinstance(track, dict):
                continue
            keys = {str(k): str(v) for k, v in (track.get("keys") or {}).items()}
            if not keys:
                continue
            merged.update(keys)
            tracks_out.append({
                "label": _track_label(track, mtype),
                "url": track.get("url") or manifest,
                "keys": keys,
            })
        if not merged:
            continue
        entries.append({
            "name": _display_name(tinfo.get("meta") or {}),
            "service": service,
            "manifest": manifest,
            "keys": merged,
            "tracks": tracks_out,
        })
    return entries


def format_text(entries: list[dict]) -> str:
    """Render entries as the shareable text block: a title header, then per track a
    '<label>: <url>' line followed by its 'kid:key' lines."""
    blocks: list[str] = []
    for e in entries:
        head = e["name"]
        if e.get("service"):
            head += f" {e['service']}"
        lines = [head]
        for t in e["tracks"]:
            lines.append("")
            url = t.get("url") or e.get("manifest") or ""
            lines.append(f"{t['label']}: {url}" if url else t["label"])
            lines.append("")
            lines.extend(f"{kid}:{key}" for kid, key in t["keys"].items())
        blocks.append("\n".join(lines).rstrip())
    return "\n\n\n".join(blocks)


def format_json(entries: list[dict]) -> dict:
    """Render entries as the compact catalog: {title: {manifest, keys}}.
    Duplicate titles get a numeric suffix so none are silently dropped."""
    out: dict[str, Any] = {}
    for e in entries:
        name = e["name"]
        if name in out:
            i = 2
            while f"{name} ({i})" in out:
                i += 1
            name = f"{name} ({i})"
        out[name] = {"manifest": e.get("manifest"), "keys": e["keys"]}
    return out


def format_json_str(entries: list[dict]) -> str:
    return json.dumps(format_json(entries), indent=4, ensure_ascii=False)
