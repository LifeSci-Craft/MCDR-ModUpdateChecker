"""The message catalogues, and whether the code and the catalogues still agree.

A translation file is the one part of a plugin that fails silently. A typo in a key does not
raise — ``translate`` is contractually forbidden from raising — it just prints the raw key in
the middle of an admin's console output. And a key that exists in ``en_us`` but not in
``zh_cn`` degrades to English without anyone noticing.

So the two directions are both checked: nothing the code asks for is missing, and nothing the
catalogue offers has gone stale. The handful of keys built at runtime (``status.<x>`` and
``matched_by.<x>``) cannot be found by scanning, so they are listed explicitly and checked for
completeness instead.
"""

import json
import re
from pathlib import Path

import pytest

from mod_update_checker import i18n
from mod_update_checker.report import ALL_STATUSES
from support import option_paths

PACKAGE = Path(__file__).resolve().parent.parent / "mod_update_checker"
CATALOGUES = sorted(PACKAGE.glob("lang/*.json"))

#: Key families whose full name is only known at runtime.
DYNAMIC_PREFIXES = ("status.", "matched_by.")

#: Values the dynamic families are built from.
#: How an entry was tied to a project. ``fingerprint`` went with CurseForge.
MATCHED_BY_VALUES = ("hash", "name")

#: A message key used literally in the source, e.g. ``"note.declared_mc"`` or
#: ``"command.help.list"``. Keys are matched by their leading family so that unrelated dotted
#: strings (``"fabric.mod.json"``, ``"example.invalid"``) are not swept in; the dot-suffix is
#: allowed to repeat, since three-segment keys are the norm for subcommands.
#:
#: A new key family has to be added here as well as to the catalogue, or its entries look
#: stale and ``test_no_catalogue_entry_is_stale`` fails — which is the intended behaviour: the
#: list is a deliberate allow-list of the dotted prefixes that mean something to this plugin.
_KEY_IN_CODE = re.compile(
    r'"('
    r'(?:line|note|advisory|report|command|console|check|help|language|download)'
    r'\.[a-z_0-9]+(?:\.[a-z_0-9]+)*'
    r')"'
)


def catalogue(language: str) -> dict:
    return json.loads((PACKAGE / "lang" / "{}.json".format(language)).read_text(encoding="utf-8"))


def languages() -> list:
    return [path.stem for path in CATALOGUES]


def config_option_paths() -> set:
    """The config's option paths, which the scanner must not mistake for message keys.

    The two namespaces overlap in spelling: ``check.ignored_mods`` is a config path, and
    ``check.in_game_header`` is a message key, and both start with a segment that looks like a
    key family. Listing the real paths is how the scanner tells them apart — a rule about
    spelling would either miss keys or invent them.
    """
    import mod_update_checker as plugin

    return option_paths(plugin.Config)


def keys_referenced_in_code() -> set:
    found = set()
    for path in PACKAGE.glob("*.py"):
        found.update(_KEY_IN_CODE.findall(path.read_text(encoding="utf-8")))
    # ``_LEGACY_FLAT_OPTIONS`` maps the old flat option names onto their new paths, so the
    # source is full of dotted config strings that were never message keys.
    return found - config_option_paths()


def test_both_catalogues_ship():
    assert languages() == ["en_us", "zh_cn"]


@pytest.mark.parametrize("language", ["en_us", "zh_cn"])
def test_catalogue_is_a_flat_object_of_strings(language):
    data = catalogue(language)
    assert data
    for key, value in data.items():
        assert isinstance(key, str) and key
        assert isinstance(value, str) and value.strip()


def test_catalogues_have_identical_key_sets():
    """A key in one language and not the other means a silent fallback for whoever reads it."""
    english = set(catalogue("en_us"))
    chinese = set(catalogue("zh_cn"))
    assert english - chinese == set(), "missing from zh_cn"
    assert chinese - english == set(), "missing from en_us"


def test_every_key_the_code_asks_for_exists():
    available = set(catalogue("en_us"))
    missing = sorted(keys_referenced_in_code() - available)
    assert missing == []


