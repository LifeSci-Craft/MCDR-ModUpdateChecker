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
    entry_key,
    resolve_folder,
    safe_jar_name,
)
from mod_update_checker.report import (
    STATUS_AWAITING_INSTALL,
    STATUS_UPDATE_AVAILABLE,
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


# --------------------------------------------------------------------------------------
# Retrying a failed download
# --------------------------------------------------------------------------------------


def _request_count(cdn, name):
    return len([path for path in cdn.request_paths() if path.endswith("/" + name)])


def test_a_flaky_link_recovers_within_the_retry_budget(tmp_path, cdn, http):
    """The point of the setting: a transfer that fails once is not a failed mod.

    The server corrupts the first two responses and then serves the file properly. With the
    default budget of three extra attempts this must succeed — that is the whole value of
    retrying, and asserting only "it did not crash" would not show it.
    """
    blob = b"PK\x03\x04" + b"genuine" * 200
    entry = _entry(**_publish(cdn, "demo-1.1.0.jar", blob))
    cdn.flaky_downloads["demo-1.1.0.jar"] = 2

    outcomes = _run(tmp_path, http, [entry])

    assert outcomes[0].status == STATUS_DOWNLOADED, outcomes[0].detail
    assert Path(outcomes[0].path).read_bytes() == blob
    assert _request_count(cdn, "demo-1.1.0.jar") == 3, "expected two failures then a success"


def test_the_retry_budget_is_respected(tmp_path, cdn, http):
    """Always corrupt: the attempts stop at the configured number, not at some other number."""
    blob = b"PK\x03\x04" + b"genuine" * 50
    entry = _entry(**_publish(cdn, "demo-1.1.0.jar", blob))
    cdn.fail_downloads.add("demo-1.1.0.jar")

    options = DownloadOptions(folder=tmp_path / "downloads", max_bytes=10 * MEGABYTE, retries=2)
    from mod_update_checker.downloads import Downloader

    outcomes = Downloader(http, options).run([entry])

    assert outcomes[0].status == STATUS_FAILED
    assert "hash mismatch" in outcomes[0].detail
    assert "after 3 attempts" in outcomes[0].detail, outcomes[0].detail
    assert _request_count(cdn, "demo-1.1.0.jar") == 3, "1 initial attempt + 2 retries"
    assert not list((tmp_path / "downloads").iterdir()), "a failed retry left something behind"


def test_zero_retries_means_exactly_one_attempt(tmp_path, cdn, http):
    """Switching retrying off has to be honoured, or it is not a setting."""
    blob = b"PK\x03\x04" + b"genuine" * 50
    entry = _entry(**_publish(cdn, "demo-1.1.0.jar", blob))
    cdn.fail_downloads.add("demo-1.1.0.jar")

    options = DownloadOptions(folder=tmp_path / "downloads", max_bytes=10 * MEGABYTE, retries=0)
    from mod_update_checker.downloads import Downloader

    outcomes = Downloader(http, options).run([entry])

    assert outcomes[0].status == STATUS_FAILED
    assert _request_count(cdn, "demo-1.1.0.jar") == 1


def test_a_missing_file_is_not_retried(tmp_path, cdn, http):
    """404 means the file is not there. Asking four times cannot change that, and repeating a
    request against a host that just said no is how a client gets itself blocked."""
    entry = _entry(
        download_url=cdn.file_url("never-published.jar"),
        download_filename="never-published.jar",
        download_sha1="b" * 40,
    )

    outcomes = _run(tmp_path, http, [entry])

    assert outcomes[0].status == STATUS_FAILED
    assert _request_count(cdn, "never-published.jar") == 1, "a 404 was retried"


def test_retry_state_does_not_leak_between_attempts(tmp_path, cdn, http):
    """Each attempt hashes only its own bytes.

    If the accumulators were shared, a successful retry would be hashing the failed attempt's
    bytes too and would fail verification forever — a retry that can never succeed. The flaky
    test above would catch it, but this pins the specific number of attempts.
    """
    blob = b"PK\x03\x04" + b"only-the-last-attempt-counts" * 30
    entry = _entry(**_publish(cdn, "demo-1.1.0.jar", blob))
    cdn.flaky_downloads["demo-1.1.0.jar"] = 1

    outcomes = _run(tmp_path, http, [entry])

    assert outcomes[0].status == STATUS_DOWNLOADED, outcomes[0].detail
    assert outcomes[0].bytes_written == len(blob), "the byte count carried over from the failure"
    assert Path(outcomes[0].path).read_bytes() == blob


def test_a_failed_attempt_leaves_no_part_file_for_the_retry(tmp_path, cdn, http):
    """The partial file from the failed attempt must be gone before the next one starts.

    Otherwise the retry would append to it and the verified length would be wrong.
    """
    blob = b"PK\x03\x04" + b"x" * 3000
    entry = _entry(**_publish(cdn, "demo-1.1.0.jar", blob))
    cdn.flaky_downloads["demo-1.1.0.jar"] = 1

    outcomes = _run(tmp_path, http, [entry])

    assert outcomes[0].status == STATUS_DOWNLOADED
    assert outcomes[0].bytes_written == len(blob)
    folder = tmp_path / "downloads"
    assert [p.name for p in folder.iterdir()] == ["demo-1.1.0.jar"]


def test_the_default_budget_comes_from_the_options(tmp_path, cdn, http):
    """Pinned so the shipped default cannot drift without a test noticing."""
    assert DownloadOptions(folder=tmp_path).retries == 3


# --------------------------------------------------------------------------------------
# The ledger: which downloaded file belongs to which mod
# --------------------------------------------------------------------------------------


def _ledger(tmp_path):
    from mod_update_checker.downloads import DownloadLedger

    return DownloadLedger(tmp_path / "download-manifest.json")


def test_the_ledger_round_trips(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record("sodium", "sodium-0.6.0.jar", "A" * 40, "0.6.0", "2026-01-01T00:00:00+00:00")
    ledger.save()

    reloaded = _ledger(tmp_path)
    record = reloaded.get("sodium")
    assert record["file"] == "sodium-0.6.0.jar"
    assert record["version"] == "0.6.0"
    # The hash is normalised, or a hash comparison would fail on case alone.
    assert record["sha1"] == "a" * 40


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        "[]",
        '{"version": 99, "mods": {}}',            # a format this build does not understand
        '{"version": 1, "mods": []}',              # wrong shape for the records
        '{"version": 1, "mods": {"a": "nope"}}',   # a record that is not an object
        '{"version": 1, "mods": {"a": {"file": 5}}}',
    ],
)
def test_a_malformed_ledger_is_ignored_rather_than_fatal(tmp_path, content):
    """Written by this plugin, read by an older version, edited by hand — all can happen.

    A plugin must not fail a check over its own bookkeeping. Losing it costs one extra
    download; crashing costs the admin their update report.
    """
    path = tmp_path / "download-manifest.json"
    path.write_text(content, encoding="utf-8")

    from mod_update_checker.downloads import DownloadLedger

    ledger = DownloadLedger(path)
    assert ledger.get("anything") is None
    ledger.record("sodium", "a.jar", "b" * 40, "1.0", "now")
    ledger.save()  # and it recovers by overwriting


