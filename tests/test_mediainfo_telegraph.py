"""Tests for the MediaInfo -> telegra.ph feature's pure, contract-bearing pieces:
- telegraph._paginate: the 64 KB page-split (a season of reports must never overflow a page).
- metadata.mediainfo_report: the 'Complete name' server-path is stripped to the bare filename
  before a public report is published (no directory-layout leak).
- download._format_mediainfo_links: one vs several page links render as a single HTML line.
"""
import json

from src import metadata, telegraph


def _page_bytes(nodes: list) -> int:
    return len(json.dumps(nodes, ensure_ascii=False).encode("utf-8"))


def test_paginate_small_report_is_one_page():
    pages = telegraph._paginate([("file.mkv", "General\nFormat : Matroska\n")])
    assert len(pages) == 1
    # heading node + pre node
    assert pages[0][0]["tag"] == "h4"
    assert pages[0][1]["tag"] == "pre"


def test_paginate_splits_a_season_across_pages_each_under_cap():
    # 20 files, each ~6 KB (a realistic season pack) -> must split, and every page stays under 64 KB.
    body = "General\n" + ("Bit rate : 3 466 kb/s\n" * 250)   # ~6 KB
    assert 5000 < len(body.encode()) < 7000
    sections = [(f"S01E{i:02d}.mkv", body) for i in range(1, 21)]
    pages = telegraph._paginate(sections)
    assert len(pages) >= 2                                   # 20 * 6 KB = 120 KB -> at least 2 pages
    for page in pages:
        assert _page_bytes(page) <= 64_000                  # Telegraph's hard cap is never exceeded


def test_paginate_truncates_a_single_oversize_section():
    huge = "x" * 200_000                                     # one section bigger than a whole page
    pages = telegraph._paginate([("huge.mkv", huge)])
    assert len(pages) == 1
    assert _page_bytes(pages[0]) <= 64_000
    assert "truncated" in pages[0][1]["children"][0]


def test_paginate_empty_input_is_no_pages():
    assert telegraph._paginate([]) == []


def test_mediainfo_report_strips_server_path(monkeypatch):
    # mediainfo prints the file's ABSOLUTE server path - it must be replaced with the bare
    # filename so a public telegra.ph page never leaks the server's directory layout.
    raw = ("General\n"
           "Complete name                            : /data/temp/job42/Secret.Show.S01E01.mkv\n"
           "Format                                   : Matroska\n")

    class _R:
        stdout = raw

    monkeypatch.setattr(metadata.subprocess, "run", lambda *a, **k: _R())
    out = metadata.mediainfo_report("/data/temp/job42/Secret.Show.S01E01.mkv")
    assert "/data/temp/job42" not in out
    assert "Complete name                            : Secret.Show.S01E01.mkv" in out
    assert "Format                                   : Matroska" in out    # other lines untouched


def test_mediainfo_report_filename_with_backslash_does_not_raise(monkeypatch):
    # the filename goes into the regex REPLACEMENT - a backslash in it must not be read as an
    # escape (which would raise re.error, breaking the never-raises / always-delete contract).
    raw = "General\nComplete name                            : /data/temp/j/We\\2ird.mkv\n"

    class _R:
        stdout = raw

    monkeypatch.setattr(metadata.subprocess, "run", lambda *a, **k: _R())
    out = metadata.mediainfo_report("/data/temp/j/We\\2ird.mkv")
    assert "/data/temp/j" not in out
    assert "We\\2ird.mkv" in out


def test_mediainfo_report_empty_output_is_blank(monkeypatch):
    class _R:
        stdout = "   \n"

    monkeypatch.setattr(metadata.subprocess, "run", lambda *a, **k: _R())
    assert metadata.mediainfo_report("/x/y.mkv") == ""


def test_mediainfo_report_missing_binary_is_blank(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("mediainfo")

    monkeypatch.setattr(metadata.subprocess, "run", _boom)
    assert metadata.mediainfo_report("/x/y.mkv") == ""


def test_format_mediainfo_links_single_and_multiple():
    from src import download
    one = download._format_mediainfo_links(["https://telegra.ph/x-01"], "en")
    assert one.count("<a ") == 1 and "telegra.ph/x-01" in one
    many = download._format_mediainfo_links(
        ["https://telegra.ph/x-01", "https://telegra.ph/x-02"], "en")
    assert many.count("<a ") == 2
    assert download._format_mediainfo_links([], "en") == ""
