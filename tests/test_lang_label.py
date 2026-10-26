"""_lang_label must accept both a bare code and the dict unshackle reports for skipped subtitles."""
from src.format import _lang_label


def test_bare_code():
    assert _lang_label("he", "en") == "Hebrew (he)"
    assert _lang_label("en-US", "he") == "אנגלית (en-US)"
    assert _lang_label("xx", "en") == "xx"          # unknown code falls back to the raw code


def test_dict_from_skipped_subtitles():
    # shape produced by unshackle SkippedSubtitle: {"id", "language", "title"}
    item = {"id": "abc", "language": "he", "title": "Some Movie"}
    assert _lang_label(item, "en") == "Hebrew (he)"


def test_dict_without_language_falls_back_to_id():
    assert _lang_label({"id": "zz"}, "en") == "zz"


def test_empty_and_none_do_not_crash():
    assert _lang_label("", "en") == ""
    assert _lang_label(None, "en") == ""
    assert _lang_label({}, "en") == ""