def test_the_ledger_is_kept_outside_the_download_folder(tmp_path):
    """So that the folder stays nothing but jars, and is checked as such."""
    from mod_update_checker.downloads import DownloadLedger

    ledger = DownloadLedger(tmp_path / "download-manifest.json")
    ledger.record("x", "x.jar", "c" * 40, "1", "now")
    ledger.save()

    assert (tmp_path / "download-manifest.json").is_file()
    assert not (tmp_path / "downloads").exists()


# --------------------------------------------------------------------------------------
# Authorising one download for the next install
#
# ``!!muc install <编号>`` marks a single record. Two things have to hold, and neither is
# obvious: an unrelated check must not drop the mark, and a mark must not survive onto bytes
# nobody agreed to install.
# --------------------------------------------------------------------------------------


def test_an_approval_survives_a_check_that_records_the_same_file(tmp_path):
    """Every check re-records what is on disk — including the one that re-observes this file.

    Without this, ``!!muc install 3`` would be silently undone by the next automatic check,
    and the admin would find out at the next stop, when nothing happened.
    """
    ledger = _ledger(tmp_path)
    ledger.record("sodium", "sodium-1.1.0.jar", "a" * 40, "1.1.0", "then",
                  installed_file="sodium.jar", name="Sodium")
    assert ledger.approve("sodium") is True

    ledger.record("sodium", "sodium-1.1.0.jar", "a" * 40, "1.1.0", "now",
                  installed_file="sodium.jar", name="Sodium")

    assert ledger.approved_keys() == ["sodium"]


