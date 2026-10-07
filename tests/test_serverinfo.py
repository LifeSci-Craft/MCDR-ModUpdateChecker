"""Working out which Minecraft version and loader the server runs.

Getting this wrong is the most damaging failure the plugin has, and the quietest: filter
Modrinth by the wrong game version and every mod comes back as "no compatible build", which
looks exactly like a real answer. So each source is tested independently, and the order in
which they are trusted is tested too.
"""

import pytest

from mod_update_checker.scanner import scan_mods
from mod_update_checker.serverinfo import (
    detect,
    detect_loader_from_mods,
    mod_supports_server_version,
    parse_log_text,
    read_log_head,
    sanitize_version,
)

from support import fabric_metadata, write_jar

FABRIC_STARTUP = """\
[12:00:00] [main/INFO]: Loading Minecraft 26.3 with Fabric Loader 0.18.1
[12:00:00] [main/INFO]: Loading 87 mods:
[12:00:03] [Server thread/INFO]: Starting minecraft server version 26.3
[12:00:05] [Server thread/INFO]: Done (2.113s)! For help, type "help"
"""

VANILLA_STARTUP = """\
[12:00:00] [main/INFO]: Starting minecraft server version 26.3
[12:00:02] [Server thread/INFO]: Done (1.5s)!
"""

QUILT_STARTUP = """\
[12:00:00] [main/INFO]: Loading Minecraft 1.21.4 with Quilt Loader 0.27.1
"""


@pytest.mark.parametrize(
    "text,expected",
    [
        (FABRIC_STARTUP, ("26.3", "fabric", "0.18.1")),
        (VANILLA_STARTUP, ("26.3", None, None)),
        (QUILT_STARTUP, ("1.21.4", "quilt", "0.27.1")),
        ("Loading Minecraft 26.3 with NeoForge Loader 21.1.0\n", ("26.3", "neoforge", "21.1.0")),
        ("nothing relevant here\n", (None, None, None)),
        ("", (None, None, None)),
    ],
)
def test_parse_log_text(text, expected):
    assert parse_log_text(text) == expected


def test_the_loader_banner_wins_over_the_vanilla_line():
    """Both lines appear in a Fabric log; the banner names the loader, so it is preferred."""
    game, loader, loader_version = parse_log_text(FABRIC_STARTUP)
    assert (game, loader) == ("26.3", "fabric")
    assert loader_version == "0.18.1"


def test_parse_prefers_a_match_anywhere_over_an_earlier_unrelated_line():
    text = "some launcher chatter\n" + FABRIC_STARTUP
    assert parse_log_text(text)[0] == "26.3"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("26.3", "26.3"),
        ("1.17 Release Candidate 1", "1.17"),
        ("  26.3  ", "26.3"),
        ("1.21.4-pre1", "1.21.4-pre1"),
        (None, None),
        ("", None),
        ("   ", None),
    ],
)
def test_sanitize_version(raw, expected):
    """MCDR documents ``ServerInformation.version`` as possibly holding
    ``"1.17 Release Candidate 1"``, which is not usable as a game version as-is."""
    assert sanitize_version(raw) == expected


def test_read_log_head(tmp_path):
    path = tmp_path / "logs" / "latest.log"
    path.parent.mkdir(parents=True)
    path.write_text(FABRIC_STARTUP, encoding="utf-8")
    assert "Loading Minecraft 26.3" in read_log_head(path)


def test_read_log_head_on_a_missing_file(tmp_path):
    assert read_log_head(tmp_path / "nope" / "latest.log") == ""


def test_read_log_head_tolerates_undecodable_bytes(tmp_path):
    path = tmp_path / "logs" / "latest.log"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe Loading Minecraft 26.3\n")
    assert "Loading Minecraft 26.3" in read_log_head(path)


def make_mods(tmp_path, entries):
    """``entries``: ``(file_name, mod_id, mc_range_or_None, loader)``."""
    directory = tmp_path / "mods"
    directory.mkdir(parents=True, exist_ok=True)
    for file_name, mod_id, mc_range, loader in entries:
        if loader == "forge":
            write_jar(
                directory / file_name,
                mods_toml='[[mods]]\nmodId="{}"\nversion="1.0.0"\n'.format(mod_id),
            )
        else:
            depends = {} if mc_range is None else {"minecraft": mc_range}
            write_jar(directory / file_name, fabric=fabric_metadata(id=mod_id, depends=depends))
    return scan_mods(directory)


