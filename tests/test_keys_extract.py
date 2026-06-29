"""Tests for the keys-only output formatting: parsing the engine's v2 export document into
the shareable text block and the compact JSON catalog."""
import json

from src import download, keys_extract

SAMPLE = {
    "version": 2,
    "service": "PCOK",
    "titles": {
        "0": {
            "meta": {"type": "movie", "name": "Strung", "year": 2026},
            "manifest_url": "https://cdn.example/master_cmaf.mpd",
            "manifest_type": "DASH",
            "tracks": {
                "v1": {
                    "type": "Video", "url": "https://cdn.example/master_cmaf.mpd",
                    "codec": "H265", "range": "DV", "bitrate": 14793000, "height": 2160,
                    "keys": {
                        "301ac05f36921b4e0d4dadc8eacd9b63": "b713ef40b5ded98238c75b11f25f9bc2",
                        "301ab4f33aab26f79c0a106bdfe5ddb9": "0bd33a58e804d9bc9211337e425f003d",
                    },
                },
                "a1": {"type": "Audio", "url": "https://cdn.example/audio", "keys": {}},
            },
        }
    },
}


def test_parse_export_basic():
    entries = keys_extract.parse_export(SAMPLE)
    assert len(entries) == 1
    e = entries[0]
    assert e["name"] == "Strung (2026)"
    assert e["service"] == "PCOK"
    assert e["manifest"] == "https://cdn.example/master_cmaf.mpd"
    assert len(e["keys"]) == 2                       # merged across tracks
    assert len(e["tracks"]) == 1                     # the keyless audio track is dropped


def test_parse_export_drops_keyless_titles():
    doc = {"service": "X", "titles": {"0": {"meta": {"name": "Nope"},
            "tracks": {"v": {"keys": {}}}}}}
    assert keys_extract.parse_export(doc) == []
    assert keys_extract.parse_export({}) == []
    assert keys_extract.parse_export(None) == []


def test_format_json_shape():
    out = keys_extract.format_json(keys_extract.parse_export(SAMPLE))
    assert list(out.keys()) == ["Strung (2026)"]
    entry = out["Strung (2026)"]
    assert entry["manifest"] == "https://cdn.example/master_cmaf.mpd"
    assert entry["keys"]["301ac05f36921b4e0d4dadc8eacd9b63"] == "b713ef40b5ded98238c75b11f25f9bc2"
    # round-trips as valid JSON
    json.loads(keys_extract.format_json_str(keys_extract.parse_export(SAMPLE)))


def test_format_text_contents():
    text = keys_extract.format_text(keys_extract.parse_export(SAMPLE))
    assert "Strung (2026) PCOK" in text
    assert "DV H265 DASH - 14793 kbps: https://cdn.example/master_cmaf.mpd" in text
    assert "301ac05f36921b4e0d4dadc8eacd9b63:b713ef40b5ded98238c75b11f25f9bc2" in text


def test_episode_naming():
    doc = {"service": "S", "titles": {"0": {
        "meta": {"type": "episode", "series_title": "Show", "season": 1, "number": 2, "name": "Pilot"},
        "manifest_url": "u", "tracks": {"v": {"keys": {"a" * 32: "b" * 32}}}}}}
    assert keys_extract.parse_export(doc)[0]["name"] == "Show S01E02 - Pilot"


def test_duplicate_titles_get_suffixed():
    doc = {"service": "S", "titles": {
        "0": {"meta": {"name": "Same"}, "manifest_url": "u1", "tracks": {"v": {"keys": {"a" * 32: "1" * 32}}}},
        "1": {"meta": {"name": "Same"}, "manifest_url": "u2", "tracks": {"v": {"keys": {"b" * 32: "2" * 32}}}},
    }}
    out = keys_extract.format_json(keys_extract.parse_export(doc))
    assert set(out.keys()) == {"Same", "Same (2)"}


def test_build_flags_keys_only():
    f, q = download.build_flags(0, "SVC", "0", ["video", "audio"], "best", keys_only=True)
    assert f["skip_dl"] is True
    assert f["export"] is True
    # normal download must not set these
    f2, _ = download.build_flags(0, "SVC", "0", ["video", "audio"], "best")
    assert "skip_dl" not in f2 and "export" not in f2
