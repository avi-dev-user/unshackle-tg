"""Pure-logic tests: download flag-building / track-selection, and the data-driven catalog
categorisation + URL routing (the genericization)."""
from src import catalog_meta, download


# --- download: track selection + flags -----------------------------------------------------
def test_to_sel_normalisation():
    assert download.to_sel(["video", "subs"]) == {"video", "subs"}
    assert download.to_sel("full") == {"video", "audio"}
    assert download.to_sel("subs") == {"subs"}
    assert download.to_sel(None) == {"video", "audio"}      # default, no crash
    assert download.to_sel([]) == {"video", "audio"}


def test_sel_label_localised():
    assert download.sel_label(["video", "audio"], "en") == "Video + Audio"
    assert "וידאו" in download.sel_label(["video"], "he")


# --- self-hosted download-link delivery ----------------------------------------------------
def test_publish_link_moves_files_and_builds_urls(tmp_path, monkeypatch):
    rec = tmp_path / "rec"
    rec.mkdir()
    monkeypatch.setattr(download, "REC_DIR", str(rec))
    monkeypatch.setattr(download, "REC_URL_BASE", "https://dl.example.test")
    out = tmp_path / "out"
    out.mkdir()
    f1 = out / "Show S01E01 [he].mkv"
    f1.write_bytes(b"x" * 10)
    f2 = out / "פרק.srt"                                  # non-ASCII name must be URL-encoded
    f2.write_text("sub")
    urls = download.publish_link([str(f1), str(f2)])
    assert not f1.exists() and not f2.exists()           # files were MOVED out of the job dir
    tokens = list(rec.iterdir())
    assert len(tokens) == 1 and tokens[0].is_dir()        # one fresh token dir holds the whole job
    assert sorted(p.name for p in tokens[0].iterdir()) == sorted(["Show S01E01 [he].mkv", "פרק.srt"])
    assert len(urls) == 2
    assert all(u.startswith(f"https://dl.example.test/{tokens[0].name}/") for u in urls)
    assert all(" " not in u for u in urls)                # spaces percent-encoded -> a valid URL
    assert any("%D7" in u for u in urls)                  # the Hebrew filename is percent-encoded


def test_publish_link_skips_missing(tmp_path, monkeypatch):
    rec = tmp_path / "rec"
    rec.mkdir()
    monkeypatch.setattr(download, "REC_DIR", str(rec))
    monkeypatch.setattr(download, "REC_URL_BASE", "https://x.test")
    f = tmp_path / "a.bin"
    f.write_bytes(b"x")
    urls = download.publish_link([str(f), str(tmp_path / "missing.bin")])
    assert len(urls) == 1 and urls[0].endswith("/a.bin")


def test_build_flags_combinations():
    f, q = download.build_flags(0, "SVC", "0", ["subs"], "best")
    assert f["subs_only"] and f["no_video"] and f["no_audio"] and f["sub_format"] == "SRT"
    assert q is None

    f, q = download.build_flags(0, "SVC", "0", ["audio"], "best")
    assert f["audio_only"] and f["no_video"] and f.get("no_mux")

    f, q = download.build_flags(0, "SVC", "0", ["video", "audio"], "1080")
    assert q == [1080] and "no_video" not in f
    assert f["no_proxy_download"] and f["skip_subtitle_errors"]   # always set

    f, q = download.build_flags(0, "SVC", "0", ["video", "audio", "subs"], "1080")
    assert q == [1080] and f["sub_format"] == "SRT"
    assert "no_subs" not in f and "no_mux" not in f

    f, q = download.build_flags(0, "SVC", "0", ["video"], "best")
    assert q is None                                             # 'best' -> no explicit quality


# --- catalog_meta: data-driven categorisation ---------------------------------------------
def test_categorise_precedence(monkeypatch):
    monkeypatch.setattr(catalog_meta.config, "CATEGORY_SEEDS", {"SEEDED": "il"})
    monkeypatch.setattr(catalog_meta.config, "FREE_SERVICES", {"FREEONE"})
    meta = {
        "DECLARED": {"category": "il"},
        "PAID": {"needs_auth": True},
        "OPEN": {},
        "SEEDED": {},
        "FREEONE": {},
    }
    monkeypatch.setattr(catalog_meta.state, "meta", lambda t: meta.get(t, {}))
    assert catalog_meta.categorise("DECLARED") == "il"     # service self-declares
    assert catalog_meta.categorise("SEEDED") == "il"       # deployment seed
    assert catalog_meta.categorise("FREEONE") == "free"    # explicit free list
    assert catalog_meta.categorise("PAID") == "sub"        # needs_auth heuristic
    assert catalog_meta.categorise("OPEN") == "free"       # default


def test_auth_required_vs_optional(monkeypatch):
    # A service can ACCEPT auth without REQUIRING it. Only subscription services are hard-gated;
    # the catch-all (yt-dlp) and free services download anonymously - cookies are a fallback.
    meta = {
        "NETFLIX": {"needs_auth": True},                 # paid -> "sub" -> mandatory
        "YT": {"needs_auth": True},                      # catch-all that accepts cookies
        "MAKO": {"needs_auth": True, "category": "il"},  # free-with-optional-login
        "OPEN": {},                                      # no auth at all
    }
    monkeypatch.setattr(catalog_meta.state, "meta", lambda t: meta.get(t, {}))
    monkeypatch.setattr(catalog_meta.config, "CATEGORY_SEEDS", {})
    monkeypatch.setattr(catalog_meta.config, "FREE_SERVICES", set())
    monkeypatch.setattr(catalog_meta.config, "CATCHALL_SERVICE", "YT")
    assert catalog_meta.svc_auth_required("NETFLIX") is True      # subscription -> hard gate
    assert catalog_meta.svc_auth_required("YT") is False          # catch-all -> never gated
    assert catalog_meta.svc_auth_required("MAKO") is False        # free/il -> optional auth
    assert catalog_meta.svc_auth_required("OPEN") is False        # no auth -> never gated


def test_detect_service_routing(monkeypatch):
    monkeypatch.setattr(catalog_meta.config, "DOMAIN_SERVICES", {"example.com": "EX"})
    monkeypatch.setattr(catalog_meta.config, "FEED_SERVICE", "POD")
    monkeypatch.setattr(catalog_meta.config, "CATCHALL_SERVICE", "YT")
    monkeypatch.setattr(catalog_meta.state, "services_cached", lambda: [])
    assert catalog_meta.detect_service("https://example.com/show") == "EX"   # domain map
    assert catalog_meta.detect_service("https://www.example.com/x") == "EX"  # www stripped
    assert catalog_meta.detect_service("https://host.tld/feed.xml") == "POD"  # feed heuristic
    assert catalog_meta.detect_service("https://unknown.tld/x") == "YT"      # catch-all
    assert catalog_meta.detect_service("not-a-url") is None                  # no host


def test_detect_service_no_catchall(monkeypatch):
    monkeypatch.setattr(catalog_meta.config, "DOMAIN_SERVICES", {})
    monkeypatch.setattr(catalog_meta.config, "FEED_SERVICE", "")
    monkeypatch.setattr(catalog_meta.config, "CATCHALL_SERVICE", "")
    monkeypatch.setattr(catalog_meta.state, "services_cached", lambda: [])
    assert catalog_meta.detect_service("https://unknown.tld/x") is None      # generic: no default
