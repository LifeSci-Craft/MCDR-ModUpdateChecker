"""Version comparison and Minecraft range matching.

The comparison routine is the part of this plugin most likely to produce a confidently wrong
answer — telling an admin to "update" a mod that is already current, or missing a real update
because two schemes were compared lexicographically. So the cases below are drawn from real
version strings seen in the wild rather than from clean semver.
"""

import pytest

from mod_update_checker.versioning import (
    compare,
    covers,
    is_prerelease,
    latest_of,
    normalize_spec,
    parse_spec,
)

COMPARE_CASES = [
    # Plain ordering, including the one that catches lexicographic comparison: "10" must
    # sort after "2", which a string comparison gets backwards.
    ("1.0.0", "1.0.1", -1),
    ("1.0.1", "1.0.0", 1),
    ("1.0.0", "1.0.0", 0),
    ("1.0", "1.0.0", -1),
    ("2.1.0", "10.0.0", -1),
    ("1.21.4", "1.21.10", -1),
    ("1.21.10", "1.21.4", 1),
    # A leading v is decoration.
    ("v1.2.3", "1.2.3", 0),
    ("V1.2.3", "v1.2.3", 0),
    # Pre-releases rank below the release they lead to, and against each other by rank.
    ("1.0.0-rc.1", "1.0.0", -1),
    ("1.0.0", "1.0.0-rc.1", 1),
    ("1.0.0-alpha", "1.0.0-beta", -1),
    ("1.0.0-beta.2", "1.0.0-beta.10", -1),
    ("1.0.0-pre1", "1.0.0-rc.1", -1),
    # Numeric identifiers inside a pre-release must compare numerically, not as text.
    ("1.0.0-rc.9", "1.0.0-rc.10", -1),
    # Build metadata is the tie-breaker only; for mod jars it usually encodes the game
    # version the build targets, which is real information.
    ("0.162.0+26.3", "0.162.0", 1),
    ("1.0.0+build.2", "1.0.0+build.10", -1),
    # Fabric API's real scheme, and the MC-prefixed scheme some authors use.
    ("0.161.0+26.3", "0.162.0+26.3", -1),
    ("mc1.21.1-1.2.3", "mc1.21.1-1.2.4", -1),
    # The "game version - mod version" scheme: the dash is a token separator, not a
    # pre-release marker, so a version with more parts outranks a prefix of itself.
    ("1.19.2-0.5.3", "1.19.2-0.5.4", -1),
    ("1.19.2", "1.19.2-0.5.3", -1),
    ("1.19.2-0.5.3", "1.19.2-0.5.2", 1),
    # Calendar versioning, now that Minecraft uses it.
    ("26.3", "26.3.1", -1),
    ("26.3", "26.2", 1),
    # Empty and junk inputs must answer something instead of raising.
    ("", "1.0.0", -1),
    ("1.0.0", "", 1),
    ("", "", 0),
]


@pytest.mark.parametrize("left,right,expected", COMPARE_CASES)
def test_compare(left, right, expected):
    assert compare(left, right) == expected


@pytest.mark.parametrize("left,right,expected", COMPARE_CASES)
def test_compare_is_antisymmetric(left, right, expected):
    assert compare(right, left) == -expected


@pytest.mark.parametrize(
    "junk",
    ["", "not-a-version", "???", "1..2", "-", "+", "1.0.0-", "x" * 200],
)
def test_compare_never_raises_on_junk(junk):
    assert compare(junk, "1.0.0") in (-1, 0, 1)
    assert compare("1.0.0", junk) in (-1, 0, 1)
    assert compare(junk, junk) == 0


def test_compare_survives_a_very_long_digit_run():
    """``int()`` refuses strings over 4300 digits since Python 3.11.

    A junk version number can easily contain one, and this module promises never to raise —
    one malformed value in one mod's metadata must not be able to abort a whole check run.
    Numeric tokens are therefore compared as digit strings, by length then lexicographically.
    """
    huge = "9" * 5000
    assert compare(huge, "1.0.0") in (-1, 0, 1)
    assert compare("1.0.0", huge) in (-1, 0, 1)
    # Length-then-lexicographic ordering is numeric ordering, at any size.
    assert compare("9" * 5000, "9" * 4999) == 1
    assert compare("1" + "0" * 4999, huge) == -1
    # Leading zeros do not change a number's value.
    assert compare("0001.002.3", "1.2.3") == 0


def test_compare_is_a_consistent_order():
    """A sorted list has to be sorted by every pairwise comparison, not just by neighbours."""
    versions = [
        "0.9.0",
        "1.0.0-alpha",
        "1.0.0-beta.1",
        "1.0.0-beta.2",
        "1.0.0-rc.1",
        "1.0.0",
        "1.0.1",
        "1.1.0",
        "2.0.0",
        "10.0.0",
    ]
    for index, earlier in enumerate(versions):
        for later in versions[index + 1:]:
            assert compare(earlier, later) == -1, (earlier, later)


