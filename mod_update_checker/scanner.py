"""Finding the ``mods/`` folder and working out what is in it.

A mod jar is not self-describing in one format: Fabric writes ``fabric.mod.json``, Quilt
writes ``quilt.mod.json``, Forge writes ``META-INF/mods.toml`` and NeoForge writes
``META-INF/neoforge.mods.toml``. All four are read here so that a mixed folder still
produces useful output instead of a row of "unknown".

What each jar contributes:

* the digests a lookup needs (SHA-1 to search by, SHA-512 to verify with), computed in one
  pass;
* the mod id, display name and version, which are the only things available for a jar whose
  bytes do **not** exist upstream — the common case for anything built from source or
  re-signed;
* the ``depends.minecraft`` range, so the report can say "built for 1.21.x, your server is
  26.3" — often the actual reason a mod misbehaves after an upgrade;
* the ``environment``, so a client-only mod sitting in a *server* folder can be flagged;
* how many jars are nested inside (Fabric "jar-in-jar"), which is worth reporting because
  those bundled copies never show up in an update check.

No MCDR import: the module takes paths and returns data, so it is unit-testable against a
throwaway directory.
"""

import json
import logging
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .digests import digests_of_file
from .versioning import RangeSpec, normalize_spec

__all__ = [
    "MOD_METADATA_ENTRIES",
    "METADATA_SIZE_LIMIT",
    "PLATFORM_MOD_IDS",
    "ModMetadata",
    "ScannedMod",
    "ScanResult",
    "resolve_mods_directory",
    "iter_mod_jars",
    "read_metadata",
    "read_metadata_error",
    "scan_jar",
    "scan_mods",
    "missing_dependencies",
]

_LOGGER = logging.getLogger(__name__)

#: How much of a metadata entry is decompressed per read. The limit is enforced on the bytes
#: that actually arrive rather than on the size the entry declares, so a lying or hostile header
#: cannot turn into a multi-megabyte allocation — the same shape the JSON and download paths
#: already use.
_METADATA_CHUNK = 65536

#: Largest metadata entry this plugin will unpack. Real ``fabric.mod.json`` and ``mods.toml``
#: files are a few kilobytes; the largest seen in the wild are well under a hundred. The limit
#: exists because ``ZipFile.read`` allocates the size the entry *declares*, so a 20 KiB jar can
#: ask for 20 MiB in one go and a ~1 MiB jar for a gigabyte — measured, and a memory spike is
#: not something a scan should be able to cause on a live server. Four mebibytes is ~40x the
#: biggest real file and still bounded, and it is checked before a single byte is decompressed.
METADATA_SIZE_LIMIT = 4 * 1024 * 1024

#: Recognised metadata files, most specific first. The loader name is derived from which
#: one was found, which is more reliable than guessing from the file name.
MOD_METADATA_ENTRIES: Tuple[Tuple[str, str], ...] = (
    ("fabric.mod.json", "fabric"),
    ("quilt.mod.json", "quilt"),
    ("META-INF/neoforge.mods.toml", "neoforge"),
    ("META-INF/mods.toml", "forge"),
)

#: Nested-jar locations used by the various loaders. Only counted, never descended into:
#: a bundled library is not something the admin installs or updates independently.
_NESTED_JAR_PATTERNS = (
    re.compile(r"^META-INF/jars/.*\.jar$"),
    re.compile(r"^META-INF/jarjar/.*\.jar$"),
    re.compile(r"^META-INF/quilt_jars/.*\.jar$"),
)

#: A ``fabric.mod.json`` occasionally carries ``//`` comments borrowed from JSON5. Strict
#: parsing is tried first; these are only used to rescue a file that failed.
_LINE_COMMENT = re.compile(r"(?m)^\s*//.*$")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")

#: Dependency names that are the platform rather than another mod. A server that is missing
#: "minecraft" is not missing a mod, and reporting it as one would bury the real findings.
#:
#: Kept deliberately short and exact. Adding a guess here — ``fabric-api``, for instance —
#: would suppress a genuinely missing library, which is the failure mode worth avoiding: a
#: list that stays silent is indistinguishable from a folder with no problems.
PLATFORM_MOD_IDS = frozenset(
    (
        "minecraft",
        "java",
        "fabricloader",
        "fabric-loader",
        "quilt_loader",
        "quilt-loader",
        "quilt_base",
        "forge",
        "neoforge",
        "minecraftforge",
        "fml",
        "javafml",
        "lowcodefml",
        "modlauncher",
        "mcloader",
    )
)


