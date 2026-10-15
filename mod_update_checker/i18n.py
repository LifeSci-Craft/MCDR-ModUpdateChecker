"""Message lookup for Mod Update Checker.

Every human-facing string lives in ``mod_update_checker/lang/<code>.json`` — one flat
``key -> template`` object per language, so adding a language is adding a file and never
touching code.

Two rules this module exists to guarantee:

* a template whose placeholders do not match the call is returned **as is**, never raised —
  a bad translation must not be able to abort a check run;
* a missing key falls back to ``en_us`` and finally to the key name, so the worst case is a
  readable-but-untranslated message.

Catalogues are read through the import system (``pkgutil.get_data``), not ``open()``: inside
a packed ``.mcdr`` the plugin lives in a zip and there is no directory to list —
``pkgutil`` asks the loader, which is the only thing that works in both layouts.

MCDR's own plugin-translation API (``server.tr`` plus ``lang/`` auto-registration) was
considered and deliberately not used: the directory name and JSON format already match it,
so switching later stays possible, while this module keeps the language selectable from the
plugin's own config (``language: auto`` follows MCDR).
"""

import importlib.resources
import json
import os
import pkgutil
import warnings
from typing import Any, Dict, List, NamedTuple, Optional, Sequence

#: Last resort for every fallback chain: the one language the docs assume is present.
FALLBACK_LANGUAGE = "en_us"

#: Value of the ``language`` option meaning "do what MCDR does".
AUTO = "auto"

LANG_DIR_NAME = "lang"

#: The catalogues this plugin ships, as a claim it can be held to.
#:
#: Two jobs, both about the same failure: at least one of these files is always in the package,
#: so ``available_languages()`` must never answer with less. The listing below is what makes
#: that true when the *enumerating* API is missing — which is a real configuration here, not a
#: hypothetical: ``importlib.resources.files`` is Python 3.9+, this plugin's declared floor is
#: 3.8 (``ruff.toml``, ``tools/check_python_floor.py``, the CI ``python-floor`` job), and inside
#: a packed ``.mcdr`` there is no directory to fall back to either. Measured on that
#: combination: ``available_languages()`` returned ``['en_us']`` and a ``zh_cn`` admin was told,
#: in English, that their language did not exist.
#:
#: A code listed here that is *not* shipped is harmless — it is only ever used as a fallback
#: floor, and a code with no catalogue falls back to English exactly as it always did.
SHIPPED_LANGUAGES: Sequence[str] = ("en_us", "zh_cn")

#: Catalogue key of the warning raised when the configured language is not one we ship.
UNSUPPORTED_KEY = "language.unsupported"

#: MCDR currently ships ``en_us`` / ``zh_cn`` / ``zh_tw``. A ``zh_tw`` admin can still read
#: the simplified catalogue, and vice versa, so each points at the other before anything
#: falls back to English.
FALLBACKS: Dict[str, Sequence[str]] = {
    "zh_tw": ("zh_cn",),
    "zh_hk": ("zh_cn",),
    "zh_cn": ("zh_tw",),
}


class Catalog(NamedTuple):
    """One language file, plus why it is empty if it could not be read."""

    messages: Dict[str, str]
    error: Optional[str]


class Choice(NamedTuple):
    """Outcome of resolving the ``language`` option.

    ``note_key`` / ``note_args`` describe a warning to print (an unrecognised value); they
    are ``None`` / empty when the request was understood.
    """

    language: str
    note_key: Optional[str]
    note_args: Dict[str, Any]


def _filesystem_dir() -> str:
    """Where this file lives on disk — meaningless inside a packed plugin, hence the
    ``pkgutil`` path below is tried first."""
    return os.path.dirname(os.path.abspath(__file__))


def _resource_bytes(relative: str) -> Optional[bytes]:
    """Read a file that ships inside the plugin package, or return ``None``."""
    try:
        data = pkgutil.get_data(__package__, relative)
    except (OSError, ImportError, ValueError):
        data = None
    if data is not None:
        return data

    try:
        with open(os.path.join(_filesystem_dir(), *relative.split("/")), "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _list_lang_files() -> List[str]:
    """File names in the package's ``lang`` folder, empty if it cannot be listed.

    Three ways, in order of how much they need from the import system, because the combination
    that matters is "old interpreter **and** packed plugin":

    * ``importlib.resources.contents`` — the enumerator that exists from 3.7, and the one that
      works for a zip as well as a directory. ``files()`` is 3.9+ and used to be what stood
      here, which meant the declared 3.8 floor silently degraded to English-only.
    * ``os.listdir`` on the module's own directory — the unpacked case, and what is left if
      even ``contents`` is unavailable or a custom loader refuses it.
    * :data:`SHIPPED_LANGUAGES` in :func:`available_languages`, which is the floor rather than a
      fourth strategy.
    """
    try:
        # Can only ever fire on 3.11+, where a replacement exists; on 3.8-3.10 ``contents`` is
        # the only enumerator there is, and a deprecation notice on a server console is noise
        # about a version that has no alternative.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            entries = importlib.resources.contents(
                "{}.{}".format(__package__, LANG_DIR_NAME)
            )
        return sorted(entry for entry in entries if entry.endswith(".json"))
    except Exception:  # noqa: BLE001 - any loader quirk falls back to the plain path
        pass
    try:
        return sorted(os.listdir(os.path.join(_filesystem_dir(), LANG_DIR_NAME)))
    except OSError:
        return []


