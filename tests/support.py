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

__all__ = [
    "fabric_metadata",
    "write_jar",
    "write_plain_file",
    "digests_of",
    "make_mods_directory",
    "flatten_options",
    "option_paths",
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
    # Imported here rather than at module level: importing any ``mod_update_checker`` submodule
    # runs the package ``__init__``, which imports MCDR. That would make this module — and the
    # plain dict helpers in it — unusable from ``tools/mcdr_matrix.py``, which runs on an
    # interpreter with no MCDR installed.
    from mod_update_checker.digests import digests_of_file

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


def flatten_options(mapping: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """Expand a nested config into ``{"section.option": value}``.

    The config file groups options into sections (``check``, ``download``, …), so talking about
    "every option" — or about which ones a test has overridden — means talking about dotted
    paths rather than top-level names. A value that is itself a dict is a section; everything
    else is an option.

    Deliberately free of any MCDR import, so the cross-version matrix tool can use it too: that
    tool runs on an interpreter with no MCDR installed.
    """
    flat: Dict[str, Any] = {}
    for name, value in mapping.items():
        path = "{}.{}".format(prefix, name) if prefix else name
        if isinstance(value, dict):
            flat.update(flatten_options(value, path))
        else:
            flat[path] = value
    return flat


def option_paths(config_class) -> set:
    """Every settable option of a config class as a dotted path, sections included.

    Duck-typed on purpose: a nested section is anything that can list its own fields, so this
    needs no MCDR import and stays usable from the tooling that runs without it.

    Read off the type annotations rather than off a serialised instance, because MCDR's
    ``serialize()`` leaves a nested section as an object rather than a plain dict — a generic
    dict-walker would see the section names and miss every option inside them.
    """
    paths = set()
    for name, annotation in config_class.get_field_annotations().items():
        if hasattr(annotation, "get_field_annotations"):
            paths |= {"{}.{}".format(name, child) for child in option_paths(annotation)}
        else:
            paths.add(name)
    return paths