@dataclass
class ModMetadata:
    """What the jar says about itself."""

    mod_id: str
    name: str
    version: str
    loader: str
    metadata_file: str
    mc_range: RangeSpec = None
    environment: Optional[str] = None
    authors: Tuple[str, ...] = ()
    description: Optional[str] = None
    sources: Tuple[str, ...] = ()
    provides: Tuple[str, ...] = ()
    #: Other mods this jar says it **must** have, by mod id, platform names already removed.
    #:
    #: Only hard requirements: a Fabric ``depends``, a Quilt ``depends``, or a Forge/NeoForge
    #: dependency with ``mandatory`` not set to false. ``recommends`` and ``suggests`` are
    #: left out on purpose — a mod that merely prefers another one to be present is not a
    #: broken server, and reporting it as one would be the sort of noise that teaches an
    #: admin to ignore the advisory line.
    requires: Tuple[str, ...] = ()
    bundled_jars: int = 0

    @property
    def mc_range_text(self) -> str:
        return normalize_spec(self.mc_range)

    @property
    def is_client_only(self) -> bool:
        return self.environment == "client"


@dataclass
class ScannedMod:
    """One jar on disk, with whatever could be learned about it."""

    file_name: str
    path: str
    size: int = 0
    mtime: float = 0.0
    #: SHA-1 of the file's exact bytes, which is how Modrinth identifies it. Empty when the
    #: caller asked for a metadata-only scan — see :func:`scan_mods`.
    sha1: str = ""
    metadata: Optional[ModMetadata] = None
    error: Optional[str] = None

    @property
    def mod_id(self) -> str:
        """The identity used for grouping and matching; empty when unidentified."""
        return self.metadata.mod_id if self.metadata else ""

    @property
    def name(self) -> str:
        if self.metadata and self.metadata.name:
            return self.metadata.name
        return self.mod_id or self.file_name

    @property
    def version(self) -> str:
        return self.metadata.version if self.metadata else ""

    @property
    def identified(self) -> bool:
        return self.metadata is not None


@dataclass
class ScanResult:
    """Everything found in one ``mods/`` folder."""

    directory: str
    mods: List[ScannedMod] = field(default_factory=list)
    disabled: List[str] = field(default_factory=list)
    #: Why the folder itself could not be read, when that happened. Empty in every other case,
    #: including the ordinary one of a folder that does not exist yet.
    #:
    #: It is a separate field rather than an entry in ``mods`` because "the directory could not
    #: be listed" and "the directory holds no jars" are different answers, and a caller has to be
    #: able to tell them apart: one is a permission problem for the admin to fix, the other is a
    #: server with nothing installed. Both produce an empty list, so the list alone cannot say.
    directory_error: str = ""

    @property
    def directory_readable(self) -> bool:
        """Whether ``mods/`` could be listed at all."""
        return not self.directory_error

    @property
    def identified(self) -> List[ScannedMod]:
        return [mod for mod in self.mods if mod.identified]

    @property
    def unidentified(self) -> List[ScannedMod]:
        return [mod for mod in self.mods if not mod.identified]

    def duplicate_ids(self) -> Dict[str, List[ScannedMod]]:
        """Mod ids present in more than one jar — a classic breakage that is easy to miss.

        Two jars of the same mod almost always means an update was dropped in next to the
        old copy. The loader will pick one, usually not the one the admin expects.
        """
        grouped: Dict[str, List[ScannedMod]] = {}
        for mod in self.mods:
            if mod.mod_id:
                grouped.setdefault(mod.mod_id, []).append(mod)
        return {key: value for key, value in grouped.items() if len(value) > 1}

    def client_only(self) -> List[ScannedMod]:
        return [mod for mod in self.mods if mod.metadata and mod.metadata.is_client_only]


# --------------------------------------------------------------------------------------
# Directory discovery
# --------------------------------------------------------------------------------------


def resolve_mods_directory(working_directory: str, configured: str = "") -> Path:
    """Where the mods live: the configured path, or ``<server>/mods``.

    A relative configured value is resolved against the server working directory, not
    against MCDR's own folder, because that is what the admin means by "``mods``".
    """
    if configured and configured.strip():
        path = Path(configured.strip()).expanduser()
        if not path.is_absolute():
            path = Path(working_directory) / path
        return path
    return Path(working_directory) / "mods"