def available_languages() -> List[str]:
    """Every shipped language code, sorted.

    Read from the package rather than hardcoded: dropping in a ``<code>.json`` is enough to
    make it selectable. The one hardcoded part is the *floor* — a package always contains the
    catalogues in :data:`SHIPPED_LANGUAGES`, so if enumeration comes back with less than that
    (an interpreter without the listing API, a zip the loader will not enumerate, a shrunken
    package) the answer is the union instead of the truncated list. Under-reporting here is not
    a cosmetic failure: the configured language is checked against this list, so a missing entry
    turns a working catalogue into ``language.unsupported`` and an English console.
    """
    codes = sorted(
        os.path.splitext(name)[0] for name in _list_lang_files() if name.endswith(".json")
    )
    missing = [code for code in SHIPPED_LANGUAGES if code not in codes]
    if missing:
        codes = sorted(set(codes) | set(SHIPPED_LANGUAGES))
    return codes or [FALLBACK_LANGUAGE]


def normalize(code: Any) -> str:
    """``zh-CN`` / ``ZH_cn`` / ``zh_cn`` are the same request."""
    if not isinstance(code, str):
        return ""
    return code.strip().lower().replace("-", "_")


# Catalogue cache. Keyed by language; also holds failures, so a file that fails to parse is
# reported once rather than on every message.
_CATALOGS: Dict[str, Catalog] = {}


def get_catalog(language: str) -> Catalog:
    """Load ``<language>.json`` (cached). Never raises: failure gives an empty catalogue."""
    cached = _CATALOGS.get(language)
    if cached is not None:
        return cached

    data = _resource_bytes("{}/{}.json".format(LANG_DIR_NAME, language))
    if data is None:
        catalog = Catalog({}, "the file is missing from the plugin package")
    else:
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            catalog = Catalog({}, "{}: {}".format(type(error).__name__, error))
        else:
            if isinstance(parsed, dict):
                # Non-string values would break formatting later; drop them here so the
                # problem surfaces as a readable key instead of a TypeError mid-message.
                catalog = Catalog(
                    {key: value for key, value in parsed.items() if isinstance(value, str)},
                    None,
                )
            else:
                catalog = Catalog({}, "the catalogue must be a JSON object")
    _CATALOGS[language] = catalog
    return catalog


def translate(key: str, language: str = FALLBACK_LANGUAGE, **kwargs: Any) -> str:
    """``key`` in ``language`` → the default language → the key itself."""
    template = get_catalog(language).messages.get(key)
    if template is None and language != FALLBACK_LANGUAGE:
        template = get_catalog(FALLBACK_LANGUAGE).messages.get(key)
    if template is None:
        return key
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError, AttributeError, TypeError):
        # A translation that lost or misspelled a placeholder is a bug in that file, not a
        # reason to fail the caller: show it as written so the mistake is visible.
        return template


def _match(code: str, available: Sequence[str]) -> Optional[str]:
    """Return the shipped language best representing ``code``, or ``None``."""
    if not code:
        return None
    if code in available:
        return code
    for fallback in FALLBACKS.get(code, ()):
        if fallback in available:
            return fallback
    base = code.split("_")[0]
    for candidate in available:
        if candidate.split("_")[0] == base:
            return candidate
    return None


def _last_resort(available: Sequence[str]) -> str:
    if FALLBACK_LANGUAGE in available:
        return FALLBACK_LANGUAGE
    return available[0] if available else FALLBACK_LANGUAGE


def resolve(
    setting: Any,
    mcdr_language: Any = None,
    available: Optional[Sequence[str]] = None,
) -> Choice:
    """Work out which language to speak.

    :param setting: the plugin's ``language`` option — ``auto`` (or blank) follows MCDR.
    :param mcdr_language: MCDR's own language, used when ``setting`` is ``auto``.
    :param available: override the shipped language list (only the tests do this).
    """
    languages = list(available) if available is not None else available_languages()
    wanted = normalize(setting)

    if wanted in ("", AUTO):
        # ``auto`` is a deliberate request to defer, so an unsupported MCDR language is
        # resolved silently: the admin never asked for anything specific.
        return Choice(
            _match(normalize(mcdr_language), languages) or _last_resort(languages), None, {}
        )

    matched = _match(wanted, languages)
    if matched is not None:
        return Choice(matched, None, {})

    fallback = _last_resort(languages)
    return Choice(
        fallback,
        UNSUPPORTED_KEY,
        {
            "value": str(setting),
            "fallback": fallback,
            "options": ", ".join([AUTO] + languages),
        },
    )


def make_translator(language: str):
    """Bind a language once, so call sites read ``tr("key", a=b)`` instead of
    ``translate("key", language, a=b)``.

    The returned callable carries the language it was bound to as ``.language``. Nothing in the
    message path needs it — but anything that *measures* a message does: a caller that caches
    "how wide is this row" has to know when the answer has changed, and asking the callable
    beats reaching for the module's current language, which is what a cache keyed on that would
    have to do.
    """

    def translator(key: str, **kwargs: Any) -> str:
        return translate(key, language, **kwargs)

    translator.language = language
    return translator
