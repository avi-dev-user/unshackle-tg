# ============================================================================
# JSON - generic catalog importer.
#
# Downloads from a normalized "catalog" file: each title carries a manifest URL and its
# content keys, so no license server is contacted. The catalog is the export-v2 shape the
# bot produces from an arbitrary provider dump:
#
#   {"version": 2, "service": "FREETV", "region": "IL",
#    "titles": {"<id>": {"meta": {...}, "manifest_url": "...", "manifest_type": "DASH",
#                        "tracks": {"1": {"keys": {"<kid_hex>": "<key_hex>"}}}}}}
#
#   unshackle dl --list JSON /path/to/catalog.json     ← list titles
#   unshackle dl JSON /path/to/catalog.json -w S05E01  ← download one
# ============================================================================

import json
from pathlib import Path
from uuid import UUID

import click

from unshackle.core.config import config
from unshackle.core.manifests import DASH, HLS, ISM
from unshackle.core.remote_service import RemoteService, _build_title
from unshackle.core.service import Service
from unshackle.core.titles import Episode, Movies, Series
from unshackle.core.tracks import Audio, Chapters, Tracks, Video

PARSERS = {"DASH": DASH, "HLS": HLS, "ISM": ISM}


class JSON(Service):
    """Reconstruct a download from a normalized catalog JSON (manifest + provided keys)."""

    ALIASES = ("CATALOG",)
    GEOFENCE = ()

    @staticmethod
    @click.command(name="JSON", short_help="Download from a normalized catalog export JSON.")
    @click.argument("title", type=str)  # path to the catalog JSON (passed as the title id)
    @click.pass_context
    def cli(ctx, **kwargs):
        return JSON(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        path = Path(title)
        if not path.is_file():
            raise click.ClickException(f"Catalog JSON not found: {path}")
        self.data = json.loads(path.read_text(encoding="utf8"))
        self.titles_data = self.data.get("titles", {})
        self.display_tag = self.data.get("service") or "JSON"
        region = self.data.get("region")
        if region:
            # Route the manifest fetch through that region's configured proxy, like any
            # geofenced service. Segments still honour --no-proxy-download.
            self.GEOFENCE = (region,)
        # Keys come from the catalog, so tell the core to skip licensing and use them directly.
        self._server_cdm = True
        self._server_cdm_type = "widevine"
        self._title_keys: dict[UUID, str] = {}
        # Maps the (sanitised, >=4-char) id handed to the core back to the original catalog key,
        # so get_tracks still finds the entry after padding. Populated by get_titles.
        self._id_map: dict[str, str] = {}
        super().__init__(ctx)

    def get_titles(self):
        # This is the single point where every catalog title becomes an unshackle Title, and
        # unshackle rejects any id shorter than 4 chars ("clash likely"). A catalog can reach us
        # with a short id ("1") or none (then the key is a loop index) - from an older/external
        # export, not just our normalizer - so enforce the >=4 invariant HERE, at the consumer,
        # and remember the original key for get_tracks. (catalog.normalize_catalog pads new
        # catalogs too, but this guard covers any source.)
        items = []
        self._id_map = {}
        for tid, entry in self.titles_data.items():
            meta = dict(entry.get("meta", {}))
            raw_id = str(meta.get("id") or tid)
            safe_id = raw_id if len(raw_id) >= 4 else raw_id.rjust(4, "0")
            base, n = safe_id, 1
            while safe_id in self._id_map:          # guard padding-induced collisions
                safe_id = f"{base}-{n}"
                n += 1
            meta["id"] = safe_id
            self._id_map[safe_id] = tid
            items.append(_build_title(meta, self.display_tag, fallback_id=safe_id))
        return Series(items) if items and isinstance(items[0], Episode) else Movies(items)

    def get_tracks(self, title) -> Tracks:
        # title.id was padded in get_titles; map it back to the real catalog key.
        tid = self._id_map.get(str(title.id), str(title.id))
        entry = self.titles_data.get(tid, {})
        # A title may carry one manifest (manifest_url) or several (manifest_urls) - e.g. Amazon
        # splits a feature into separate video and audio DASH manifests. Parse each and merge the
        # tracks; warn_only skips any id collisions between manifests instead of crashing.
        manifest_urls = entry.get("manifest_urls") or (
            [entry["manifest_url"]] if entry.get("manifest_url") else [])
        if not manifest_urls:
            raise click.ClickException(f"No manifest URL in catalog for '{title}'.")
        # Pick the decryptor per title: shaka is preferred, but it SIGSEGVs on Smooth/PIFF (.ism)
        # content, so route those to mp4decrypt. An explicit catalog "decryptor" overrides. Each
        # job runs in its own process, so setting it here stays isolated to this download.
        decryptor = self.data.get("decryptor")
        if not decryptor and any(".ism" in u.lower() for u in manifest_urls):
            decryptor = "mp4decrypt"
        if decryptor:
            config.decryption = decryptor
        parser = PARSERS.get((entry.get("manifest_type") or "DASH").upper(), DASH)
        tracks = Tracks()
        for murl in manifest_urls:
            tracks.add(parser.from_url(murl, self.session).to_tracks(language=title.language),
                       warn_only=True)
        # Stash only THIS title's keys. Injecting the whole catalog's keys into one track makes
        # the decrypter choke (mp4decrypt/shaka get hundreds of --key args), so keep it per-title.
        self._title_keys = {
            UUID(hex=kid): key
            for kid, key in ((entry.get("tracks") or {}).get("1", {}).get("keys") or {}).items()
        }
        return tracks

    def resolve_server_keys(self, title) -> None:
        """Inject this title's keys into its encrypted tracks by KID (no network). Called by dl.py
        after track selection; a stub DRM holds the keys and the manifest downloader preserves it."""
        encrypted = [t for t in title.tracks if isinstance(t, (Video, Audio)) and self._is_encrypted(t)]
        if encrypted and not self._title_keys:
            # The manifest is DRM-protected but the catalog carries no key for it. A CDM can't
            # help (the catalog has no license server), so fail clearly instead of muxing a
            # still-encrypted, unplayable file.
            raise click.ClickException(f"'{title}' is DRM-protected but the catalog has no decryption key for it.")
        if not self._title_keys:
            return  # unencrypted content - nothing to inject
        kid_hexes = [kid.hex for kid in self._title_keys]
        for track in encrypted:
            drm_obj = track.drm[0] if track.drm else RemoteService._create_drm_stub("widevine", kid_hexes)
            for kid, key in self._title_keys.items():
                drm_obj.content_keys[kid] = key
            track.drm = [drm_obj]

    @staticmethod
    def _is_encrypted(track) -> bool:
        if track.drm:
            return True
        dash = track.data.get("dash") if getattr(track, "data", None) else None
        if dash:
            for element in (dash.get("representation"), dash.get("adaptation_set")):
                if element is not None and element.findall("ContentProtection"):
                    return True
        return False

    def get_chapters(self, title):
        return Chapters()

    # NOTE: get_widevine_license is deliberately NOT overridden. Keys come from the catalog
    # (server_cdm mode), so the core never licenses - and leaving the base method in place keeps
    # /services reporting has_drm=False, so the bot skips the (unneeded) CDM-selection step.
