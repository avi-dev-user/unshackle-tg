"""Keyed i18n with external locale files (ngx-translate style).

UI strings are referenced by a semantic KEY: `tr("NO_TRACKS_FOUND", lang)`. Each language is a
flat `locales/<code>.json` mapping KEY -> text (e.g. en.json: {"NO_TRACKS_FOUND": "No tracks
found."}, he.json: {"NO_TRACKS_FOUND": "לא נמצאו מסלולים."}). At import time every
`locales/*.json` is auto-loaded, so ADDING A LANGUAGE NEEDS NO CODE CHANGE - a translator just
copies en.json to e.g. `locales/de.json`, translates the values, and it appears in the in-bot
language switch. English (en.json) is the source/fallback.

(The function is named `tr`, not `t`, because `t` is used across the codebase as a loop
variable for titles/tags and would shadow it.)

Strings with runtime values use `{named}` placeholders + `.format(...)` at the call site, so all
languages share one set of placeholders, e.g. `tr("SEASON_OF", lang).format(n=1, m=3)`.
"""
import json
import os

# The fallback language for users who haven't picked one. The framework default is English;
# a deployment can set DEFAULT_LANG (e.g. "he") so its users get that language out of the box.
DEFAULT_LANG = os.environ.get("DEFAULT_LANG", "en")
_LOCALES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "locales")

# Optional pretty names for the language switch; a code with no entry shows the code itself.
_LANG_NAMES = {"en": "English", "he": "עברית", "ar": "العربية", "ru": "Русский",
               "es": "Español", "fr": "Français", "de": "Deutsch", "pt": "Português",
               "it": "Italiano", "tr": "Türkçe", "nl": "Nederlands"}


def _load_catalogs() -> dict[str, dict]:
    """{lang_code: {english_source: translation}} from every locales/<code>.json."""
    cats: dict[str, dict] = {}
    if os.path.isdir(_LOCALES_DIR):
        for fn in sorted(os.listdir(_LOCALES_DIR)):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(_LOCALES_DIR, fn), encoding="utf-8") as fh:
                        cats[fn[:-5]] = json.load(fh)
                except Exception:
                    pass
    return cats


_CATALOGS = _load_catalogs()

# English (source) is always available; every locale file present adds a language.
LANGS = {DEFAULT_LANG: _LANG_NAMES.get(DEFAULT_LANG, DEFAULT_LANG),
         **{c: _LANG_NAMES.get(c, c) for c in _CATALOGS if c != DEFAULT_LANG}}


def tr(key: str, lang: str = DEFAULT_LANG) -> str:
    """Resolve a message KEY to the user's language; fall back to English, then the key itself."""
    cat = _CATALOGS.get(lang)
    if cat and key in cat:
        return cat[key]
    return _CATALOGS.get(DEFAULT_LANG, {}).get(key, key)
