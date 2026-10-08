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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .digests import digests_of_file
from .versioning import RangeSpec, normalize_spec

__all__ = [
    "MOD_METADATA_ENTRIES",
    "ModMetadata",
    "ScannedMod",
    "ScanResult",
    "resolve_mods_directory",
    "iter_mod_jars",
    "scan_jar",
    "scan_mods",
]

_LOGGER = logging.getLogger(__name__)

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
    sha1: str = ""
    sha512: str = ""
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
    """
    if not directory.is_dir():
        return [], []

    jars: List[Path] = []
    disabled: List[str] = []
    for entry in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
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
    return jars, disabled


# --------------------------------------------------------------------------------------
# Metadata extraction
# --------------------------------------------------------------------------------------


def _loads_lenient(raw: str) -> Any:
    """Parse JSON, then retry after stripping the JSON5-isms that turn up in the wild."""
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


def _metadata_from_fabric(data: Dict[str, Any], entry: str, loader: str) -> ModMetadata:
    depends = data.get("depends") if isinstance(data.get("depends"), dict) else {}
    # ``_string_list`` already flattens the bare-string form, so ``provides`` needs no
    # special case of its own here.
    provided = _string_list(data.get("provides"))
    return ModMetadata(
        mod_id=_first_string(data.get("id")),
        name=_first_string(data.get("name")) or _first_string(data.get("id")),
        version=_first_string(data.get("version")),
        loader=loader,
        metadata_file=entry,
        mc_range=depends.get("minecraft"),
        environment=_first_string(data.get("environment")) or None,
        authors=_string_list(data.get("authors")) or _string_list(data.get("author")),
        description=_first_string(data.get("description")) or None,
        sources=_contact_links(data.get("contact")),
        provides=tuple(item for item in provided if item),
    )


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
                mc_range = dependency["minecraft"]
    elif isinstance(depends, dict):
        mc_range = depends.get("minecraft")
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
        mc_range=_minecraft_range_from_dependencies(parsed.get("dependencies")),
        environment=None,
        authors=_string_list(first.get("authors")),
        description=str(first.get("description", "") or "") or None,
        sources=(),
    )


def _metadata_from_toml_regex(raw: str, entry: str, loader: str) -> ModMetadata:
    """The ``tomllib``-free reader.

    Narrow on purpose: it takes the first ``[[mods]]`` block's id, version and name, plus the
    ``minecraft`` dependency's range — the four things this plugin actually uses. It is not
    a TOML parser, and it is tested directly rather than only through the ``tomllib`` path,
    which is the one most interpreters will take.
    """
    match = _TOML_MODS_BLOCK.search(raw)
    block = match.group(1) if match else raw

    mc_range: RangeSpec = None
    for dependency in _TOML_DEP_BLOCK.finditer(raw):
        body = dependency.group(1)
        if _toml_value(body, "modId").lower() == "minecraft":
            mc_range = _toml_value(body, "versionRange") or None
            break

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


def read_metadata(archive: zipfile.ZipFile) -> Optional[ModMetadata]:
    """Extract mod metadata from an open jar, or ``None`` if nothing recognisable is there."""
    names = archive.namelist()
    available = set(names)
    for entry, loader in MOD_METADATA_ENTRIES:
        if entry not in available:
            continue
        try:
            raw = archive.read(entry).decode("utf-8", errors="replace")
        except (KeyError, OSError, zipfile.BadZipFile):
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


def scan_jar(path: Path) -> ScannedMod:
    """Hash one jar and read its metadata. Never raises."""
    mod = ScannedMod(file_name=path.name, path=str(path))
    try:
        mod.mtime = path.stat().st_mtime
        with open(path, "rb") as handle:
            mod.sha1, mod.sha512, mod.size = digests_of_file(handle)
    except OSError as error:
        mod.error = "{}: {}".format(type(error).__name__, error)
        return mod

    try:
        with zipfile.ZipFile(mod.path) as archive:
            mod.metadata = read_metadata(archive)
    except (zipfile.BadZipFile, OSError) as error:
        mod.error = "not a readable jar ({}: {})".format(type(error).__name__, error)
        return mod

    # ``metadata is None`` is not an error: a perfectly valid jar that simply is not a mod — a
    # library, a datapack archive, a stray download — is left without one so the report can
    # list it as unidentified rather than as a failure.
    return mod


def scan_mods(directory: Path, logger: Optional[Any] = None) -> ScanResult:
    """Scan a ``mods/`` folder. Missing directory yields an empty, well-formed result."""
    log = logger or _LOGGER
    result = ScanResult(directory=str(directory))
    jars, disabled = iter_mod_jars(directory)
    result.disabled = disabled

    for jar in jars:
        try:
            result.mods.append(scan_jar(jar))
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
