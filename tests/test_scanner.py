"""Reading a real ``mods/`` folder.

Everything here builds actual jars on disk. The scanner's whole reason to exist is that real
metadata is inconsistent — four formats, ``depends`` as a string in one release and a list in
the next, a jar that turns out to be a library — so testing it against a stub would test
nothing.
"""

import hashlib
import zipfile
from pathlib import Path

from mod_update_checker.scanner import (
    _metadata_from_toml_regex,
    _toml_string_list,
    _toml_value,
    iter_mod_jars,
    missing_dependencies,
    read_metadata,
    resolve_mods_directory,
    scan_jar,
    scan_mods,
)

from support import fabric_metadata, write_jar, write_plain_file

MODS_TOML = """\
modLoader="javafml"
loaderVersion="[47,)"
license="MIT"

[[mods]]
modId="jei"
version="15.2.0.27"
displayName="Just Enough Items"
description='''
View Items and Recipes
'''
authors="mezz"

[[dependencies.jei]]
    modId="forge"
    mandatory=true
    versionRange="[47,)"
    ordering="NONE"
    side="BOTH"

[[dependencies.jei]]
    modId="minecraft"
    mandatory=true
    versionRange="[1.20.1,1.21)"
    ordering="NONE"
    side="BOTH"
"""

QUILT_JSON = {
    "schema_version": 1,
    "quilt_loader": {
        "id": "quilt_example",
        "version": "3.4.5",
        "metadata": {
            "name": "Quilt Example",
            "description": "a quilt mod",
            "contributors": {"Tester": "author"},
            "contact": {"homepage": "https://example.invalid/quilt"},
        },
        "depends": [{"minecraft": ">=26.3"}, {"quilt_loader": ">=0.20"}],
        "provides": ["quilt_alias"],
    },
}


# --------------------------------------------------------------------------------------
# Directory resolution
# --------------------------------------------------------------------------------------


def test_resolve_mods_directory_defaults_to_server_working_directory(tmp_path):
    assert resolve_mods_directory(str(tmp_path)) == tmp_path / "mods"
    assert resolve_mods_directory(str(tmp_path), "") == tmp_path / "mods"
    assert resolve_mods_directory(str(tmp_path), "   ") == tmp_path / "mods"


def test_resolve_mods_directory_relative_is_relative_to_the_server(tmp_path):
    """Not to MCDR's own folder — "mods" means the server's mods folder."""
    assert resolve_mods_directory(str(tmp_path), "mods") == tmp_path / "mods"
    assert resolve_mods_directory(str(tmp_path), "jars/mods") == tmp_path / "jars" / "mods"


def test_resolve_mods_directory_absolute_is_used_as_given(tmp_path):
    absolute = tmp_path / "elsewhere" / "mods"
    assert resolve_mods_directory("C:/somewhere/else", str(absolute)) == absolute


def test_iter_mod_jars(tmp_path):
    directory = tmp_path / "mods"
    directory.mkdir()
    (directory / "sodium.jar").write_bytes(b"")
    (directory / "SODIUM-UPPER.JAR").write_bytes(b"")
    (directory / "old.jar.disabled").write_bytes(b"")
    (directory / "older.jar.old").write_bytes(b"")
    (directory / "backup.jar.bak").write_bytes(b"")
    (directory / "notes.txt").write_bytes(b"")
    (directory / "subdir").mkdir()

    jars, disabled = iter_mod_jars(directory)

    assert [item.name for item in jars] == ["SODIUM-UPPER.JAR", "sodium.jar"]
    assert sorted(disabled) == ["backup.jar.bak", "old.jar.disabled", "older.jar.old"]


def test_iter_mod_jars_on_a_missing_directory():
    assert iter_mod_jars(Path("does/not/exist")) == ([], [])


# --------------------------------------------------------------------------------------
# Fabric metadata
# --------------------------------------------------------------------------------------


