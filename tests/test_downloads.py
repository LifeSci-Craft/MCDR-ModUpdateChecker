"""Auto-download: the one part of this plugin that writes files from remote input.

Every test here is about a specific way that can go wrong, which is why the file is organised
by failure mode rather than by function:

* a file name from an API is untrusted input, and the destination must be inside a known folder
  no matter what it says;
* a jar that does not match its published hash must never be kept — a silently corrupted mod is
  worse than a missing one, because nothing downstream can tell;
* a download that fails must leave nothing behind, not even a partial file, because a half
  written jar looks exactly like a complete one;
* a file that is already there must not be replaced, and re-running must not fetch it again.

The transfer itself is exercised against the local fake CDN rather than a stub, so the bytes
really do arrive over HTTP and really are hashed.
"""

import hashlib
from pathlib import Path

import pytest

from mod_update_checker.downloads import (
    STATUS_ALREADY_PRESENT,
    STATUS_DOWNLOADED,
    STATUS_FAILED,
    STATUS_SKIPPED,
    DownloadOptions,
    Downloader,
    resolve_folder,
    safe_jar_name,
)
from mod_update_checker.upstream import HttpClient

from fake_upstream import FakeUpstream

MEGABYTE = 1024 * 1024


# --------------------------------------------------------------------------------------
# File names from an untrusted source
# --------------------------------------------------------------------------------------


def test_an_ordinary_name_is_left_alone():
    assert safe_jar_name("sodium-fabric-0.5.8.jar") == "sodium-fabric-0.5.8.jar"
    assert safe_jar_name("my.mod.v1.2.jar") == "my.mod.v1.2.jar"


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Traversal, in both separator styles. The directory part is dropped entirely rather
        # than sanitised, because ".._.._" is still a name nobody wants.
        ("../../etc/passwd", "passwd.jar"),
        ("../../../home/user/.ssh/authorized_keys", "authorized_keys.jar"),
        ("..\\..\\windows\\system32\\evil.jar", "evil.jar"),
        ("/etc/passwd", "passwd.jar"),
        ("C:\\Windows\\evil.jar", "evil.jar"),
        ("D:/x/y/z.jar", "z.jar"),
        # A name that is nothing but traversal has to fall back.
        ("..", "fallback.jar"),
        ("../..", "fallback.jar"),
        ("....//....//", "fallback.jar"),
        (".", "fallback.jar"),
        ("", "fallback.jar"),
        # Illegal characters are replaced, not dropped, so two different names cannot collapse
        # into one.
        ("a<b>c.jar", "a_b_c.jar"),
        ("a:b.jar", "a_b.jar"),
        ("a|b.jar", "a_b.jar"),
        ("a?b.jar", "a_b.jar"),
        ('a"b.jar', "a_b.jar"),
        ("a\x00b.jar", "a_b.jar"),
        ("a\x1fb.jar", "a_b.jar"),
        # Windows silently strips these, so the name we write and the name on disk would differ.
        ("name...jar", "name.jar"),
        ("name . .jar", "name.jar"),
        ("  spaced.jar  ", "spaced.jar"),
        # Windows device names, with and without an extension.
        ("CON.jar", "fallback.jar"),
        ("nul.jar", "fallback.jar"),
        ("COM1.jar", "fallback.jar"),
        ("LPT9.jar", "fallback.jar"),
        ("aux", "fallback.jar"),
        # A leading dot would hide the file.
        (".hidden.jar", "hidden.jar"),
        # Not a jar: the extension is imposed rather than trusted.
        ("mod.zip", "mod.zip.jar"),
        ("mod", "mod.jar"),
        ("mod.txt.exe", "mod.txt.exe.jar"),
        # A name that is only an extension has nothing usable in it.
        (".jar", "fallback.jar"),
    ],
)
def test_hostile_and_odd_names_are_made_safe(raw, expected):
    assert safe_jar_name(raw, "fallback") == expected


@pytest.mark.parametrize(
    "raw",
    [
        "../../../etc/passwd",
        "..\\..\\evil.jar",
        "/absolute/path/x.jar",
        "C:\\x\\y.jar",
        "a/b/c.jar",
        "\\\\server\\share\\x.jar",
        "\x00\x01\x02.jar",
        "CON.jar",
        "..",
        "",
        " ",
        ".",
        "..." ,
        "x" * 5000,
        "🦀🚀.jar",
        " leading-dash.jar",
        "--flag.jar",
        "nul",
        "trailing. ",
    ],
)
def test_a_returned_name_is_never_a_path(raw):
    """The invariant that makes every other guarantee hold.

    A name with a separator, or one that means "the parent directory", would let a remote API
    choose where the plugin writes. This is asserted over the whole hostile set rather than
    spot-checked, because it is the property the rest of the module relies on.
    """
    name = safe_jar_name(raw, "fallback")
    assert name
    assert "/" not in name
    assert "\\" not in name
    assert name not in (".", "..")
    assert not name.startswith(".")
    assert name.endswith(".jar")
    assert "\x00" not in name
    # A file name, not a path: resolving it against any directory stays in that directory.
    assert (Path("base") / name).parent == Path("base")


