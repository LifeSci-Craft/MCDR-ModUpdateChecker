"""Version comparison and Minecraft version-range matching.

Two jobs live here, and both exist because mod version strings are a zoo:

* :func:`compare` — decide which of two mod version strings is newer. The inputs are
  whatever a mod author felt like writing: ``1.2.3``, ``v1.2.3``, ``0.162.0+26.3``,
  ``1.19.2-0.5.3``, ``mc1.21.1-1.2.3``, ``2.1.0-beta.4+build.31``. The routine is
  deliberately total — it always answers something instead of raising, because a single
  unparseable mod must not be able to abort a whole check run.

* :func:`covers` — does a ``fabric.mod.json`` ``depends.minecraft`` expression (``>=1.21.4
  <1.22``, ``~1.21``, ``1.21.x``, ``[1.21,1.22)``, or a list of those meaning *any of*)
  admit a given Minecraft version? Used to tell an admin "this jar was built for 1.21.x
  but your server runs 26.3", and to resolve the game-version filter when the server's own
  version could not be detected.

Nothing here imports MCDR or touches the network, so the whole module is directly
unit-testable.
"""

import re
from typing import Callable, Iterable, List, Optional, Sequence, Tuple, Union

__all__ = [
    "compare",
    "is_prerelease",
    "latest_of",
    "covers",
    "parse_spec",
    "normalize_spec",
]

# --------------------------------------------------------------------------------------
# Version strings
# --------------------------------------------------------------------------------------

#: Pre-release markers and how they rank against each other. Lower is older. A version
#: whose *extra* trailing segment is one of these is therefore older than the same version
#: without it, which is what makes ``1.0.0`` newer than ``1.0.0-rc.1``.
_PRERELEASE_RANK = {
    "snapshot": -40,
    "dev": -35,
    "nightly": -34,
    "test": -30,
    "alpha": -20,
    "a": -20,
    "beta": -10,
    "b": -10,
    "pre": -6,
    "prerelease": -6,
    "preview": -6,
    "rc": -2,
    "cr": -2,
}

#: Splits a version string into segments. Every separator mod authors use in practice.
_SEPARATORS = re.compile(r"[.\-_]+")
#: Splits a segment into alternating text / digit runs, so ``rc11`` == ``rc`` + ``11``.
_ALNUM = re.compile(r"(\d+)")

Token = Tuple[int, str, str]  # (kind, digit string, text); kind 0 = numeric, 1 = text


def _normalise_digits(digits: str) -> str:
    """``0007`` -> ``7``, so equal numbers compare equal however they were written."""
    return digits.lstrip("0") or "0"


def _compare_digits(left: str, right: str) -> int:
    """Numeric comparison of two non-negative integers held as digit strings.

    Deliberately not ``int()``: Python 3.11+ refuses to convert a string longer than 4300
    digits, and a junk version number can easily contain one — which would break this
    module's promise never to raise. Comparing by length first (then lexicographically) gives
    the same ordering for arbitrary lengths, at any size.
    """
    if len(left) != len(right):
        return -1 if len(left) < len(right) else 1
    if left != right:
        return -1 if left < right else 1
    return 0