def test_scan_jar_reads_fabric_metadata(tmp_path):
    path = write_jar(
        tmp_path / "mods" / "example.jar",
        fabric=fabric_metadata(
            id="example_mod",
            version="2.3.4",
            name="Example Mod",
            authors=["Alice", {"name": "Bob"}],
            contact={"sources": "https://github.com/example/mod", "homepage": "https://x.invalid"},
            depends={"minecraft": ">=26.3", "fabricloader": ">=0.16.0"},
        ),
        nested_jars=2,
    )

    mod = scan_jar(path)

    assert mod.error is None
    assert mod.identified
    assert mod.mod_id == "example_mod"
    assert mod.name == "Example Mod"
    assert mod.version == "2.3.4"
    assert mod.metadata.loader == "fabric"
    assert mod.metadata.metadata_file == "fabric.mod.json"
    assert mod.metadata.mc_range == ">=26.3"
    assert mod.metadata.bundled_jars == 2
    assert mod.metadata.sources[0] == "https://github.com/example/mod"
    assert set(mod.metadata.authors) == {"Alice", "Bob"}


def test_scan_jar_records_the_three_digests(tmp_path):
    payload_jar = write_jar(tmp_path / "mods" / "example.jar", fabric=fabric_metadata())
    raw = payload_jar.read_bytes()

    mod = scan_jar(payload_jar)

    assert mod.sha1 == hashlib.sha1(raw).hexdigest()
    assert mod.sha512 == hashlib.sha512(raw).hexdigest()
    assert mod.size == len(raw)
    assert mod.mtime > 0


def test_depends_minecraft_as_a_list_is_kept_as_an_alternative_list(tmp_path):
    path = write_jar(
        tmp_path / "a.jar",
        fabric=fabric_metadata(depends={"minecraft": [">=1.21 <1.22", ">=26.3"]}),
    )
    mod = scan_jar(path)
    assert mod.metadata.mc_range == [">=1.21 <1.22", ">=26.3"]
    assert mod.metadata.mc_range_text == ">=1.21 <1.22 | >=26.3"


def test_missing_depends_yields_no_range(tmp_path):
    path = write_jar(tmp_path / "a.jar", fabric=fabric_metadata(depends={}))
    assert scan_jar(path).metadata.mc_range is None


def test_provides_as_a_bare_string(tmp_path):
    path = write_jar(tmp_path / "a.jar", fabric=fabric_metadata(provides="alias_id"))
    assert scan_jar(path).metadata.provides == ("alias_id",)


def test_environment_is_recorded_so_client_only_mods_can_be_flagged(tmp_path):
    client = write_jar(tmp_path / "c.jar", fabric=fabric_metadata(environment="client"))
    both = write_jar(tmp_path / "b.jar", fabric=fabric_metadata(environment="*"))
    assert scan_jar(client).metadata.is_client_only is True
    assert scan_jar(both).metadata.is_client_only is False


def test_version_placeholder_is_dropped(tmp_path):
    """A few releases ship ``${version}`` unexpanded; reporting that verbatim is useless."""
    path = write_jar(tmp_path / "a.jar", fabric=fabric_metadata(version="${version}"))
    assert scan_jar(path).metadata.version == ""


def test_fabric_json_with_comments_and_a_trailing_comma(tmp_path):
    """Fabric's own parser is lenient, so JSON5-isms do reach jars in the wild."""
    raw = """{
  // the id
  "schemaVersion": 1,
  "id": "lenient_mod",
  "version": "1.0.0",
  "name": "Lenient",
  "depends": {"minecraft": ">=26.3"},
}"""
    path = tmp_path / "lenient.jar"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("fabric.mod.json", raw)
        archive.writestr("com/x.class", b"\xca\xfe\xba\xbe")

    assert scan_jar(path).metadata.mod_id == "lenient_mod"


def test_metadata_id_is_required(tmp_path):
    """A ``fabric.mod.json`` without an id identifies nothing, so the jar stays unidentified."""
    path = write_jar(tmp_path / "a.jar", fabric={"schemaVersion": 1, "version": "1.0"})
    mod = scan_jar(path)
    assert mod.identified is False
    assert mod.error is None


