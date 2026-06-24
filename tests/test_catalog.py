"""Tests for catalog.normalize_catalog - the parser for user-uploaded .json catalogs.
Covers the round-3 hardening: KID/key hex validation and tolerance of malformed input."""
from src import catalog


def test_list_and_dict_wrappers_equivalent():
    entry = {"id": "1", "mpd_url": "http://x/y.mpd"}
    from_list = catalog.normalize_catalog([entry])
    from_dict = catalog.normalize_catalog({"titles": [entry]})
    # short id "1" is left-padded to a 4-char key so unshackle's Title accepts it
    assert list(from_list["titles"]) == list(from_dict["titles"]) == ["0001"]
    assert from_list["version"] == 2


def test_entry_without_manifest_is_skipped():
    out = catalog.normalize_catalog([{"id": "1"}, {"id": "2", "mpd_url": "http://x/2.mpd"}])
    assert list(out["titles"]) == ["0002"]


def test_episode_vs_movie_detection():
    out = catalog.normalize_catalog([
        {"id": "e", "mpd": "http://x/e.mpd", "season": 1, "episode": 3, "series": "S"},
        {"id": "m", "mpd": "http://x/m.mpd", "title": "Film"},
    ])
    assert out["titles"]["000e"]["meta"]["type"] == "episode"
    assert out["titles"]["000e"]["meta"]["number"] == 3
    assert out["titles"]["000m"]["meta"]["type"] == "movie"


def test_manifest_type_inference():
    assert catalog._manifest_and_type({"url": "http://x/a.m3u8"})[1] == "HLS"
    assert catalog._manifest_and_type({"url": "http://x/a.mpd"})[1] == "DASH"
    assert catalog._manifest_and_type({"url": "http://x/a.ism/manifest"})[1] == "ISM"
    assert catalog._manifest_and_type({})[0] is None


def test_keys_keep_only_valid_hex():
    e = {"keys": {
        "0123456789ABCDEF0123456789abcdef": "FEDCBA9876543210fedcba9876543210",  # valid (cased)
        "00112233-4455-6677-8899-aabbccddeeff": "00112233445566778899aabbccddeeff",  # dashed kid
        "short": "deadbeef",                 # bad kid length -> dropped
        "0123456789abcdef0123456789abcdef": "nothex",  # bad key -> dropped
    }}
    keys = catalog._keys(e)
    assert keys["0123456789abcdef0123456789abcdef"] == "fedcba9876543210fedcba9876543210"
    assert "00112233445566778899aabbccddeeff" in keys     # dashes stripped, kept
    assert "short" not in keys
    assert len(keys) == 2                                  # the two bad ones dropped


def test_keys_tolerates_non_dict():
    assert catalog._keys({"keys": "not-a-dict"}) == {}
    assert catalog._keys({"keys": None}) == {}
    assert catalog._keys({}) == {}


def test_top_level_kid_key_pair():
    e = {"id": "1", "mpd": "http://x/1.mpd",
         "kid": "0123456789abcdef0123456789abcdef", "key": "fedcba9876543210fedcba9876543210"}
    out = catalog.normalize_catalog([e])
    assert out["titles"]["0001"]["tracks"]["1"]["keys"]


def test_short_ids_are_padded_to_min_length():
    # unshackle's Title raises "unique ID is not large enough" below 4 chars; normalize must
    # guarantee every key is >=4. Covers a short id, and an entry with NO id (falls back to the
    # loop index, which is also too short) - the exact case that crashed list-titles.
    out = catalog.normalize_catalog([
        {"id": "12", "mpd": "http://x/a.mpd"},          # short id  -> padded
        {"mpd": "http://x/b.mpd"},                      # no id, index 1 -> "0001"
    ])
    keys = list(out["titles"])
    assert keys == ["0012", "0001"]
    assert all(len(k) >= 4 for k in keys)
    # a normal long id is left untouched
    long_out = catalog.normalize_catalog([{"id": "abcd1234", "mpd": "http://x/c.mpd"}])
    assert list(long_out["titles"]) == ["abcd1234"]


def test_padding_collision_stays_unique():
    # one explicit "0001" plus a short "1" that pads to the same key must not clobber each other
    out = catalog.normalize_catalog([
        {"id": "0001", "mpd": "http://x/a.mpd"},
        {"id": "1", "mpd": "http://x/b.mpd"},
    ])
    keys = list(out["titles"])
    assert keys == ["0001", "0001-1"]
    assert len(set(keys)) == 2


def test_non_list_non_dict_input_is_empty():
    assert catalog.normalize_catalog("garbage")["titles"] == {}
    assert catalog.normalize_catalog(None)["titles"] == {}
