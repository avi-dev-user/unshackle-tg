"""flatten_tracks: a season reply ({"episodes": [...]}) must not look like "no tracks"."""
from src.format import flatten_tracks


def _t(lang, codec="x", height=None, bitrate=1):
    return {"language": lang, "codec": codec, "height": height, "bitrate": bitrate}


def _ep(n, video, audio, subs):
    return {"title": {"season": 1, "number": n, "name": "S"}, "video": video, "audio": audio, "subtitles": subs}


def test_single_episode_reply_is_unchanged():
    one = {"video": [_t("he", height=1080)], "audio": [_t("he")], "subtitles": [], "title": {"name": "A"}}
    assert flatten_tracks(one) is one


def test_season_reply_is_merged_and_deduplicated():
    eps = [_ep(n, [_t("he", height=1080), _t("he", height=576)], [_t("he"), _t("en")], [_t("he"), _t("ru")])
           for n in (1, 2, 3)]
    out = flatten_tracks({"episodes": eps})
    assert len(out["video"]) == 2 and len(out["audio"]) == 2 and len(out["subtitles"]) == 2
    assert out["title"]["number"] == 1


def test_season_with_different_languages_per_episode_keeps_all():
    eps = [_ep(1, [], [_t("he")], []), _ep(2, [], [_t("en")], [_t("ru")])]
    out = flatten_tracks({"episodes": eps})
    assert {a["language"] for a in out["audio"]} == {"he", "en"}
    assert len(out["subtitles"]) == 1


def test_bad_input_does_not_crash():
    assert flatten_tracks(None)["video"] == []
    assert flatten_tracks({"episodes": [None, "x"]})["audio"] == []
    assert flatten_tracks({}) == {}                 # not a season reply: passed through untouched
    assert flatten_tracks({"episodes": []}) == {"episodes": []}