def test_a_long_name_is_truncated_but_still_a_jar():
    name = safe_jar_name("l" * 5000 + ".jar", "fallback")
    assert name.endswith(".jar")
    assert len(name) <= 124
    assert "/" not in name


def test_unicode_names_survive():
    """Mod file names do contain non-ASCII; mangling them would break the link to the jar."""
    assert safe_jar_name("模组-1.0.jar") == "模组-1.0.jar"
    assert safe_jar_name("mod+v1.jar") == "mod+v1.jar"


# --------------------------------------------------------------------------------------
# The destination folder cannot be a path
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "..",
        "../mods",
        "../../server/mods",
        "a/b",
        "a\\b",
        "/absolute",
        "/etc",
        "C:\\windows",
        "C:/windows",
        "",
        " ",
        "bad\x00name",
        "CON",
    ],
)
def test_a_folder_name_that_is_a_path_is_refused(tmp_path, name):
    """Making the folder a *name* is what makes writing into ``server/mods`` impossible.

    This is the constraint that keeps an admin from pointing the feature at the live server
    directory, so it is enforced rather than documented.
    """
    folder, reason = resolve_folder(tmp_path, name)
    assert folder is None
    assert reason, "a rejection must say why, or the log line is useless"


def test_a_plain_folder_name_is_accepted_and_stays_inside(tmp_path):
    folder, reason = resolve_folder(tmp_path, "downloads")
    assert reason == ""
    assert folder is not None
    assert folder == tmp_path / "downloads"
    assert folder.resolve().parent == tmp_path.resolve()


# --------------------------------------------------------------------------------------
# Which entries are worth fetching
# --------------------------------------------------------------------------------------


def _entry(**overrides):
    from mod_update_checker.report import UpdateEntry

    data = {
        "mod_id": "demo",
        "name": "Demo",
        "file_name": "demo.jar",
        "status": "update_available",
        "platform": "modrinth",
        "download_url": "https://cdn.example/demo-1.1.0.jar",
        "download_filename": "demo-1.1.0.jar",
        "download_sha1": "a" * 40,
        "download_size": 1024,
    }
    data.update(overrides)
    return UpdateEntry(**data)


def test_eligibility_and_the_reason_for_each_skip(tmp_path):
    options = DownloadOptions(folder=tmp_path, max_bytes=10 * MEGABYTE)
    downloader = Downloader(http=None, options=options)

    wanted, skipped = downloader.eligible(
        [
            _entry(),
            _entry(file_name="a.jar", status="up_to_date"),
            _entry(file_name="b.jar", download_url=""),
            _entry(file_name="c.jar", platform="curseforge"),
            _entry(file_name="d.jar", download_sha1=""),
            _entry(file_name="e.jar", download_size=99 * MEGABYTE),
        ]
    )

    assert [entry.file_name for entry in wanted] == ["demo.jar"]
    reasons = {outcome.file_name: outcome.detail for outcome in skipped}
    # Statuses that are not updates are not "skipped", they are simply not candidates — so they
    # produce no record at all, and the report is not padded with them.
    assert "a.jar" not in reasons
    assert reasons == {
        "b.jar": "no-download-url",
        "c.jar": "not-modrinth",
        "d.jar": "no-hash-to-verify",
        "e.jar": "declared-too-large",
    }


# --------------------------------------------------------------------------------------
# Real transfers, against the fake CDN
# --------------------------------------------------------------------------------------


@pytest.fixture
def cdn():
    upstream = FakeUpstream().start()
    yield upstream
    upstream.stop()


@pytest.fixture
def http(cdn):
    client = HttpClient(user_agent="test", timeout=10.0, retries=0)
    yield client
    client.close()


def _publish(cdn, name, blob):
    cdn.serve_file(name, blob)
    return {
        "download_url": cdn.file_url(name),
        "download_filename": name,
        "download_sha1": hashlib.sha1(blob).hexdigest(),
        "download_sha512": hashlib.sha512(blob).hexdigest(),
        "download_size": len(blob),
    }


