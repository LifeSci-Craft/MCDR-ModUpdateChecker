"""The mods folder and upstream catalogue the MCDR matrix runs against.

Kept apart from ``mcdr_matrix.py`` so the tool stays readable, and importable on its own so
the pytest end-to-end test can plant exactly the same scenario.

The scenario is small but complete: mods with a pending update, one already current, one whose
project publishes nothing for this Minecraft version, one whose transfer fails verification,
one whose transfer is flaky enough to need the retry budget, one the admin asked to ignore, and
one jar that is not a mod at all. Every outcome the console can print should occur at least
once, otherwise a regression that stops printing one of them would still pass.
"""

import hashlib
from pathlib import Path
from typing import List

from fake_upstream import (
    FakeFile,
    FakeProject,
    FakeUpstream,
    FakeVersion,
)
from support import fabric_metadata, write_jar

#: Fixed so the matrix's expectations are stable.
GAME_VERSION = "26.3"


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

    def published(file_name: str, **metadata) -> dict:
        """Build the *new* build's bytes, publish them, and describe the file.

        The auto-download feature is only worth testing against bytes that really hash to what
        the API declared — anything else would prove that the downloader accepts files it
        should not. So the jar is built for real and its true digests are declared.
        """
        path = write_jar(directory / "published" / file_name,
                         fabric=fabric_metadata(**metadata))
        blob = path.read_bytes()
        upstream.serve_file(file_name, blob)
        return {
            "sha1": hashlib.sha1(blob).hexdigest(),
            "sha512": hashlib.sha512(blob).hexdigest(),
            "size": len(blob),
            "filename": file_name,
            "url": upstream.file_url(file_name),
        }

    def version(project_id, version_id, number, sha1, date, filename,
                game_versions=(GAME_VERSION,), **file_kwargs) -> FakeVersion:
        if "url" not in file_kwargs:
            # Point at the download route whenever those bytes have actually been published,
            # so the auto-download path is exercised against a real transfer instead of a
            # placeholder host that would only ever fail.
            file_kwargs["url"] = (
                upstream.file_url(filename)
                if filename in upstream.cdn_files
                else "https://cdn.example/" + filename
            )
        return FakeVersion(
            id=version_id,
            project_id=project_id,
            version_number=number,
            game_versions=game_versions,
            date_published=date,
            files=[FakeFile(sha1=sha1, filename=filename, **file_kwargs)],
        )

    # 1. Has a newer build waiting — and it is really downloadable, so the auto-download path
    #    has something genuine to verify rather than a placeholder hash.
    outdated = add("outdated.jar", id="outdated", version="1.0.0", name="Outdated Mod")
    new_outdated = published("outdated-1.1.0.jar", id="outdated", version="1.1.0",
                            name="Outdated Mod")
    upstream.add_project(
        FakeProject(
            id="proj-outdated",
            slug="outdated",
            title="Outdated Mod",
            versions=[
                version("proj-outdated", "o-1", "1.0.0", sha1_of(outdated),
                        "2026-01-01T00:00:00Z", "outdated-1.0.0.jar"),
                version("proj-outdated", "o-2", "1.1.0", new_outdated["sha1"],
                        "2026-02-01T00:00:00Z", new_outdated["filename"],
                        sha512=new_outdated["sha512"], size=new_outdated["size"]),
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

    # 4. Excluded from checking by config. It has a newer build upstream, so if the ignore
    #    were not honoured it would show up as an update — and, with downloading on, it would
    #    also be fetched. Both are asserted.
    ignored = add("ignored.jar", id="ignored", version="1.0.0", name="Ignored Mod")
    ignored_new = published("ignored-1.1.0.jar", id="ignored", version="1.1.0", name="Ignored Mod")
    upstream.unwanted_downloads.add(ignored_new["filename"])
    upstream.add_project(
        FakeProject(
            id="proj-ignored",
            slug="ignored",
            title="Ignored Mod",
            versions=[
                version("proj-ignored", "i-1", "1.0.0", sha1_of(ignored),
                        "2026-01-01T00:00:00Z", "ignored-1.0.0.jar"),
                version("proj-ignored", "i-2", "1.1.0", ignored_new["sha1"],
                        "2026-02-01T00:00:00Z", ignored_new["filename"],
                        sha512=ignored_new["sha512"], size=ignored_new["size"]),
            ],
        )
    )

    # 5. A library jar that is not a mod at all: the report must say so rather than guess.
    library = directory / "library.jar"
    write_jar(library, fabric=None)
    jars.append(library)

    # 6. Its download is served with the wrong bytes, so the declared hash does not match what
    #    arrives. The downloader must refuse it and leave nothing behind — a jar that was
    #    silently written despite failing verification is the one outcome worse than no file.
    tampered = add("tampered.jar", id="tampered", version="1.0.0", name="Tampered Mod")
    clean = published("tampered-1.1.0.jar", id="tampered", version="1.1.0", name="Tampered Mod")
    upstream.fail_downloads.add(clean["filename"])
    upstream.add_project(
        FakeProject(
            id="proj-tampered",
            slug="tampered",
            title="Tampered Mod",
            versions=[
                version("proj-tampered", "t-1", "1.0.0", sha1_of(tampered),
                        "2026-01-01T00:00:00Z", "tampered-1.0.0.jar"),
                version("proj-tampered", "t-2", "1.1.0", clean["sha1"],
                        "2026-02-01T00:00:00Z", clean["filename"],
                        sha512=clean["sha512"], size=clean["size"]),
            ],
        )
    )

    # 7. A transfer that fails twice and then works. This is the case the retry budget exists
    #    for, and the only way to show it recovering is to have a real transfer fail for real.
    #    Succeeds on the third attempt, which is inside the shipped budget of 1 + 3.
    flaky = add("flaky.jar", id="flaky", version="1.0.0", name="Flaky Mod")
    flaky_build = published("flaky-1.1.0.jar", id="flaky", version="1.1.0", name="Flaky Mod")
    upstream.flaky_downloads[flaky_build["filename"]] = 2
    upstream.add_project(
        FakeProject(
            id="proj-flaky",
            slug="flaky",
            title="Flaky Mod",
            versions=[
                version("proj-flaky", "f-1", "1.0.0", sha1_of(flaky),
                        "2026-01-01T00:00:00Z", "flaky-1.0.0.jar"),
                version("proj-flaky", "f-2", "1.1.0", flaky_build["sha1"],
                        "2026-02-01T00:00:00Z", flaky_build["filename"],
                        sha512=flaky_build["sha512"], size=flaky_build["size"]),
            ],
        )
    )

    return jars
