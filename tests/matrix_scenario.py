"""The mods folder and upstream catalogue the MCDR matrix runs against.

Kept apart from ``mcdr_matrix.py`` so the tool stays readable, and importable on its own so
the pytest end-to-end test can plant exactly the same scenario.

The scenario is small but complete: one mod with a pending update, one already current, one
whose project publishes nothing for this Minecraft version, one CurseForge-only mod
identified by fingerprint rather than by hash, and one jar that is not a mod at all. Every
outcome the console can print should occur at least once, otherwise a regression that stops
printing one of them would still pass.
"""

import hashlib
from pathlib import Path
from typing import List

from fake_upstream import (
    FakeCfFile,
    FakeCfMod,
    FakeFile,
    FakeProject,
    FakeUpstream,
    FakeVersion,
)
from support import fabric_metadata, write_jar

#: Fixed so the matrix's expectations are stable.
GAME_VERSION = "26.3"
CF_MOD_ID = 555001


def sha1_of(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def build_scenario_jars(upstream: FakeUpstream, workdir: Path) -> List[Path]:
    """Fill ``upstream`` with a small catalogue and return the jars to install."""
    directory = workdir / "jars"
    directory.mkdir(parents=True, exist_ok=True)
    jars: List[Path] = []

    def add(file_name: str, **metadata) -> Path:
        path = write_jar(directory / file_name, fabric=fabric_metadata(**metadata))
        jars.append(path)
        return path

    def version(project_id, version_id, number, sha1, date, filename,
                game_versions=(GAME_VERSION,)) -> FakeVersion:
        return FakeVersion(
            id=version_id,
            project_id=project_id,
            version_number=number,
            game_versions=game_versions,
            date_published=date,
            files=[FakeFile(sha1=sha1, filename=filename, url="https://cdn.example/" + filename)],
        )

    # 1. Has a newer build waiting.
    outdated = add("outdated.jar", id="outdated", version="1.0.0", name="Outdated Mod")
    upstream.add_project(
        FakeProject(
            id="proj-outdated",
            slug="outdated",
            title="Outdated Mod",
            versions=[
                version("proj-outdated", "o-1", "1.0.0", sha1_of(outdated),
                        "2026-01-01T00:00:00Z", "outdated-1.0.0.jar"),
                version("proj-outdated", "o-2", "1.1.0", "1" * 40,
                        "2026-02-01T00:00:00Z", "outdated-1.1.0.jar"),
            ],
        )
    )

    # 2. Already current: the newest published file is the local one.
    current = add("current.jar", id="current", version="2.0.0", name="Current Mod")
    upstream.add_project(
        FakeProject(
            id="proj-current",
            slug="current",
            title="Current Mod",
            versions=[
                version("proj-current", "c-1", "2.0.0", sha1_of(current),
                        "2026-01-01T00:00:00Z", "current-2.0.0.jar")
            ],
        )
    )

    # 3. Exists upstream, but nothing for this Minecraft version.
    blocked = add("blocked.jar", id="blocked", version="1.0.0", name="Blocked Mod")
    upstream.add_project(
        FakeProject(
            id="proj-blocked",
            slug="blocked",
            title="Blocked Mod",
            versions=[
                version("proj-blocked", "b-1", "1.0.0", sha1_of(blocked),
                        "2026-01-01T00:00:00Z", "blocked-1.0.0.jar",
                        game_versions=("1.20.1",))
            ],
        )
    )

    # 4. CurseForge only: its hash is not on Modrinth, only its fingerprint is known.
    cf_only = add("cfonly.jar", id="cfonly", version="1.0.0", name="CurseForge Only Mod")
    from mod_update_checker.scanner import scan_jar

    fingerprint = scan_jar(cf_only).fingerprint
    upstream.add_cf_mod(
        FakeCfMod(
            id=CF_MOD_ID,
            slug="cfonly",
            name="CurseForge Only Mod",
            files=[
                FakeCfFile(
                    id=1,
                    mod_id=CF_MOD_ID,
                    file_name="cfonly-1.0.0.jar",
                    display_name="1.0.0 for Fabric " + GAME_VERSION,
                    fingerprint=fingerprint,
                    game_versions=(GAME_VERSION, "Fabric"),
                    file_date="2026-01-01T00:00:00Z",
                ),
                FakeCfFile(
                    id=2,
                    mod_id=CF_MOD_ID,
                    file_name="cfonly-1.1.0.jar",
                    display_name="1.1.0 for Fabric " + GAME_VERSION,
                    fingerprint=424242,
                    game_versions=(GAME_VERSION, "Fabric"),
                    file_date="2026-03-01T00:00:00Z",
                    download_url="https://edge.example/cfonly-1.1.0.jar",
                ),
            ],
        )
    )

    # 5. A library jar that is not a mod at all: the report must say so rather than guess.
    library = directory / "library.jar"
    write_jar(library, fabric=None)
    jars.append(library)

    return jars