def _run(tmp_path, http, entries, max_bytes=10 * MEGABYTE):
    options = DownloadOptions(folder=tmp_path / "downloads", max_bytes=max_bytes)
    return Downloader(http, options).run(entries)


def test_a_verified_file_is_written_byte_for_byte(tmp_path, cdn, http):
    blob = b"PK\x03\x04" + bytes(range(256)) * 40
    entry = _entry(**_publish(cdn, "demo-1.1.0.jar", blob))

    outcomes = _run(tmp_path, http, [entry])

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.status == STATUS_DOWNLOADED, outcome.detail
    written = Path(outcome.path)
    assert written.read_bytes() == blob
    assert outcome.bytes_written == len(blob)
    assert written.parent.name == "downloads"


def test_a_tampered_transfer_is_refused_and_leaves_nothing(tmp_path, cdn, http):
    """The most important test in this file.

    A jar whose bytes do not match the published hash must not survive on disk in any form: not
    as the target, not as a partial file. A corrupted mod that looks installed is the failure
    this whole design exists to prevent.
    """
    blob = b"PK\x03\x04" + b"genuine" * 100
    entry = _entry(**_publish(cdn, "demo-1.1.0.jar", blob))
    cdn.fail_downloads.add("demo-1.1.0.jar")

    outcomes = _run(tmp_path, http, [entry])

    assert outcomes[0].status == STATUS_FAILED
    assert "hash mismatch" in outcomes[0].detail
    folder = tmp_path / "downloads"
    assert list(folder.iterdir()) == [], "something was left behind"


def test_a_lying_content_length_is_caught_before_the_body_arrives(tmp_path, cdn, http):
    """The declared size is upstream data and cannot be trusted.

    Here the API says the file is tiny (so the entry passes the size gate) while the server
    declares a large ``Content-Length``. The transfer must be refused rather than written.
    """
    blob = b"PK\x03\x04" + b"x" * 200000
    entry = _entry(**_publish(cdn, "demo-1.1.0.jar", blob))
    entry.download_size = 10          # the API lied, or the check would have skipped it

    outcomes = _run(tmp_path, http, [entry], max_bytes=1000)

    assert outcomes[0].status == STATUS_FAILED
    assert "limit" in outcomes[0].detail
    assert list((tmp_path / "downloads").iterdir()) == []


def test_a_stream_without_a_declared_length_is_still_bounded(tmp_path, cdn, http):
    """Some proxies stream with no ``Content-Length`` at all.

    Then there is nothing to pre-check, and the limit has to be enforced on the bytes as they
    arrive. Either way the outcome must be the same: no file, no partial file.
    """
    blob = b"PK\x03\x04" + b"y" * 200000
    entry = _entry(**_publish(cdn, "demo-1.1.0.jar", blob))
    entry.download_size = 10
    cdn.no_content_length.add("demo-1.1.0.jar")

    outcomes = _run(tmp_path, http, [entry], max_bytes=1000)

    assert outcomes[0].status == STATUS_FAILED
    assert "limit" in outcomes[0].detail
    folder = tmp_path / "downloads"
    assert list(folder.iterdir()) == [], "a partial file was left behind"


def test_an_oversized_file_is_rejected_before_the_request(tmp_path, cdn, http):
    blob = b"PK\x03\x04" + b"y" * 5000
    entry = _entry(**_publish(cdn, "demo-1.1.0.jar", blob))

    outcomes = _run(tmp_path, http, [entry], max_bytes=100)

    assert outcomes[0].status == STATUS_SKIPPED
    assert outcomes[0].detail == "declared-too-large"
    # Never asked for it, so the CDN saw no traffic for it.
    assert not [path for path in cdn.request_paths() if "demo" in path]
    assert not (tmp_path / "downloads").exists()


def test_an_empty_body_is_a_failure_not_an_empty_file(tmp_path, cdn, http):
    entry = _entry(**_publish(cdn, "demo-1.1.0.jar", b""))

    outcomes = _run(tmp_path, http, [entry])

    assert outcomes[0].status == STATUS_FAILED
    assert "empty" in outcomes[0].detail
    assert list((tmp_path / "downloads").iterdir()) == []


def test_a_missing_file_upstream_is_a_failure(tmp_path, cdn, http):
    entry = _entry(
        download_url=cdn.file_url("never-published.jar"),
        download_filename="never-published.jar",
        download_sha1="b" * 40,
    )

    outcomes = _run(tmp_path, http, [entry])

    assert outcomes[0].status == STATUS_FAILED
    assert list((tmp_path / "downloads").iterdir()) == []