# --------------------------------------------------------------------------------------
# Quilt and TOML
# --------------------------------------------------------------------------------------


def test_scan_jar_reads_quilt_metadata(tmp_path):
    path = write_jar(tmp_path / "q.jar", quilt=QUILT_JSON)
    mod = scan_jar(path)
    assert mod.mod_id == "quilt_example"
    assert mod.version == "3.4.5"
    assert mod.name == "Quilt Example"
    assert mod.metadata.loader == "quilt"
    assert mod.metadata.mc_range == ">=26.3"
    assert mod.metadata.provides == ("quilt_alias",)


def test_scan_jar_reads_forge_mods_toml(tmp_path):
    path = write_jar(tmp_path / "f.jar", mods_toml=MODS_TOML)
    mod = scan_jar(path)
    assert mod.mod_id == "jei"
    assert mod.version == "15.2.0.27"
    assert mod.name == "Just Enough Items"
    assert mod.metadata.loader == "forge"
    assert mod.metadata.mc_range == "[1.20.1,1.21)"


def test_scan_jar_reads_neoforge_toml_and_prefers_it_over_forge(tmp_path):
    neoforge = MODS_TOML.replace('modId="jei"', 'modId="neoforge_mod"')
    path = write_jar(tmp_path / "n.jar", mods_toml=MODS_TOML, neoforge_toml=neoforge)
    mod = scan_jar(path)
    assert mod.mod_id == "neoforge_mod"
    assert mod.metadata.loader == "neoforge"
    assert mod.metadata.metadata_file == "META-INF/neoforge.mods.toml"


def test_toml_regex_reader_without_tomllib():
    """The fallback path, exercised directly.

    Most interpreters have ``tomllib``, so the regex reader would otherwise only ever be
    reached on 3.10 and older — i.e. exactly on the machines nobody tests on. Its behaviour
    is pinned here instead.
    """
    metadata = _metadata_from_toml_regex(MODS_TOML, "META-INF/mods.toml", "forge")
    assert metadata.mod_id == "jei"
    assert metadata.version == "15.2.0.27"
    assert metadata.name == "Just Enough Items"
    assert metadata.mc_range == "[1.20.1,1.21)"
    assert metadata.metadata_file == "META-INF/mods.toml"


def test_toml_regex_reader_ignores_malformed_input():
    metadata = _metadata_from_toml_regex("this is not toml at all", "META-INF/mods.toml", "forge")
    assert metadata.mod_id == ""
    assert metadata.mc_range is None


def test_toml_value_and_string_list_helpers():
    block = 'modId = "abc"\nauthors = ["A", "B"]\nother = "z"'
    assert _toml_value(block, "modId") == "abc"
    assert _toml_value(block, "missing") == ""
    assert _toml_string_list(block, "authors") == ("A", "B")
    assert _toml_string_list(block, "modId") == ("abc",)
    assert _toml_string_list(block, "missing") == ()


def test_read_metadata_returns_none_for_a_non_mod_jar(tmp_path):
    path = tmp_path / "library.jar"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("net/example/Lib.class", b"\xca\xfe\xba\xbe")
    with zipfile.ZipFile(path) as archive:
        assert read_metadata(archive) is None


# --------------------------------------------------------------------------------------
# Degrading gracefully
# --------------------------------------------------------------------------------------


def test_a_file_that_is_not_a_zip_is_reported_not_raised(tmp_path):
    path = write_plain_file(tmp_path / "corrupt.jar")
    mod = scan_jar(path)
    assert mod.identified is False
    assert mod.error is not None
    assert "BadZipFile" in mod.error or "not a readable jar" in mod.error
    # It was still hashed, so it can still be looked up upstream.
    assert len(mod.sha1) == 40