# --------------------------------------------------------------------------------------
# detect(): the order sources are trusted in
# --------------------------------------------------------------------------------------


def test_config_override_wins_over_everything(tmp_path):
    scan = make_mods(tmp_path, [("a.jar", "a", ">=1.20.1", "fabric")])
    context = detect(
        working_directory=str(tmp_path),
        configured_version="26.3",
        server_information_version="1.21.4",
        scan=scan,
    )
    assert context.mc_version == "26.3"
    assert context.mc_version_source == "config"


def test_auto_falls_through_to_server_information(tmp_path):
    context = detect(
        working_directory=str(tmp_path),
        configured_version="auto",
        server_information_version="26.3",
    )
    assert context.mc_version == "26.3"
    assert context.mc_version_source == "server_info"


def test_server_information_is_sanitised(tmp_path):
    context = detect(
        working_directory=str(tmp_path),
        configured_version="auto",
        server_information_version="1.17 Release Candidate 1",
    )
    assert context.mc_version == "1.17"


def test_a_log_file_is_used_when_the_server_is_offline(tmp_path):
    path = tmp_path / "logs" / "latest.log"
    path.parent.mkdir(parents=True)
    path.write_text(FABRIC_STARTUP, encoding="utf-8")

    context = detect(working_directory=str(tmp_path), configured_version="auto")

    assert context.mc_version == "26.3"
    assert context.mc_version_source == "log"
    assert context.loader == "fabric"
    assert context.loader_version == "0.18.1"


def test_mod_metadata_is_the_last_resort(tmp_path):
    scan = make_mods(
        tmp_path,
        [
            ("a.jar", "a", ">=26.3", "fabric"),
            ("b.jar", "b", ">=26.3", "fabric"),
            ("c.jar", "c", ">=1.20.1", "fabric"),
        ],
    )
    context = detect(working_directory=str(tmp_path), configured_version="auto", scan=scan)
    assert context.mc_version == "26.3"
    assert context.mc_version_source == "mods"


def test_unknown_when_nothing_can_be_determined(tmp_path):
    context = detect(working_directory=str(tmp_path), configured_version="auto")
    assert context.mc_version is None
    assert context.mc_version_source == "unknown"
    assert context.known is False


def test_loader_is_cross_checked_against_the_jars_on_disk(tmp_path):
    """A folder of Forge jars on a server configured as Fabric is worth surfacing."""
    scan = make_mods(
        tmp_path,
        [("a.jar", "a", None, "forge"), ("b.jar", "b", None, "forge")],
    )
    context = detect(
        working_directory=str(tmp_path), configured_loader="fabric", scan=scan
    )
    assert context.loader == "forge"
    assert context.loader_source == "mods"
    assert context.mod_loader_counts == {"forge": 2}


def test_loader_stays_as_configured_when_the_mods_agree(tmp_path):
    scan = make_mods(tmp_path, [("a.jar", "a", None, "fabric")])
    context = detect(working_directory=str(tmp_path), configured_loader="fabric", scan=scan)
    assert context.loader == "fabric"
    assert context.loader_source == "config+mods"


def test_detect_loader_from_mods_on_an_empty_scan(tmp_path):
    assert detect_loader_from_mods(scan_mods(tmp_path / "nothing")) == {}


def test_context_describe():
    context = detect(
        working_directory=".",
        configured_version="26.3",
        configured_loader="fabric",
    )
    assert context.describe() == "26.3 / Fabric"


def test_context_describe_without_a_version():
    context = detect(working_directory=".", configured_version="auto", configured_loader="fabric")
    assert context.describe() == "? / Fabric"


# --------------------------------------------------------------------------------------
# mod_supports_server_version
# --------------------------------------------------------------------------------------


def test_mod_supports_server_version(tmp_path):
    # Indexed by mod id: the scan returns jars in file-name order, which is not the order
    # they were declared in.
    mods = {
        mod.mod_id: mod
        for mod in make_mods(
            tmp_path,
            [
                ("good.jar", "good", ">=26.3", "fabric"),
                ("bad.jar", "bad", ">=1.20.1 <1.21", "fabric"),
                ("silent.jar", "silent", None, "fabric"),
            ],
        ).mods
    }

    assert mod_supports_server_version(mods["good"], "26.3") is True
    assert mod_supports_server_version(mods["bad"], "26.3") is False
    # No declared range means no opinion, which must not become a false alarm.
    assert mod_supports_server_version(mods["silent"], "26.3") is None
    assert mod_supports_server_version(mods["good"], None) is None