def test_a_second_run_does_not_fetch_again(tmp_path, cdn, http):
    """Idempotent, and it costs no network traffic — that is what makes this cheap to leave on.

    Without it, every check on a server whose admin has not got round to installing the jar yet
    would re-download it, on a schedule, forever.
    """
    blob = b"PK\x03\x04" + b"z" * 4000
    entry = _entry(**_publish(cdn, "demo-1.1.0.jar", blob))

    first = _run(tmp_path, http, [entry])
    assert first[0].status == STATUS_DOWNLOADED

    cdn.clear_requests()
    http.close()
    second = _run(tmp_path, http, [entry])

    assert second[0].status == STATUS_ALREADY_PRESENT
    assert second[0].path == first[0].path
    assert not cdn.request_paths(), "a file already on disk was fetched again"


def test_an_existing_different_build_is_kept_not_overwritten(tmp_path, cdn, http):
    """An older jar in the folder may be exactly what an admin needs to roll back.

    So a name collision with *different* content puts the new build alongside it under a name
    derived from its own hash — deterministic, so a re-run recognises it instead of piling up
    copies.
    """
    folder = tmp_path / "downloads"
    folder.mkdir(parents=True)
    older = b"PK\x03\x04" + b"older" * 50
    (folder / "demo.jar").write_bytes(older)
    older_hash = hashlib.sha1(older).hexdigest()

    new = b"PK\x03\x04" + b"newer" * 50
    entry = _entry(**_publish(cdn, "demo.jar", new))
    entry.download_sha1 = hashlib.sha1(new).hexdigest()

    outcomes = _run(tmp_path, http, [entry])

    assert outcomes[0].status == STATUS_DOWNLOADED, outcomes[0].detail
    assert (folder / "demo.jar").read_bytes() == older, "the older build was overwritten"
    assert hashlib.sha1((folder / "demo.jar").read_bytes()).hexdigest() == older_hash
    written = Path(outcomes[0].path)
    assert written.name == "demo.{}.jar".format(entry.download_sha1[:8])
    assert written.read_bytes() == new

    # And a re-run finds its own earlier work rather than creating a second copy.
    outcomes = _run(tmp_path, http, [entry])
    assert outcomes[0].status == STATUS_ALREADY_PRESENT
    assert len(list(folder.iterdir())) == 2


def test_a_traversal_attempt_in_the_file_name_cannot_escape_the_folder(tmp_path, cdn, http):
    """The end-to-end version of the name test: the bytes land inside the folder, full stop."""
    blob = b"PK\x03\x04" + b"payload" * 10
    published = _publish(cdn, "escape-1.0.0.jar", blob)
    entry = _entry(
        download_filename="../../../../tmp/escaped.jar",
        download_url=published["download_url"],
        download_sha1=published["download_sha1"],
        download_sha512=published["download_sha512"],
        download_size=published["download_size"],
    )

    outcomes = _run(tmp_path, http, [entry])

    assert outcomes[0].status == STATUS_DOWNLOADED, outcomes[0].detail
    written = Path(outcomes[0].path).resolve()
    folder = (tmp_path / "downloads").resolve()
    assert written.parent == folder, written
    assert written.name == "escaped.jar"
    assert not (tmp_path / "escaped.jar").exists()


def test_nothing_is_created_when_there_is_nothing_to_fetch(tmp_path, cdn, http):
    """Enabling the feature on a server with no updates must not leave an empty folder behind."""
    outcomes = _run(tmp_path, http, [_entry(status="up_to_date")])

    assert outcomes == []
    assert not (tmp_path / "downloads").exists()


def test_a_folder_that_cannot_be_created_is_reported_not_raised(tmp_path, cdn, http):
    """A blocked path is the admin's problem to fix, not a reason for the check to die."""
    blocker = tmp_path / "downloads"
    blocker.write_text("I am a file, not a folder", encoding="utf-8")
    blob = b"PK\x03\x04" + b"data" * 10
    entry = _entry(**_publish(cdn, "demo-1.1.0.jar", blob))

    outcomes = _run(tmp_path, http, [entry])

    assert outcomes[0].status == STATUS_FAILED
    assert "folder" in outcomes[0].detail
    assert blocker.read_text(encoding="utf-8").startswith("I am a file"), "the file was trodden on"


def test_outcomes_are_matched_back_to_the_entry_they_came_from(tmp_path, cdn, http):
    """The report attaches a note per entry, so the identifiers have to line up."""
    blob = b"PK\x03\x04" + b"one" * 10
    entry = _entry(**_publish(cdn, "demo-1.1.0.jar", blob))

    outcomes = _run(tmp_path, http, [entry])

    assert outcomes[0].file_name == "demo.jar"
    assert outcomes[0].mod_id == "demo"
    assert outcomes[0].ok is True