def iter_mod_jars(directory: Path) -> Tuple[List[Path], List[str]]:
    """Return ``(jars, disabled_names)``.

    Only ``*.jar`` is treated as installed. ``*.disabled`` / ``*.old`` / ``*.bak`` are
    counted separately: they are usually a previous version that was renamed out of the way,
    and including them would double-report every mod.

    A directory that cannot be listed is an empty result rather than an exception: ``mods/``
    can be stat-able and still unreadable — held exclusively by another process on Windows, an
    ACL that was changed, an antivirus scanning it, a sync client mid-upload — and ``iterdir``
    raises ``OSError`` for all of them. Letting it out means the whole check becomes one line in
    the log, with no report at all, for a folder the plugin could still describe as unreadable.

    The reason is deliberately **not** a third return value. This function has callers outside
    ``scanner`` that unpack exactly two names, and widening the tuple turns every one of them
    into a ``ValueError``; :func:`scan_mods` reports the failure through
    :attr:`ScanResult.directory_error` instead, where it costs nothing and cannot break a caller.
    """
    jars, disabled, _failure = _iter_mod_jars(directory)
    return jars, disabled


def directory_error(directory: Path) -> str:
    """Why ``directory`` could not be listed, or an empty string when it could.

    The cheap companion to :func:`iter_mod_jars` for callers that want a *count* rather than the
    files: ``scan_mods`` can report the same failure, but it opens every jar to do so, which is
    far too much work for a startup line. Without this, "there is no mods folder yet" and "your
    mods folder cannot be read" both reach the console as a jar count of zero.
    """
    return _iter_mod_jars(directory)[2]


def _iter_mod_jars(directory: Path) -> Tuple[List[Path], List[str], str]:
    """``iter_mod_jars`` plus the reason the directory could not be listed, if it could not."""
    if not directory.is_dir():
        return [], [], ""

    jars: List[Path] = []
    disabled: List[str] = []
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name.lower())
    except OSError as error:
        # Emphatically not the same answer as "no jars": see :attr:`ScanResult.directory_error`,
        # which :func:`scan_mods` sets so the caller can tell the two apart.
        return [], [], "{}: {}".format(type(error).__name__, error)

    for entry in entries:
        if not entry.is_file():
            continue
        lowered = entry.name.lower()
        if lowered.endswith(".jar"):
            jars.append(entry)
        elif any(
            lowered.endswith(suffix)
            for suffix in (".jar.disabled", ".jar.old", ".jar.bak", ".jar~")
        ):
            disabled.append(entry.name)
    return jars, disabled, ""


# --------------------------------------------------------------------------------------
# Metadata extraction
# --------------------------------------------------------------------------------------


def _loads_lenient(raw: str) -> Any:
    """Parse JSON, then retry after stripping the JSON5-isms that turn up in the wild.

    Refuses anything over :data:`METADATA_SIZE_LIMIT` up front as well. The entry's declared
    uncompressed size is checked before this runs, so this is the second half of one boundary
    rather than a duplicate: it is also the only bound on the work ``json.loads`` is asked to do.
    """
    if len(raw) > METADATA_SIZE_LIMIT:
        raise ValueError("metadata entry is too large to parse")
    try:
        return json.loads(raw)
    except ValueError:
        pass
    cleaned = _BLOCK_COMMENT.sub("", raw)
    cleaned = _LINE_COMMENT.sub("", cleaned)
    cleaned = _TRAILING_COMMA.sub(r"\1", cleaned)
    return json.loads(cleaned)


def _first_string(value: Any) -> str:
    """Mods put a string, a list, or a dict where a string is expected."""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _first_string(item)
            if text:
                return text
        return ""
    return ""