def test_an_approval_does_not_follow_the_file_it_was_given_for(tmp_path):
    """A different build is bytes nobody agreed to, so the approval is dropped."""
    ledger = _ledger(tmp_path)
    ledger.record("sodium", "sodium-1.1.0.jar", "a" * 40, "1.1.0", "then")
    ledger.approve("sodium")

    ledger.record("sodium", "sodium-1.2.0.jar", "b" * 40, "1.2.0", "now")

    assert ledger.approved_keys() == []
    assert "approved" not in ledger.get("sodium"), "the mark was carried onto other bytes"


def test_approving_a_record_that_does_not_exist_is_refused(tmp_path):
    """Said rather than stored: a mark on nothing would install nothing, silently."""
    ledger = _ledger(tmp_path)
    assert ledger.approve("ghost") is False
    assert ledger.approved_keys() == []


def test_an_approval_is_written_to_disk_and_read_back(tmp_path):
    """The install runs from the stop event, possibly after a reload, so it must persist."""
    ledger = _ledger(tmp_path)
    ledger.record("sodium", "sodium-1.1.0.jar", "a" * 40, "1.1.0", "now")
    ledger.approve("sodium")
    ledger.save()

    assert _ledger(tmp_path).approved_keys() == ["sodium"]


# --------------------------------------------------------------------------------------
# Replacing a superseded download
# --------------------------------------------------------------------------------------


def _seed(tmp_path, ledger, entry, name, blob, version="1.0.0"):
    folder = tmp_path / "downloads"
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(blob)
    ledger.record(entry_key(entry), name, hashlib.sha1(blob).hexdigest(), version, "then")
    ledger.save()


def test_a_superseded_download_is_replaced_rather_than_accumulating(tmp_path, cdn, http):
    """The behaviour that keeps the folder from filling with every version ever fetched.

    Names cannot do this job — the new build has a different one — which is exactly why the
    ledger records who a file belongs to.
    """
    from mod_update_checker.downloads import DownloadLedger

    ledger = DownloadLedger(tmp_path / "download-manifest.json")
    older = b"PK\x03\x04" + b"version one" * 20
    new = b"PK\x03\x04" + b"version two" * 20
    entry = _entry(**_publish(cdn, "demo-1.2.0.jar", new))
    _seed(tmp_path, ledger, entry, "demo-1.0.0.jar", older, version="1.0.0")

    outcomes = _run_with_ledger(tmp_path, http, [entry], ledger)

    assert outcomes[0].status == STATUS_DOWNLOADED, outcomes[0].detail
    folder = tmp_path / "downloads"
    assert not (folder / "demo-1.0.0.jar").exists(), "the superseded build was left behind"
    assert (folder / "demo-1.2.0.jar").read_bytes() == new
    assert len(list(folder.iterdir())) == 1, "the folder accumulated a copy"
    # And the ledger now points at the new file, so the next run recognises it.
    assert DownloadLedger(tmp_path / "download-manifest.json").file_of(entry_key(entry)) == \
        "demo-1.2.0.jar"


def test_a_download_that_is_already_current_is_not_re_downloaded(tmp_path, cdn, http):
    """Recorded hash matches the wanted one: nothing to do, and no network traffic."""
    from mod_update_checker.downloads import DownloadLedger

    ledger = DownloadLedger(tmp_path / "download-manifest.json")
    blob = b"PK\x03\x04" + b"current" * 20
    entry = _entry(**_publish(cdn, "demo-1.1.0.jar", blob))
    _seed(tmp_path, ledger, entry, "demo-1.1.0.jar", blob, version="1.1.0")

    cdn.clear_requests()
    outcomes = _run_with_ledger(tmp_path, http, [entry], ledger)

    assert outcomes[0].status == STATUS_ALREADY_PRESENT
    assert not cdn.request_paths(), "an up-to-date download was fetched again"


def test_a_file_the_plugin_did_not_write_is_never_deleted(tmp_path, cdn, http):
    """Deleting a file we are not certain we created would be the worst bug here.

    The ledger records bytes; if the file on disk is not those bytes, it is someone else's and
    is left alone. The new build then lands under a hash-suffixed name rather than clobbering it.
    """
    from mod_update_checker.downloads import DownloadLedger

    ledger = DownloadLedger(tmp_path / "download-manifest.json")
    recorded = b"PK\x03\x04" + b"what we wrote" * 20
    replaced_by_admin = b"PK\x03\x04" + b"something else entirely" * 20
    entry = _entry(**_publish(cdn, "demo-1.2.0.jar", b"PK\x03\x04 new" * 20))
    _seed(tmp_path, ledger, entry, "demo-1.0.0.jar", recorded, version="1.0.0")
    # The admin overwrote our download with their own file of the same name.
    (tmp_path / "downloads" / "demo-1.0.0.jar").write_bytes(replaced_by_admin)

    outcomes = _run_with_ledger(tmp_path, http, [entry], ledger)

    assert outcomes[0].status == STATUS_DOWNLOADED, outcomes[0].detail
    kept = (tmp_path / "downloads" / "demo-1.0.0.jar").read_bytes()
    assert kept == replaced_by_admin, "a file we did not write was deleted"


