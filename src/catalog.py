"""Normalize an arbitrary VOD-catalog JSON into the shape the JSON engine service consumes.

A user sends the bot a .json dump from some external extractor; we don't control its exact
shape, so this is deliberately tolerant of common field-name variants. The output is the
export-v2 structure the `JSON` service reads (per title: a manifest URL + its content keys),
so downloads decrypt from the provided keys without contacting a license server.
"""
import re
from typing import Any

_HEX = re.compile(r"[0-9a-fA-F]+")


def _manifest_and_type(e: dict) -> tuple[str | None, str | None]:
    m = e.get("dash_url") or e.get("mpd_url") or e.get("mpd") or e.get("manifest_url") or e.get("url")
    if not m:
        return None, None
    u = m.lower()
    if ".m3u8" in u:
        return m, "HLS"
    if "/dash" in u or ".mpd" in u:
        return m, "DASH"
    if ".ism" in u:
        return m, "ISM"
    return m, "DASH"


def _put_key(out: dict, kid, key) -> None:
    """Accept only a 32-hex KID -> hex CONTENT-KEY pair (these are passed to mp4decrypt as
    args, so reject anything that isn't clean hex from the untrusted catalog file)."""
    k = str(kid).replace("-", "").strip().lower()
    v = str(key).strip().lower()
    if len(k) == 32 and _HEX.fullmatch(k) and v and _HEX.fullmatch(v):
        out[k] = v


def _keys(e: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    raw = e.get("decryption_keys") or e.get("keys") or {}
    if isinstance(raw, dict):                       # tolerate non-dict 'keys' without crashing
        for kid, key in raw.items():
            _put_key(out, kid, key)
    if e.get("kid") and e.get("key"):
        _put_key(out, e["kid"], e["key"])
    return out


def normalize_catalog(raw: Any, service: str = "FREETV", region: str = "IL", language: str = "he") -> dict:
    """Convert a raw catalog (a list of entries, or a dict wrapping one) into export v2.

    `service` becomes the display tag (output filename), `region` drives the manifest-fetch
    geofence (routed through that region's configured proxy), `language` is the fallback track
    language. Per-entry `language` overrides the default.
    """
    if isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        entries = raw.get("titles") or raw.get("episodes") or raw.get("items") or raw.get("results") or [raw]
    else:
        entries = []

    titles: dict[str, dict] = {}
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        manifest, mtype = _manifest_and_type(e)
        if not manifest:
            continue
        # unshackle's Title rejects any id shorter than 4 chars ("clash likely"), and a catalog
        # entry may carry a short id ("1") or none at all (then it falls back to the loop index
        # "0"/"1"/...). Left-pad short ids to 4 chars so every downstream title is valid; the id
        # is opaque (it only keys this catalog), so padding it is safe.
        tid = str(e.get("episode_id") or e.get("id") or e.get("guid") or i)
        if len(tid) < 4:
            tid = tid.rjust(4, "0")
            if tid in titles:           # only de-dupe collisions introduced by the padding
                tid = f"{tid}-{i}"
        if e.get("episode") is not None or e.get("season") is not None or e.get("series") or e.get("series_title"):
            meta = {
                "type": "episode",
                "series_title": e.get("series") or e.get("series_title") or service,
                "season": e.get("season") or 0,
                "number": e.get("episode") or e.get("number") or 0,
                "name": e.get("title") or e.get("name"),
                "language": e.get("language") or language,
            }
        else:
            meta = {
                "type": "movie",
                "name": e.get("title") or e.get("name") or tid,
                "language": e.get("language") or language,
            }
        titles[tid] = {
            "meta": meta,
            "manifest_url": manifest,
            "manifest_type": mtype,
            "tracks": {"1": {"keys": _keys(e)}},
        }

    out = {"version": 2, "service": service, "region": region, "titles": titles}
    # The JSON service auto-picks the decryptor per title from the manifest (Smooth/.ism ->
    # mp4decrypt, else shaka). Only pass through an explicit override if the raw catalog set one.
    if isinstance(raw, dict) and raw.get("decryptor"):
        out["decryptor"] = raw["decryptor"]
    return out
