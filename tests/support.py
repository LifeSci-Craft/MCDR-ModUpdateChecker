"""Test fixtures: build real jar files and real ``mods/`` folders on disk.

The plugin's scanner is only interesting because real jars are messy — metadata in four
different formats, ``depends`` given as a string in one release and a list in the next, a
jar that is a library rather than a mod. So the tests build genuine zip archives with
genuine metadata files instead of stubbing the scanner out, and the digests the upstream
fakes are keyed on are the digests of those real bytes.
"""

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from mod_update_checker.digests import digests_of_file

__all__ = [
    "fabric_metadata",
    "write_jar",
    "write_plain_file",
    "digests_of",
    "make_mods_directory",
]

DEFAULT_FABRIC: Dict[str, Any] = {
    "schemaVersion": 1,
    "id": "example_mod",
    "version": "1.0.0",
    "name": "Example Mod",
    "description": "test fixture",
    "authors": ["Tester"],
    "contact": {"homepage": "https://example.invalid/mod"},
    "depends": {"minecraft": ">=26.3", "fabricloader": ">=0.16.0"},
    "environment": "*",
    "entrypoints": {"main": ["com.example.Mod"]},
}


def fabric_metadata(**overrides: Any) -> Dict[str, Any]:
    """A ``fabric.mod.json`` dict, with ``overrides`` merged over the defaults."""
    data = dict(DEFAULT_FABRIC)
    data.update(overrides)
    return data


def write_jar(
    path: Path,
    *,
    fabric: Optional[Dict[str, Any]] = None,
    quilt: Optional[Dict[str, Any]] = None,
    mods_toml: Optional[str] = None,
    neoforge_toml: Optional[str] = None,
    extra_entries: Optional[Dict[str, bytes]] = None,
    nested_jars: int = 0,
) -> Path:
    """Write a jar carrying whichever metadata files were asked for."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        if fabric is not None:
            archive.writestr("fabric.mod.json", json.dumps(fabric, ensure_ascii=False))
        if quilt is not None:
            archive.writestr("quilt.mod.json", json.dumps(quilt, ensure_ascii=False))
        if mods_toml is not None:
            archive.writestr("META-INF/mods.toml", mods_toml)
        if neoforge_toml is not None:
            archive.writestr("META-INF/neoforge.mods.toml", neoforge_toml)
        for name, payload in (extra_entries or {}).items():
            archive.writestr(name, payload)
        for index in range(nested_jars):
            archive.writestr(
                "META-INF/jars/nested{}.jar".format(index), b"PK\x03\x04nested"
            )
        # A real jar is never metadata-only; a payload makes the bytes less homogeneous and
        # therefore a slightly better test of the hashing paths.
        archive.writestr("com/example/Mod.class", b"\xca\xfe\xba\xbe" + bytes(range(256)))
    return path


def write_plain_file(path: Path, payload: bytes = b"not a zip at all\n") -> Path:
    """Write something that is not a jar, to check the scanner degrades instead of dying."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def digests_of(path: Path) -> Tuple[str, str, int]:
    """``(sha1, sha512, size)`` for a file on disk."""
    with open(path, "rb") as handle:
        return digests_of_file(handle)


def sha1_of(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def make_mods_directory(
    root: Path, jars: Iterable[Tuple[str, Dict[str, Any]]]
) -> List[Path]:
    """Create ``<root>/mods`` with one jar per ``(file_name, fabric_metadata)`` pair."""
    directory = root / "mods"
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for file_name, metadata in jars:
        written.append(write_jar(directory / file_name, fabric=metadata))
    return written