def test_one_bad_jar_does_not_stop_the_scan(tmp_path, caplog):
    directory = tmp_path / "mods"
    write_jar(directory / "good.jar", fabric=fabric_metadata(id="good"))
    write_plain_file(directory / "broken.jar")

    result = scan_mods(directory)

    assert len(result.mods) == 2
    assert len(result.identified) == 1
    assert len(result.unidentified) == 1


def test_scan_mods_on_a_missing_directory(tmp_path):
    result = scan_mods(tmp_path / "nope")
    assert result.mods == []
    assert result.disabled == []


def test_scan_mods_reports_disabled_jars_separately(tmp_path):
    directory = tmp_path / "mods"
    write_jar(directory / "active.jar", fabric=fabric_metadata(id="active"))
    (directory / "previous.jar.disabled").write_bytes(b"whatever")

    result = scan_mods(directory)

    assert [mod.mod_id for mod in result.identified] == ["active"]
    assert result.disabled == ["previous.jar.disabled"]


# --------------------------------------------------------------------------------------
# Cross-jar analysis
# --------------------------------------------------------------------------------------


def test_duplicate_mod_ids_are_detected(tmp_path):
    """Two jars of one mod is the classic consequence of an update done by hand."""
    directory = tmp_path / "mods"
    write_jar(directory / "sodium-0.5.8.jar", fabric=fabric_metadata(id="sodium", version="0.5.8"))
    write_jar(directory / "sodium-0.5.9.jar", fabric=fabric_metadata(id="sodium", version="0.5.9"))
    write_jar(directory / "lithium.jar", fabric=fabric_metadata(id="lithium"))

    duplicates = scan_mods(directory).duplicate_ids()

    assert list(duplicates) == ["sodium"]
    assert [mod.file_name for mod in duplicates["sodium"]] == [
        "sodium-0.5.8.jar",
        "sodium-0.5.9.jar",
    ]


def test_client_only_jar_is_listed(tmp_path):
    directory = tmp_path / "mods"
    write_jar(directory / "server_mod.jar", fabric=fabric_metadata(id="server_mod", environment="*"))
    write_jar(directory / "hud.jar", fabric=fabric_metadata(id="hud", environment="client"))

    assert [mod.mod_id for mod in scan_mods(directory).client_only()] == ["hud"]


def test_name_falls_back_to_the_file_name_when_metadata_is_absent(tmp_path):
    path = write_plain_file(tmp_path / "unknown.jar")
    mod = scan_jar(path)
    assert mod.name == "unknown.jar"
    assert mod.version == ""


# --------------------------------------------------------------------------------------
# Declared dependencies
#
# ``requires`` exists so a missing library can be reported, which is a different kind of
# finding from "this jar is out of date" — it explains a crash rather than a stale file. What
# is tested here is what gets *left out*, because that is where the false positives live: a
# warning that fires on a working server teaches an admin to ignore the warning line.
# --------------------------------------------------------------------------------------


def test_fabric_requires_lists_the_other_mods_not_the_platform(tmp_path):
    path = write_jar(
        tmp_path / "a.jar",
        fabric=fabric_metadata(
            id="a",
            depends={
                "minecraft": ">=26.3",
                "fabricloader": ">=0.16.0",
                "java": ">=17",
                "fabric-api": "*",
                "cloth-config": ">=12",
            },
        ),
    )

    assert scan_jar(path).metadata.requires == ("cloth-config", "fabric-api")


def test_fabric_requires_ignores_recommends_and_suggests(tmp_path):
    """A mod that merely *prefers* another one is not a broken server."""
    path = write_jar(
        tmp_path / "b.jar",
        fabric=fabric_metadata(
            id="b",
            depends={"minecraft": ">=26.3"},
            recommends={"modmenu": "*"},
            suggests={"sodium": "*"},
        ),
    )

    assert scan_jar(path).metadata.requires == ()