@pytest.mark.parametrize(
    "version,expected",
    [
        ("1.0.0", False),
        ("1.0.0-rc.1", True),
        ("1.0.0-beta", True),
        ("2.0.0-alpha.3", True),
        ("1.0.0-SNAPSHOT", True),
        ("1.0.0+build.5", False),
        # The marker is in the core; the "+26.3" is the Minecraft version, not a channel.
        ("1.0.0-beta+26.3", True),
        ("1.19.2-0.5.3", False),
    ],
)
def test_is_prerelease(version, expected):
    assert is_prerelease(version) is expected


def test_latest_of():
    assert latest_of(["1.0.0", "1.2.0", "1.1.9"]) == "1.2.0"
    assert latest_of(["1.0.0-rc.1", "1.0.0-beta"]) == "1.0.0-rc.1"
    assert latest_of([]) is None


COVER_CASES = [
    # Fabric's own style: a conjunction of bounds.
    (">=1.21.4 <1.22", "1.21.4", True),
    (">=1.21.4 <1.22", "1.21.9", True),
    (">=1.21.4 <1.22", "1.22", False),
    (">=1.21.4 <1.22", "1.21.3", False),
    (">=1.21.4,<1.22", "1.21.9", True),
    # Tilde and caret.
    ("~1.21.4", "1.21.4", True),
    ("~1.21.4", "1.21.9", True),
    ("~1.21.4", "1.22.0", False),
    ("~1.21.4", "1.21.3", False),
    ("^1.21.4", "1.99.0", True),
    ("^1.21.4", "2.0.0", False),
    # Wildcards.
    ("1.21.x", "1.21.4", True),
    ("1.21.x", "1.21", True),
    ("1.21.x", "1.22", False),
    ("1.21.*", "1.21.4", True),
    ("*", "26.3", True),
    ("", "26.3", True),
    # Interval notation.
    ("[1.21,1.22)", "1.21.5", True),
    ("[1.21,1.22)", "1.22", False),
    ("(1.21,1.22]", "1.21", False),
    ("(1.21,1.22]", "1.22", True),
    ("[26.3,)", "26.4", True),
    ("(,1.22]", "1.21", True),
    # Exact.
    ("26.3", "26.3", True),
    ("26.3", "26.4", False),
    ("=26.3", "26.3", True),
    # Calendar versioning is just another version to this code.
    (">=26.3", "26.3", True),
    (">=26.3", "26.2", False),
    # An operator separated from its version by a space. Both spellings occur in real
    # metadata, and the spaced form used to be read as an exact match against the operator
    # token itself, silently turning a range into an equality test.
    (">= 1.21.4", "1.21.5", True),
    (">= 1.21.4", "1.21.3", False),
    ("< 1.22", "1.21", True),
    ("< 1.22", "1.22", False),
    ("~ 1.21.4", "1.21.9", True),
    (">= 1.21.4 < 1.22", "1.21.5", True),
    (">= 1.21.4 < 1.22", "1.22", False),
]


@pytest.mark.parametrize("spec,version,expected", COVER_CASES)
def test_covers(spec, version, expected):
    assert covers(spec, version) is expected


def test_covers_list_means_any_of():
    spec = [">=1.21 <1.22", ">=26.3"]
    assert covers(spec, "1.21.5") is True
    assert covers(spec, "26.3") is True
    assert covers(spec, "1.20") is False


def test_covers_none_means_unconstrained():
    assert covers(None, "26.3") is True


def test_covers_ignores_blank_alternatives():
    """``[""]`` turns up in the wild and must not mean "matches nothing"."""
    assert covers([""], "26.3") is True
    assert covers(["", ">=26.3"], "26.3") is True


def test_covers_survives_a_garbage_range():
    assert covers("not a range at all", "26.3") in (True, False)
    assert covers(">=1.21.4 <", "1.21.5") in (True, False)


def test_parse_spec_returns_callables():
    tests = parse_spec(">=1.21.4")
    assert callable(tests[0])
    assert any(test("1.21.5") for test in tests)


@pytest.mark.parametrize(
    "spec,expected",
    [
        (None, "*"),
        ("", "*"),
        (">=1.21.4 <1.22", ">=1.21.4 <1.22"),
        ([">=1.21 <1.22", ">=26.3"], ">=1.21 <1.22 | >=26.3"),
        ([""], "*"),
    ],
)
def test_normalize_spec(spec, expected):
    assert normalize_spec(spec) == expected