def _string_list(value: Any) -> Tuple[str, ...]:
    """Flatten the several shapes an author list takes into plain names."""
    out: List[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        name = value.get("name") or value.get("email")
        if isinstance(name, str):
            out.append(name)
    elif isinstance(value, (list, tuple)):
        for item in value:
            for name in _string_list(item):
                if name not in out:
                    out.append(name)
    return tuple(out)


def _contact_links(contact: Any) -> Tuple[str, ...]:
    """URLs from ``fabric.mod.json``'s ``contact`` object, in a useful order."""
    if not isinstance(contact, dict):
        return ()
    preferred = ("sources", "source", "homepage", "issues", "discord")
    ordered: List[str] = []
    for key in preferred:
        value = contact.get(key)
        if isinstance(value, str) and value.startswith("http") and value not in ordered:
            ordered.append(value)
    for key in sorted(contact):
        value = contact.get(key)
        if isinstance(value, str) and value.startswith("http") and value not in ordered:
            ordered.append(value)
    return tuple(ordered)


def _coerce_range(value: Any) -> RangeSpec:
    """Normalise a declared Minecraft version range to ``str``, ``list[str]`` or ``None``.

    One funnel for all three metadata readers, because the value comes from a third-party jar
    and its type is whatever the author typed. A number, a bool or a mapping is not a range —
    it is a mistake — and the cost of the mistake used to be the whole check: a
    ``{"depends": {"minecraft": 123}}`` reached :func:`versioning.parse_spec`, which iterated
    it and raised ``TypeError``, so every mod's result was lost, not just that jar's. A mapping
    is dropped as well rather than iterated, which would report its *keys* as versions.

    List items are kept only when they are strings, and emptied ones are dropped: a version
    range is text, and ``123`` in a list is a typo rather than a constraint — the point of
    ``[123]`` ending up as "no constraint" is that the list form is not a loophole around the
    scalar rule above. ``[">=1.21", 123, ""]`` keeps ``">=1.21"``, which is what the author
    meant.
    """
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = [item.strip() for item in value if isinstance(item, str)]
        items = [item for item in items if item]
        return items or None
    return None


def _metadata_from_fabric(data: Dict[str, Any], entry: str, loader: str) -> ModMetadata:
    depends = data.get("depends") if isinstance(data.get("depends"), dict) else {}
    # ``_string_list`` already flattens the bare-string form, so ``provides`` needs no
    # special case of its own here.
    provided = _string_list(data.get("provides"))
    # ``depends`` is ``{"<mod id>": "<version range>"}``, so the keys are the requirements.
    # ``recommends`` / ``suggests`` are not read: they are preferences, not breakage.
    return ModMetadata(
        mod_id=_first_string(data.get("id")),
        name=_first_string(data.get("name")) or _first_string(data.get("id")),
        version=_first_string(data.get("version")),
        loader=loader,
        metadata_file=entry,
        mc_range=_coerce_range(depends.get("minecraft")),
        environment=_first_string(data.get("environment")) or None,
        authors=_string_list(data.get("authors")) or _string_list(data.get("author")),
        description=_first_string(data.get("description")) or None,
        sources=_contact_links(data.get("contact")),
        provides=tuple(item for item in provided if item),
        requires=_required_mods(depends.keys()),
    )


def _quilt_dependency_ids(depends: Any) -> Tuple[str, ...]:
    """Quilt declares dependencies as a list of ``{"id": ...}`` objects, or as a mapping."""
    if isinstance(depends, dict):
        return _required_mods(depends.keys())
    if isinstance(depends, list):
        return _required_mods(
            item.get("id") for item in depends if isinstance(item, dict)
        )
    return ()


def _metadata_from_quilt(data: Dict[str, Any], entry: str) -> ModMetadata:
    """Quilt nests everything under ``quilt_loader``."""
    loader_block = data.get("quilt_loader")
    if not isinstance(loader_block, dict):
        loader_block = {}
    metadata_block = loader_block.get("metadata")
    if not isinstance(metadata_block, dict):
        metadata_block = {}
    depends = loader_block.get("depends")
    mc_range: RangeSpec = None
    if isinstance(depends, list):
        for dependency in depends:
            if isinstance(dependency, dict) and "minecraft" in dependency:
                mc_range = _coerce_range(dependency["minecraft"])
    elif isinstance(depends, dict):
        mc_range = _coerce_range(depends.get("minecraft"))
    provides = loader_block.get("provides")
    provided = _string_list(provides)
    return ModMetadata(
        mod_id=_first_string(loader_block.get("id")),
        name=_first_string(metadata_block.get("name"))
        or _first_string(loader_block.get("id")),
        version=_first_string(loader_block.get("version")),
        loader="quilt",
        metadata_file=entry,
        mc_range=mc_range,
        environment=None,
        authors=_string_list(metadata_block.get("contributors")),
        description=_first_string(metadata_block.get("description")) or None,
        sources=_contact_links(metadata_block.get("contact")),
        provides=tuple(item for item in provided if item),
        requires=_quilt_dependency_ids(depends),
    )


_TOML_MODS_BLOCK = re.compile(r"\[\[\s*mods\s*\]\](.*?)(?=\n\[\[|\Z)", re.DOTALL)
_TOML_DEP_BLOCK = re.compile(
    r"\[\[\s*dependencies\s*\.?\s*[^\]]*\]\](.*?)(?=\n\[\[|\Z)", re.DOTALL
)
_TOML_QUOTED = re.compile(r'"([^"]*)"')


def _toml_value(block: str, key: str) -> str:
    """The string value of ``key`` in a TOML block, or ``""``."""
    match = re.search(
        r'^\s*' + re.escape(key) + r'\s*=\s*"((?:[^"\\]|\\.)*)"', block, re.MULTILINE
    )
    return match.group(1) if match else ""


def _toml_bool(block: str, key: str) -> Optional[bool]:
    """``key = true`` / ``key = false`` in a TOML block, or ``None`` when it is not there.

    Only needed for ``mandatory``, and the distinction between "absent" and "false" is the
    whole point: Forge treats an absent ``mandatory`` as required, so defaulting to ``False``
    would silently drop the dependencies most likely to be missing.
    """
    match = re.search(
        r"^\s*" + re.escape(key) + r"\s*=\s*(true|false)\b",
        block,
        re.MULTILINE | re.IGNORECASE,
    )
    return match.group(1).lower() == "true" if match else None


def _required_mods(names: Iterable[Any]) -> Tuple[str, ...]:
    """Normalise declared dependency names: lowercased, deduplicated, platform removed.

    Returned sorted so a report never reshuffles because a metadata file listed the same
    dependencies in a different order.
    """
    found: List[str] = []
    for name in names:
        text = str(name or "").strip().lower()
        if not text or text in PLATFORM_MOD_IDS or text in found:
            continue
        found.append(text)
    return tuple(sorted(found))


def _required_from_dependencies(dependencies: Any) -> Tuple[str, ...]:
    """Mod ids required by a parsed ``mods.toml`` dependency table.

    Structure is ``{<owning mod id>: [<dependency>, ...]}`` — the outer key names the mod that
    *declares* the dependency, and the target is the inner ``modId``. Reading the outer key
    instead looks plausible and matches nothing, which is the bug this docstring exists to
    stop being reintroduced.
    """
    if not isinstance(dependencies, dict):
        return ()
    names: List[str] = []
    for entries in dependencies.values():
        if not isinstance(entries, list):
            continue
        for dependency in entries:
            if not isinstance(dependency, dict):
                continue
            if dependency.get("mandatory") is False:
                continue
            names.append(dependency.get("modId"))
    return _required_mods(names)


def _toml_string_list(block: str, key: str) -> Tuple[str, ...]:
    """``key`` as either a bare string or an inline array of strings."""
    single = _toml_value(block, key)
    if single:
        return (single,)
    match = re.search(
        r"^\s*" + re.escape(key) + r"\s*=\s*\[([^\]]*)\]", block, re.MULTILINE
    )
    if not match:
        return ()
    return tuple(_TOML_QUOTED.findall(match.group(1)))


def _tomllib_parse(raw: str) -> Optional[Dict[str, Any]]:
    """``tomllib.loads`` when the interpreter has it (3.11+), else ``None``.

    MCDR still supports 3.8, and pulling in a TOML library as a plugin dependency for three
    fields would be silly, so there is a regex reader below as well.
    """
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - 3.10 and older
        return None
    try:
        parsed = tomllib.loads(raw)
    except Exception:  # noqa: BLE001 - malformed metadata must not abort the scan
        return None
    return parsed if isinstance(parsed, dict) else None


def _minecraft_range_from_dependencies(dependencies: Any) -> Optional[str]:
    """The ``versionRange`` declared for ``minecraft`` in a parsed ``mods.toml``.

    The structure is ``{<owning mod id>: [<dependency>, ...]}`` — the outer key names the mod
    that *declares* the dependency, and the target is the inner ``modId`` field. Filtering on
    the outer key looks right and silently matches nothing, so the inner field is what is
    checked here.
    """
    if not isinstance(dependencies, dict):
        return None
    for entries in dependencies.values():
        if not isinstance(entries, list):
            continue
        for dependency in entries:
            if not isinstance(dependency, dict):
                continue
            if str(dependency.get("modId", "")).lower() != "minecraft":
                continue
            if dependency.get("versionRange"):
                return str(dependency["versionRange"])
    return None


def _metadata_from_toml_parsed(
    parsed: Dict[str, Any], entry: str, loader: str
) -> Optional[ModMetadata]:
    """Build metadata from a parsed ``mods.toml``; ``None`` if it has no ``[[mods]]``."""
    mods = parsed.get("mods")
    if not (isinstance(mods, list) and mods and isinstance(mods[0], dict)):
        return None
    first = mods[0]
    return ModMetadata(
        mod_id=str(first.get("modId", "") or ""),
        name=str(first.get("displayName", "") or "") or str(first.get("modId", "") or ""),
        version=str(first.get("version", "") or ""),
        loader=loader,
        metadata_file=entry,
        mc_range=_coerce_range(_minecraft_range_from_dependencies(parsed.get("dependencies"))),
        environment=None,
        authors=_string_list(first.get("authors")),
        description=str(first.get("description", "") or "") or None,
        sources=(),
        requires=_required_from_dependencies(parsed.get("dependencies")),
    )


def _metadata_from_toml_regex(raw: str, entry: str, loader: str) -> ModMetadata:
    """The ``tomllib``-free reader.

    Narrow on purpose: it takes the first ``[[mods]]`` block's id, version and name, plus the
    declared dependencies — the things this plugin actually uses. It is not a TOML parser, and
    it is tested directly rather than only through the ``tomllib`` path, which is the one most
    interpreters will take.
    """
    match = _TOML_MODS_BLOCK.search(raw)
    block = match.group(1) if match else raw

    mc_range: RangeSpec = None
    required: List[str] = []
    for dependency in _TOML_DEP_BLOCK.finditer(raw):
        body = dependency.group(1)
        target = _toml_value(body, "modId")
        if target.lower() == "minecraft":
            # The first one wins, matching the strict parser's behaviour.
            if mc_range is None:
                mc_range = _coerce_range(_toml_value(body, "versionRange"))
            continue
        if _toml_bool(body, "mandatory") is False:
            continue
        required.append(target)

    return ModMetadata(
        mod_id=_toml_value(block, "modId"),
        name=_toml_value(block, "displayName") or _toml_value(block, "modId"),
        version=_toml_value(block, "version"),
        loader=loader,
        metadata_file=entry,
        mc_range=mc_range,
        environment=None,
        authors=_toml_string_list(block, "authors"),
        description=_toml_value(block, "description") or None,
        sources=(),
        requires=_required_mods(required),
    )


def _metadata_from_toml(raw: str, entry: str, loader: str) -> ModMetadata:
    """Read ``mods.toml`` / ``neoforge.mods.toml``, preferring the real parser."""
    parsed = _tomllib_parse(raw)
    if parsed is not None:
        metadata = _metadata_from_toml_parsed(parsed, entry, loader)
        # A file that parses but carries no usable id falls through to the regex reader,
        # which may still find a ``[[mods]]`` block the strict parser rejected.
        if metadata is not None and metadata.mod_id:
            return metadata
    return _metadata_from_toml_regex(raw, entry, loader)


def _count_nested_jars(names: Iterable[str]) -> int:
    return sum(1 for name in names if any(p.match(name) for p in _NESTED_JAR_PATTERNS))


def _read_entry_capped(archive: zipfile.ZipFile, entry: str, limit: int) -> bytes:
    """Read one archive entry without ever allocating its declared size.

    ``ZipFile.read`` allocates ``file_size`` bytes up front, so until this existed the declared
    size was an allocation an outside file got to ask for, and ``METADATA_SIZE_LIMIT`` only
    refused the sizes that *said* they were too big. Measured: a 4209-byte jar made
    ``fabric.mod.json`` declare 4194303 bytes and decompressed all of it. Streaming through
    ``archive.open`` and counting the bytes as they arrive makes the limit the thing that stops
    the read, whatever the entry claimed — the same shape the JSON and download paths use.

    A ratio guard was tried first and is *not* the right primary defence: ``"d" * 262144`` is
    legitimate metadata that compresses 520:1, so any threshold low enough to stop an
    amplification case also drops honest files (the repository has a test for exactly that
    quarter-megabyte file). Counting the bytes cannot make that trade, because the cap holds
    whatever the ratio is.

    Raises :class:`BadZipFile` — which both callers already treat as "cannot be read" — when the
    entry turns out to be longer than the limit.
    """
    chunks = []
    total = 0
    declared = archive.getinfo(entry).file_size
    with archive.open(entry) as stream:
        while True:
            chunk = stream.read(_METADATA_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise zipfile.BadZipFile(
                    "{} declares {} bytes and passed the {} byte limit while being read".format(
                        entry, declared, limit
                    )
                )
            chunks.append(chunk)
    return b"".join(chunks)


def read_metadata(archive: zipfile.ZipFile) -> Optional[ModMetadata]:
    """Extract mod metadata from an open jar, or ``None`` if nothing recognisable is there.

    ``None`` covers two different situations — a valid jar that carries no metadata at all, and
    a jar whose metadata is *listed* but cannot be unpacked — and the caller has to be able to
    tell them apart: the first is a library, the second is a broken file worth reporting.
    :func:`scan_jar` recovers the difference by asking :func:`read_metadata_error` afterwards,
    which is one extra ``namelist`` and no extra decompression.

    This function does not raise, and the shapes a *zip* can be broken in are the reason it has
    to say so: an entry with the encryption bit set (``RuntimeError``), a deflate stream that
    does not decompress (``zlib.error``), a truncated central directory (``BadZipFile``), an
    entry asking for a compression method the interpreter does not implement
    (``NotImplementedError``, which is a ``RuntimeError`` and was missing here until a jar used
    one), and an entry whose declared size is a lie (``BadZipFile`` from
    :func:`_read_entry_capped`). All of them are "this file cannot be read", which is a fact to
    report rather than a reason to produce no report at all.
    """
    names = archive.namelist()
    available = set(names)
    for entry, loader in MOD_METADATA_ENTRIES:
        if entry not in available:
            continue
        try:
            info = archive.getinfo(entry)
            if info.file_size > METADATA_SIZE_LIMIT:
                # A metadata entry that claims to be enormous is not something to unpack, no
                # matter what its compressed size is: a 20 KiB file can declare 20 MiB and
                # ``ZipFile.read`` allocates the declared size in one go. This runs once per jar
                # from the check thread, so an absurd entry is a memory spike that can stall or
                # kill the server for a file nobody asked it to read.
                continue
            raw = _read_entry_capped(archive, entry, METADATA_SIZE_LIMIT).decode(
                "utf-8", errors="replace"
            )
        except (
            KeyError,
            OSError,
            RuntimeError,
            NotImplementedError,
            zipfile.BadZipFile,
            zlib.error,
        ):
            # ``RuntimeError`` is what ``zipfile`` raises for an entry whose encryption bit is
            # set, and ``NotImplementedError`` (a ``RuntimeError`` subclass) is a compression
            # method the interpreter does not implement; both are listed explicitly so the
            # contract does not depend on that inheritance. ``zlib.error`` is a corrupt deflate
            # stream — which is exactly what a jar copied while the download was still running
            # looks like — and ``BadZipFile`` covers a truncated archive and the over-long entry
            # caught by :func:`_read_entry_capped`.
            continue

        metadata: Optional[ModMetadata] = None
        if entry.endswith(".json"):
            try:
                parsed = _loads_lenient(raw)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                metadata = (
                    _metadata_from_quilt(parsed, entry)
                    if loader == "quilt"
                    else _metadata_from_fabric(parsed, entry, loader)
                )
        else:
            metadata = _metadata_from_toml(raw, entry, loader)

        if metadata is not None and metadata.mod_id:
            # ``${version}`` placeholders survive into a few broken releases; showing them
            # verbatim in a report is confusing, so fall back to the file name instead.
            if "${" in metadata.version or "{{" in metadata.version:
                metadata.version = ""
            metadata.bundled_jars = _count_nested_jars(names)
            return metadata
    return None


def read_metadata_error(archive: zipfile.ZipFile) -> str:
    """Why the jar's metadata could not be read, or ``""`` when it could.

    Called only after :func:`read_metadata` has answered ``None``, so this walks the same four
    names and finds the same condition rather than repeating the read: a metadata entry that is
    present but unreadable, or one that declares a size past :data:`METADATA_SIZE_LIMIT`. A jar
    that simply carries no metadata file — a library, a datapack, a stray download — returns
    ``""``, which is not a failure.

    The exceptions that count as "cannot be read" are the same set :func:`read_metadata` swallows,
    deliberately: this function's whole job is to explain the difference between "unreadable" and
    "absent", and two lists that disagree would attribute the same damage to two different causes
    depending on which path reached it first.
    """
    try:
        available = set(archive.namelist())
    except (
        OSError,
        RuntimeError,
        NotImplementedError,
        zipfile.BadZipFile,
        zlib.error,
    ) as error:
        return "cannot list the archive contents ({}: {})".format(type(error).__name__, error)

    for entry, _loader in MOD_METADATA_ENTRIES:
        if entry not in available:
            continue
        try:
            info = archive.getinfo(entry)
        except (KeyError, OSError) as error:
            return "the {} entry cannot be read ({}: {})".format(entry, type(error).__name__, error)
        if info.file_size > METADATA_SIZE_LIMIT:
            return "{} declares {} bytes, over the {} byte limit".format(
                entry, info.file_size, METADATA_SIZE_LIMIT
            )
        try:
            _read_entry_capped(archive, entry, METADATA_SIZE_LIMIT)
        except (
            KeyError,
            OSError,
            RuntimeError,
            NotImplementedError,
            zipfile.BadZipFile,
            zlib.error,
        ) as error:
            return "{} cannot be unpacked ({}: {})".format(entry, type(error).__name__, error)
    return ""


def scan_jar(path: Path, hashes: bool = True) -> ScannedMod:
    """Hash one jar and read its metadata. Never raises.

    "Never" includes the ways a *zip* can be broken rather than merely absent: an entry with the
    encryption bit set (``RuntimeError``), a deflate stream that does not decompress
    (``zlib.error``), a truncated central directory (``BadZipFile``). All of them become
    ``ScannedMod.error``, because one unreadable file in ``mods/`` is a fact to report and not a
    reason to produce no report at all.

    :param hashes: when ``False``, the file's bytes are never read — the size comes from
        ``stat`` and ``sha1`` is left empty. Callers that only want to describe a folder
        (counting jars, showing a status page) have no use for a digest, and on a real
        modpack hashing every jar is seconds of reading that nothing asked for.
    """
    mod = ScannedMod(file_name=path.name, path=str(path))
    try:
        information = path.stat()
        mod.mtime = information.st_mtime
        mod.size = information.st_size
    except OSError as error:
        mod.error = "{}: {}".format(type(error).__name__, error)
        return mod

    if hashes:
        try:
            with open(path, "rb") as handle:
                mod.sha1, mod.size = digests_of_file(handle)
        except OSError as error:
            mod.error = "{}: {}".format(type(error).__name__, error)
            return mod

    try:
        with zipfile.ZipFile(mod.path) as archive:
            mod.metadata = read_metadata(archive)
            if mod.metadata is None:
                # ``None`` is also what a valid jar with no metadata looks like, so ask why
                # before calling it either way. A jar whose ``fabric.mod.json`` is *there* and
                # unreadable is a broken file, and saying so is the difference between an admin
                # replacing it and an admin wondering why a mod silently does nothing.
                failure = read_metadata_error(archive)
                if failure:
                    mod.error = "unreadable metadata ({})".format(failure)
    except (OSError, RuntimeError, zipfile.BadZipFile, zlib.error) as error:
        mod.error = "not a readable jar ({}: {})".format(type(error).__name__, error)
        return mod

    # ``metadata is None`` is not an error: a perfectly valid jar that simply is not a mod — a
    # library, a datapack archive, a stray download — is left without one so the report can
    # list it as unidentified rather than as a failure.
    return mod


def scan_mods(
    directory: Path, logger: Optional[Any] = None, hashes: bool = True
) -> ScanResult:
    """Scan a ``mods/`` folder. Missing directory yields an empty, well-formed result.

    A directory that exists but cannot be listed also yields an empty result — with
    :attr:`ScanResult.directory_error` set, so the caller can say "I could not read your mods
    folder" instead of "you have no mods". Never raises.

    :param hashes: passed through to :func:`scan_jar`. ``False`` gives the same list of jars
        with the same metadata and no bytes read, for the callers that are describing the
        folder rather than identifying its contents.
    """
    log = logger or _LOGGER
    result = ScanResult(directory=str(directory))
    jars, disabled, failure = _iter_mod_jars(directory)
    result.disabled = disabled
    result.directory_error = failure
    if failure:
        # Said out loud here as well as reported through the result: this is the log line an
        # admin will actually read, and it names the folder and the reason in one place.
        log.warning("could not list the mods directory %s: %s", directory, failure)

    for jar in jars:
        try:
            result.mods.append(scan_jar(jar, hashes=hashes))
        except Exception as error:  # noqa: BLE001 - one bad jar must not stop the scan
            log.warning("failed to inspect %s: %s", jar.name, error)
            result.mods.append(
                ScannedMod(
                    file_name=jar.name,
                    path=str(jar),
                    error="{}: {}".format(type(error).__name__, error),
                )
            )
    return result


def missing_dependencies(scan: ScanResult) -> Dict[str, List[str]]:
    """Hard dependencies no jar in the folder satisfies: ``{mod id: [dependent files]}``.

    This is the same kind of finding as a duplicate mod id or a client-only mod in a server
    folder — it is not an update, but it explains more crashes than a stale jar does, and the
    information was already sitting in the metadata being read for other reasons.

    A dependency counts as satisfied when some jar's own mod id matches, **or** when some jar
    declares it in ``provides``: a mod that says "I am a drop-in replacement for X" is what
    the loader will use to satisfy a requirement for X, so ignoring it would produce a
    warning about a server that is working.

    Two known ways to be wrong, both deliberate and both worth less than the finding is worth:

    * a dependency **bundled inside another jar** (Fabric "jar-in-jar") is satisfied at
      runtime but invisible here, since nested jars are counted rather than opened;
    * a dependency sitting in ``mods/`` under a name that only *that* jar's metadata would
      reveal, inside a ``.disabled``/``.old`` file, is likewise invisible.

    Both make this list an over-report rather than an under-report, which is the direction
    worth erring in for a warning — and the caller words it as such.
    """
    available: set = set()
    for mod in scan.mods:
        if not mod.metadata:
            continue
        if mod.metadata.mod_id:
            available.add(mod.metadata.mod_id.lower())
        for provided in mod.metadata.provides:
            available.add(str(provided).lower())

    missing: Dict[str, List[str]] = {}
    for mod in scan.mods:
        if not mod.metadata:
            continue
        own = mod.metadata.mod_id.lower()
        for required in mod.metadata.requires:
            if required in available or required == own:
                continue
            missing.setdefault(required, []).append(mod.file_name)

    return {key: sorted(set(files)) for key, files in sorted(missing.items())}