def test_no_catalogue_entry_is_stale():
    """Catches entries left behind by a rename, which otherwise linger forever."""
    referenced = keys_referenced_in_code()
    dynamic = {
        key
        for key in catalogue("en_us")
        if key.startswith(DYNAMIC_PREFIXES)
    }
    unreferenced = sorted(set(catalogue("en_us")) - referenced - dynamic)
    assert unreferenced == []


def test_the_dynamic_key_families_are_complete():
    available = set(catalogue("en_us"))
    assert {"status." + status for status in ALL_STATUSES} <= available
    assert {"matched_by." + value for value in MATCHED_BY_VALUES} <= available
    # No leftovers in those families either.
    assert {
        key for key in available if key.startswith("status.")
    } == {"status." + status for status in ALL_STATUSES}
    assert {
        key for key in available if key.startswith("matched_by.")
    } == {"matched_by." + value for value in MATCHED_BY_VALUES}


@pytest.mark.parametrize("language", ["en_us", "zh_cn"])
def test_translation_placeholders_are_present_in_both_languages(language):
    """A translation that lost a placeholder prints the whole template unformatted.

    Compared against ``en_us`` rather than against the call sites, because the templates are
    the thing being checked for agreement.
    """
    english = catalogue("en_us")
    other = catalogue(language)
    for key, template in english.items():
        expected = set(re.findall(r"\{(\w+)\}", template))
        actual = set(re.findall(r"\{(\w+)\}", other.get(key, "")))
        assert actual == expected, "placeholder mismatch in {}".format(key)


def test_unknown_placeholder_cannot_break_a_message(monkeypatch):
    """``translate`` must never raise: a bad catalogue must not be able to abort a check."""
    monkeypatch.setitem(i18n._CATALOGS, "xx_test", i18n.Catalog({"k": "{a} and {b}"}, None))
    assert i18n.translate("k", "xx_test", a=1) == "{a} and {b}"
    assert i18n.translate("k", "xx_test", a=1, b=2) == "1 and 2"


def test_missing_key_falls_back_then_shows_itself(monkeypatch):
    assert i18n.translate("no.such.key", "en_us") == "no.such.key"
    # Present in en_us only: a zh_cn reader still gets readable text.
    monkeypatch.setitem(i18n._CATALOGS, "zh_cn2", i18n.Catalog({}, None))
    assert i18n.translate("report.no_updates", "zh_cn2") == i18n.translate(
        "report.no_updates", "en_us"
    )


def test_available_languages_reads_the_package():
    assert i18n.available_languages() == ["en_us", "zh_cn"]


@pytest.mark.parametrize(
    "setting,mcdr_language,expected",
    [
        ("auto", "zh_cn", "zh_cn"),
        ("auto", "en_us", "en_us"),
        ("", "zh_cn", "zh_cn"),
        ("zh_cn", "en_us", "zh_cn"),
        ("ZH-CN", "en_us", "zh_cn"),
        ("en_us", "zh_cn", "en_us"),
        # Not shipped: zh_tw is pointed at its readable sibling before English.
        ("zh_tw", "en_us", "zh_cn"),
        # auto defers, so an unsupported MCDR language resolves silently.
        ("auto", "de_de", "en_us"),
    ],
)
def test_language_resolution(setting, mcdr_language, expected):
    choice = i18n.resolve(setting, mcdr_language)
    assert choice.language == expected
    assert choice.note_key is None


def test_an_unknown_configured_language_warns_but_still_works():
    choice = i18n.resolve("klingon", "en_us")
    assert choice.language == "en_us"
    assert choice.note_key == i18n.UNSUPPORTED_KEY
    assert choice.note_args["value"] == "klingon"


def test_make_translator_binds_the_language():
    tr = i18n.make_translator("zh_cn")
    assert tr("report.no_updates") == i18n.translate("report.no_updates", "zh_cn")
    assert "没有发现更新" in tr("report.no_updates")
