"""Tests for the i18n system: locale-file integrity (the thing the manual review checked by
hand) and tr() fallback behaviour."""
import glob
import json
import os
import re

from src import i18n

_LOCALES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "locales")
_PH = re.compile(r"\{(\w+)\}")


def _load(code):
    with open(os.path.join(_LOCALES, f"{code}.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_en_he_key_parity():
    en, he = _load("en"), _load("he")
    assert set(en) == set(he), f"en-only={set(en)-set(he)}  he-only={set(he)-set(en)}"


def test_placeholder_parity_per_key():
    en, he = _load("en"), _load("he")
    for k in en:
        assert set(_PH.findall(en[k])) == set(_PH.findall(he[k])), f"placeholder mismatch in {k!r}"


def test_every_code_key_exists_in_locales():
    en = _load("en")
    root = os.path.dirname(_LOCALES)
    missing = []
    for f in glob.glob(os.path.join(root, "src", "*.py")):
        if f.endswith("i18n.py"):
            continue
        for m in re.finditer(r'tr\(\s*"([A-Z0-9_]+)"', open(f, encoding="utf-8").read()):
            if m.group(1) not in en:
                missing.append((os.path.basename(f), m.group(1)))
    assert not missing, f"tr() keys missing from en.json: {missing}"


def test_tr_returns_translation_then_falls_back():
    # a real key resolves in he; an unknown key falls back to itself
    assert i18n.tr("NO_TRACKS_FOUND", "he") == _load("he")["NO_TRACKS_FOUND"]
    assert i18n.tr("NO_TRACKS_FOUND", "en") == _load("en")["NO_TRACKS_FOUND"]
    assert i18n.tr("THIS_KEY_DOES_NOT_EXIST", "he") == "THIS_KEY_DOES_NOT_EXIST"


def test_format_placeholders_resolve():
    # a formatted key must not raise and must substitute
    s = i18n.tr("SEASON_OF", "he").format(season=1, total=3)
    assert "{" not in s