def test_quilt_requires_reads_the_list_form(tmp_path):
    quilt = dict(QUILT_JSON)
    quilt["quilt_loader"] = dict(QUILT_JSON["quilt_loader"])
    quilt["quilt_loader"]["depends"] = [
        {"minecraft": ">=26.3"},
        {"quilt_loader": ">=0.20"},
        {"id": "cloth-config", "versions": ">=12"},
    ]
    path = write_jar(tmp_path / "q.jar", quilt=quilt)

    assert scan_jar(path).metadata.requires == ("cloth-config",)


def test_forge_requires_includes_the_mods_but_not_forge_or_minecraft(tmp_path):
    mods_toml = MODS_TOML + """
[[dependencies.jei]]
    modId="cloth_config"
    mandatory=true
    versionRange="[12,)"

[[dependencies.jei]]
    modId="jei_plugin_api"
    mandatory=false
"""
    path = write_jar(tmp_path / "f.jar", mods_toml=mods_toml)

    # ``mandatory=false`` is a preference, so the last one is dropped.
    assert scan_jar(path).metadata.requires == ("cloth_config",)


def test_the_regex_reader_agrees_with_the_parser_about_requires():
    """The two readers have to answer the same question, or 3.10 sees a different report.

    ``tomllib`` is absent on 3.10 and older, which is precisely the interpreter nobody
    develops on — so the fallback is asserted against the same input rather than trusted.
    """
    mods_toml = MODS_TOML + """
[[dependencies.jei]]
    modId="cloth_config"
    mandatory=true
    versionRange="[12,)"

[[dependencies.jei]]
    modId="optional_thing"
    mandatory=false
"""
    metadata = _metadata_from_toml_regex(mods_toml, "META-INF/mods.toml", "forge")

    assert metadata.requires == ("cloth_config",)
    assert metadata.mc_range == "[1.20.1,1.21)"


def test_a_required_mod_that_is_present_is_not_missing(tmp_path):
    directory = tmp_path / "mods"
    write_jar(
        directory / "needs.jar",
        fabric=fabric_metadata(id="needs", depends={"fabric-api": "*", "minecraft": ">=26.3"}),
    )
    write_jar(directory / "fabric-api.jar", fabric=fabric_metadata(id="fabric-api"))

    assert missing_dependencies(scan_mods(directory)) == {}


def test_a_provided_id_satisfies_a_requirement(tmp_path):
    """``provides`` is how a mod says "I am a drop-in replacement for X".

    Ignoring it would warn about a server that is working, which is the failure mode worth
    spending a test on.
    """
    directory = tmp_path / "mods"
    write_jar(
        directory / "needs.jar",
        fabric=fabric_metadata(id="needs", depends={"old-api": "*", "minecraft": ">=26.3"}),
    )
    write_jar(
        directory / "replacement.jar",
        fabric=fabric_metadata(id="replacement", provides=["old-api"]),
    )

    assert missing_dependencies(scan_mods(directory)) == {}


def test_a_missing_dependency_names_the_jars_that_want_it(tmp_path):
    directory = tmp_path / "mods"
    for name in ("one", "two"):
        write_jar(
            directory / "{}.jar".format(name),
            fabric=fabric_metadata(
                id=name, depends={"cloth-config": "*", "minecraft": ">=26.3"}
            ),
        )

    assert missing_dependencies(scan_mods(directory)) == {
        "cloth-config": ["one.jar", "two.jar"]
    }


def test_a_mod_that_depends_on_itself_is_not_reported(tmp_path):
    """Nonsense metadata, but nonsense that must not produce a warning about nothing."""
    directory = tmp_path / "mods"
    write_jar(
        directory / "self.jar",
        fabric=fabric_metadata(id="self", depends={"self": "*"}),
    )

    assert missing_dependencies(scan_mods(directory)) == {}


def test_a_jar_with_no_metadata_contributes_nothing(tmp_path):
    directory = tmp_path / "mods"
    write_jar(directory / "needs.jar", fabric=fabric_metadata(id="needs", depends={"gone": "*"}))
    write_plain_file(directory / "library.jar")

    assert missing_dependencies(scan_mods(directory)) == {"gone": ["needs.jar"]}
