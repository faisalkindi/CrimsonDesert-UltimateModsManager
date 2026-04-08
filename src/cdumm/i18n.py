"""Minimal i18n for CDUMM - JSON key-value lookup with format substitution."""
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_strings: dict[str, str] = {}
_fallback: dict[str, str] = {}
_current_lang: str = "en"

if getattr(sys, 'frozen', False):
    TRANSLATIONS_DIR = Path(sys._MEIPASS) / "cdumm" / "translations"
else:
    TRANSLATIONS_DIR = Path(__file__).parent / "translations"


def load(lang: str = "en") -> None:
    """Load a language file. Always loads English as fallback first."""
    global _strings, _fallback, _current_lang
    _current_lang = lang
    _fallback = _load_file("en")
    if lang == "en":
        _strings = _fallback
    else:
        _strings = _load_file(lang)


def _load_file(lang: str) -> dict[str, str]:
    path = TRANSLATIONS_DIR / f"{lang}.json"
    if not path.exists():
        logger.warning("Translation file not found: %s", path)
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to load translations from %s: %s", path, e)
        return {}


def tr(key: str, **kwargs) -> str:
    """Look up a translated string by key, with optional format substitution.

    Falls back to English, then to the key itself.
    """
    text = _strings.get(key) or _fallback.get(key) or key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError):
            logger.debug("Format substitution failed for key '%s'", key)
    return text


_RTL_LANGUAGES = {"ar", "he", "fa", "ur"}


def current_language() -> str:
    return _current_lang


def is_rtl() -> bool:
    """Check if the current language is right-to-left."""
    return _current_lang in _RTL_LANGUAGES


def available_languages() -> list[tuple[str, str]]:
    """Return list of (code, display_name) for all available translations."""
    langs = []
    if not TRANSLATIONS_DIR.exists():
        return [("en", "English")]
    for f in sorted(TRANSLATIONS_DIR.glob("*.json")):
        code = f.stem
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
            name = data.get("_language_name", code)
        except Exception:
            name = code
        langs.append((code, name))
    return langs or [("en", "English")]