def test_a_record_for_a_missing_file_is_dropped_silently(tmp_path, cdn, http):
    """The admin installed it, so the record is simply stale — not an error."""
    from mod_update_checker.downloads import DownloadLedger

    ledger = DownloadLedger(tmp_path / "download-manifest.json")
    blob = b"PK\x03\x04" + b"gone" * 20
    entry = _entry(**_publish(cdn, "demo-1.1.0.jar", blob))
    _seed(tmp_path, ledger, entry, "demo-1.0.0.jar", b"older", version="1.0.0")
    (tmp_path / "downloads" / "demo-1.0.0.jar").unlink()

    outcomes = _run_with_ledger(tmp_path, http, [entry], ledger)

    assert outcomes[0].status == STATUS_DOWNLOADED
    assert not (tmp_path / "downloads" / "demo-1.0.0.jar").exists()


def _run_with_ledger(tmp_path, http, entries, ledger, max_bytes=10 * MEGABYTE):
    from mod_update_checker.downloads import Downloader

    options = DownloadOptions(folder=tmp_path / "downloads", max_bytes=max_bytes)
    return Downloader(http, options, ledger=ledger).run(entries)


# --------------------------------------------------------------------------------------
# Classifying what is already fetched
# --------------------------------------------------------------------------------------


def test_classify_moves_a_fetched_build_out_of_the_update_list(tmp_path):
    """A mod that has been fetched is no longer "an update to download"."""
    blob = b"PK\x03\x04" + b"fetched" * 10
    folder = tmp_path / "downloads"
    folder.mkdir()
    entry = _entry(**_publish(FakeUpstream(), "demo-1.1.0.jar", blob))
    (folder / entry.download_filename).write_bytes(blob)

    from mod_update_checker.downloads import classify_downloaded

    moved = classify_downloaded([entry], folder, ledger=None)

    assert moved == [entry]
    assert entry.status == STATUS_AWAITING_INSTALL


def test_classify_leaves_content_that_does_not_match(tmp_path):
    """A truncated or hand-replaced jar is not something to tell someone to install."""
    folder = tmp_path / "downloads"
    folder.mkdir()
    entry = _entry(**_publish(FakeUpstream(), "demo-1.1.0.jar", b"PK\x03\x04 good" * 10))
    (folder / entry.download_filename).write_bytes(b"PK\x03\x04 truncated")

    from mod_update_checker.downloads import classify_downloaded

    assert classify_downloaded([entry], folder, ledger=None) == []
    assert entry.status == STATUS_UPDATE_AVAILABLE


def test_classify_ignores_entries_that_are_not_updates(tmp_path):
    folder = tmp_path / "downloads"
    folder.mkdir()
    blob = b"PK\x03\x04" + b"x" * 30
    entry = _entry(**_publish(FakeUpstream(), "demo-1.1.0.jar", blob))
    entry.status = "up_to_date"
    (folder / entry.download_filename).write_bytes(blob)

    from mod_update_checker.downloads import classify_downloaded

    assert classify_downloaded([entry], folder, ledger=None) == []
    assert entry.status == "up_to_date"


def test_classify_needs_a_hash_to_work_with(tmp_path):
    """Without a hash nothing can be verified, so nothing may be called ready."""
    folder = tmp_path / "downloads"
    folder.mkdir()
    entry = _entry(download_sha1="")
    (folder / entry.download_filename).write_bytes(b"whatever")

    from mod_update_checker.downloads import classify_downloaded

    assert classify_downloaded([entry], folder, ledger=None) == []
    assert entry.status == STATUS_UPDATE_AVAILABLE


def test_classify_handles_a_folder_that_does_not_exist(tmp_path):
    from mod_update_checker.downloads import classify_downloaded

    entry = _entry()
    assert classify_downloaded([entry], tmp_path / "nope", ledger=None) == []
    assert entry.status == STATUS_UPDATE_AVAILABLE