def _tokenize(text: str) -> List[Token]:
    """Turn a version fragment into comparable tokens.

    A leading ``v`` is dropped (``v1.2.3`` is ``1.2.3``), and every digit run becomes its
    own numeric token, so ``rc11`` sorts after ``rc2`` rather than before it.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) > 1 and text[0] in "vV" and text[1].isdigit():
        text = text[1:]
    tokens: List[Token] = []
    for segment in _SEPARATORS.split(text):
        if not segment:
            continue
        for piece in _ALNUM.split(segment):
            if not piece:
                continue
            if piece.isdigit():
                tokens.append((0, _normalise_digits(piece), ""))
            else:
                tokens.append((1, "", piece.lower()))
    return tokens


def _is_prerelease_token(token: Token) -> bool:
    return token[0] == 1 and token[2] in _PRERELEASE_RANK


def _compare_tokens(left: Sequence[Token], right: Sequence[Token]) -> int:
    """Compare two token streams. Returns -1, 0 or 1."""
    for index in range(min(len(left), len(right))):
        a, b = left[index], right[index]
        if a[0] == 0 and b[0] == 0:
            verdict = _compare_digits(a[1], b[1])
            if verdict != 0:
                return verdict
            continue
        if a[0] == 1 and b[0] == 1:
            rank_a = _PRERELEASE_RANK.get(a[2])
            rank_b = _PRERELEASE_RANK.get(b[2])
            if rank_a is not None and rank_b is not None:
                if rank_a != rank_b:
                    return -1 if rank_a < rank_b else 1
                continue
            if a[2] != b[2]:
                return -1 if a[2] < b[2] else 1
            continue
        # One side numeric, the other textual. A number always outranks a word, which is
        # what makes ``1.0.0`` beat ``1.0.0-rc`` and ``1.21`` beat ``mc1.21``.
        return 1 if a[0] == 0 else -1

    if len(left) == len(right):
        return 0

    longer, shorter = (left, right) if len(left) > len(right) else (right, left)
    extra = longer[min(len(left), len(right)):]
    # A trailing segment that *starts* with a pre-release marker makes a version older, not
    # newer: ``1.0.0`` beats ``1.0.0-rc.1``. Only the first extra token is consulted, since
    # the rest of a pre-release identifier is its sequence number (``-rc.1`` -> ``rc`` + 1)
    # and would otherwise mask the marker.
    #
    # Anything else extra is a genuine extension, so the longer side wins: ``1.0.1`` beats
    # ``1.0``, and ``1.19.2-0.5.3`` beats a bare ``1.19.2``. That last one is a judgement
    # call — the dash there separates a game version from a mod version rather than
    # introducing a pre-release — but it is the reading that matches what authors mean.
    if extra and _is_prerelease_token(extra[0]):
        return -1 if longer is left else 1
    return 1 if longer is left else -1


def _split_build(text: str) -> Tuple[str, str]:
    """Split ``0.162.0+26.3`` into its core and its build metadata."""
    core, _, build = text.partition("+")
    return core, build


def compare(left: str, right: str) -> int:
    """Compare two version strings: ``-1`` if left is older, ``0``, ``1`` if newer.

    Build metadata (after ``+``) is only consulted when the cores are equal, so
    ``0.162.0+26.3`` ranks above a bare ``0.162.0`` — for mod jars that suffix normally
    encodes the Minecraft version the build targets, which is genuine information.

    The function never raises: junk in becomes a lexicographic answer out.
    """
    if left == right:
        return 0
    core_left, build_left = _split_build(str(left))
    core_right, build_right = _split_build(str(right))
    verdict = _compare_tokens(_tokenize(core_left), _tokenize(core_right))
    if verdict != 0:
        return verdict
    if build_left or build_right:
        return _compare_tokens(_tokenize(build_left), _tokenize(build_right))
    return 0


def is_prerelease(version: str) -> bool:
    """True when the string carries an ``alpha``/``beta``/``rc``/``pre`` style marker.

    Build metadata is dropped first: in ``1.0.0-beta+26.3`` the marker is in the core, and
    the ``26.3`` after the ``+`` is the Minecraft version, not a release channel.
    """
    core, _ = _split_build(str(version))
    return any(_is_prerelease_token(token) for token in _tokenize(core))


def latest_of(versions: Iterable[str]) -> Optional[str]:
    """The newest string in ``versions``, or ``None`` for an empty input."""
    best: Optional[str] = None
    for candidate in versions:
        if best is None or compare(candidate, best) > 0:
            best = candidate
    return best


# --------------------------------------------------------------------------------------
# Version ranges
# --------------------------------------------------------------------------------------

RangeSpec = Union[str, Sequence[str], None]

_INTERVAL = re.compile(r"^([\[\(])\s*([^,\]\)]*)\s*,\s*([^\]\)]*)\s*([\]\)])$")
_OPERATOR = re.compile(r"^(>=|<=|==|>|<|=|~|\^)?\s*(.*)$")
_WILDCARD_SEGMENTS = {"x", "*", "X"}

#: An operator with whitespace before its version — ``>= 1.21.4``. Both spellings occur in
#: real metadata files, and without this the token split below turns ``>=`` into an empty
#: operand that matches everything, silently degrading a range into an exact-match test.
_LOOSE_OPERATOR = re.compile(r"(>=|<=|==|>|<|=|~|\^)\s+(?=\S)")


def _segments(version: str) -> List[str]:
    core, _ = _split_build(str(version))
    if len(core) > 1 and core[0] in "vV" and core[1].isdigit():
        core = core[1:]
    return [segment for segment in _SEPARATORS.split(core) if segment != ""]


def _same_prefix(base: str, version: str, depth: int) -> bool:
    """Do ``base`` and ``version`` agree on their first ``depth`` segments?"""
    base_segments = _segments(base)
    version_segments = _segments(version)
    for index in range(min(depth, len(base_segments))):
        if index >= len(version_segments):
            return False
        if base_segments[index] != version_segments[index]:
            return False
    return True


def _wildcard_match(base: str, version: str) -> bool:
    """``1.21.x`` matches ``1.21`` and ``1.21.4`` but not ``1.22``."""
    base_segments = _segments(base)
    version_segments = _segments(version)
    for index, segment in enumerate(base_segments):
        if segment in _WILDCARD_SEGMENTS:
            # A trailing wildcard may also match nothing at all (``1.21.x`` ~ ``1.21``).
            return True
        if index >= len(version_segments):
            return False
        if segment != version_segments[index]:
            return False
    return True


def _single_predicate(op: Optional[str], base: str) -> Callable[[str], bool]:
    """Build a one-argument test for an operator plus a version."""
    base = base.strip()
    if base in ("", "*", "x", "X"):
        return lambda _version: True

    if any(segment in _WILDCARD_SEGMENTS for segment in _segments(base)):
        if op in (None, "=", "=="):
            return lambda version: _wildcard_match(base, version)
        # ``>=1.21.x`` is nonsense; treating it as a wildcard match is the least
        # surprising thing to do, and it keeps a broken metadata file from raising.
        return lambda version: _wildcard_match(base, version)

    def at_least(version: str) -> bool:
        return compare(version, base) >= 0

    def greater(version: str) -> bool:
        return compare(version, base) > 0

    def at_most(version: str) -> bool:
        return compare(version, base) <= 0

    def smaller(version: str) -> bool:
        return compare(version, base) < 0

    def equal(version: str) -> bool:
        return compare(version, base) == 0

    if op == ">=":
        return at_least
    if op == ">":
        return greater
    if op == "<=":
        return at_most
    if op == "<":
        return smaller
    if op == "~":
        # Maven/Fabric style: ``~1.21.4`` means ">=1.21.4 and same major.minor".
        return lambda version: at_least(version) and _same_prefix(base, version, 2)
    if op == "^":
        return lambda version: at_least(version) and _same_prefix(base, version, 1)
    return equal


def _parse_conjunction(expression: str) -> List[Callable[[str], bool]]:
    """Parse one all-of expression into a list of tests that must all pass."""
    expression = expression.strip()
    if expression in ("*", ""):
        return [lambda _version: True]

    interval = _INTERVAL.match(expression)
    if interval:
        open_bracket, low, high, close_bracket = interval.groups()
        tests: List[Callable[[str], bool]] = []
        if low.strip():
            tests.append(
                _single_predicate(">=" if open_bracket == "[" else ">", low.strip())
            )
        if high.strip():
            tests.append(
                _single_predicate("<=" if close_bracket == "]" else "<", high.strip())
            )
        return tests or [lambda _version: True]

    # ``>=1.21.4 <1.22`` — split the tokens but keep their operators attached. Splitting on
    # whitespace cannot express this, because the operator and its version are glued together
    # in some styles (``>=1.21.4``) and separated in others (``>= 1.21.4``); the glued form is
    # produced first so both end up in one representation.
    tests = []
    for token in re.split(r"[,\s]+", _LOOSE_OPERATOR.sub(r"\1", expression)):
        if not token:
            continue
        match = _OPERATOR.match(token)
        op, base = (match.group(1), match.group(2)) if match else (None, token)
        if op is None and base.strip() in ("", "*"):
            continue
        tests.append(_single_predicate(op, base))
    return tests or [lambda _version: True]


def parse_spec(spec: RangeSpec) -> List[Callable[[str], bool]]:
    """Turn a ``depends.minecraft``-style value into a flat list of *any-of* tests.

    ``None`` (the key is absent) means "no constraint", i.e. everything matches.
    A list means the alternatives the metadata allows — Fabric treats a list as *any of*,
    so this follows suit. Empty alternatives (``[""]``, seen in the wild) are ignored
    rather than treated as "matches nothing", which would silently drop a good mod.
    """
    if spec is None:
        return [lambda _version: True]

    alternatives: Sequence[str]
    if isinstance(spec, str):
        alternatives = [spec]
    else:
        alternatives = [str(item) for item in spec]

    any_of: List[Callable[[str], bool]] = []
    for alternative in alternatives:
        if not alternative.strip():
            continue
        tests = _parse_conjunction(alternative)
        any_of.append(
            (lambda tests: lambda version: all(test(version) for test in tests))(tests)
        )
    if not any_of:
        return [lambda _version: True]
    return any_of


def covers(spec: RangeSpec, version: str) -> bool:
    """Does ``spec`` admit ``version``?"""
    return any(test(version) for test in parse_spec(spec))


def normalize_spec(spec: RangeSpec) -> str:
    """A short human-readable form of a range specification, for reports."""
    if spec is None:
        return "*"
    if isinstance(spec, str):
        text = spec.strip()
        return text or "*"
    parts = [str(item).strip() for item in spec]
    parts = [part for part in parts if part]
    return " | ".join(parts) if parts else "*"
