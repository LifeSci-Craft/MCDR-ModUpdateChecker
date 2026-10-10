"""The ``.old`` backups: which files count, how old they are, and what may be deleted.

Everything here is about the one predicate that stands between an admin's ``!!muc confirm`` and
their ``mods/`` folder. The rest of the plugin can be wrong in ways that are visible; this
module being wrong deletes somebody's rollback — or, worse, something that was never a backup
at all. So the name rule, the re-check at deletion time, and the ageing are each pinned
separately.
"""

import os
import time

from mod_update_checker.cleanup import (
    BACKUP_SUFFIX,
    Backup,
    age_in_days,
    expired,
    is_backup_name,
    list_backups,
    remove_backups,
    restored_name,
    total_bytes,
)

DAY = 86400


def _write(folder, name, size=1024, age_days=0):
    path = folder / name
    path.write_bytes(b"x" * size)
    if age_days:
        stamp = time.time() - age_days * DAY
        os.utime(str(path), (stamp, stamp))
    return path


# -- what counts as a backup -------------------------------------------------------------


def test_only_the_names_this_plugin_creates_are_backups():
    """The rule the whole deletion feature rests on.

    The ``.jar`` is not decoration: ``config.yml.old`` and ``notes.old`` are somebody else's
    files, and "it ends in .old" is not a reason to offer them for deletion.
    """
    assert is_backup_name("sodium.jar.old")
    assert is_backup_name("[锂-性能优化]Lithium.jar.old")
    assert is_backup_name("sodium.jar.old.2")
    assert is_backup_name("SODIUM.JAR.OLD")

    assert not is_backup_name("sodium.jar")
    assert not is_backup_name("notes.old")
    assert not is_backup_name("config.yml.old")
    assert not is_backup_name("sodium.jar.old.bak")
    assert not is_backup_name("old")
    assert not is_backup_name("")
    assert not is_backup_name(None)


def test_the_suffix_matches_the_one_the_installer_writes():
    """Two modules, one spelling. Asserted rather than imported.

    ``installer`` keeps its own copy on purpose — it must be able to write a backup without
    this module existing — so the two are compared here instead of being made to depend on
    each other. A drift would mean backups the plugin creates and the plugin refuses to clean.
    """
    from mod_update_checker import installer

    assert installer._BACKUP_SUFFIX == BACKUP_SUFFIX


def test_the_original_name_is_recovered_from_the_backup():
    assert restored_name("sodium.jar.old") == "sodium.jar"
    assert restored_name("sodium.jar.old.3") == "sodium.jar"
    assert restored_name("plain.jar") == "plain.jar"


# -- listing and ageing ------------------------------------------------------------------


def test_the_listing_covers_only_backups(tmp_path):
    _write(tmp_path, "sodium.jar.old", size=100)
    _write(tmp_path, "lithium.jar.old.2", size=200)
    _write(tmp_path, "sodium.jar", size=300)
    _write(tmp_path, "notes.old", size=400)
    (tmp_path / "adir.jar.old").mkdir()

    found = list_backups(tmp_path)

    # Same age, so the tie-break is the name — and the two files that are not backups, the
    # jar itself and somebody else's ``notes.old``, are not in the list at all.
    assert [item.file_name for item in found] == ["lithium.jar.old.2", "sodium.jar.old"]
    assert [item.size_bytes for item in found] == [200, 100]
    assert total_bytes(found) == 300


def test_a_folder_that_is_not_there_is_not_an_error(tmp_path):
    """This runs inside a check, and a check has to finish even on a broken server."""
    assert list_backups(tmp_path / "nope") == []


def test_age_is_whole_days_and_never_negative():
    now = time.time()
    assert age_in_days(now - 10 * DAY, now) == 10
    assert age_in_days(now - (10 * DAY + 3600), now) == 10
    assert age_in_days(now, now) == 0
    # A clock that moved, or a copy that brought its own timestamp with it.
    assert age_in_days(now + 5 * DAY, now) == 0


def test_the_oldest_backup_is_listed_first(tmp_path):
    """The reader's question is "which of these has been here longest"."""
    _write(tmp_path, "young.jar.old", age_days=1)
    _write(tmp_path, "old.jar.old", age_days=99)
    _write(tmp_path, "middle.jar.old", age_days=30)

    assert [item.file_name for item in list_backups(tmp_path)] == [
        "old.jar.old", "middle.jar.old", "young.jar.old"
    ]
    assert [item.age_days for item in list_backups(tmp_path)] == [99, 30, 1]


def test_age_is_read_from_the_timestamp_the_installer_stamps(tmp_path):
    """A backup's age is when it *became* a backup, not when the jar it holds was built.

    This is the whole reason the installer touches the file after renaming it: a rename keeps
    the original timestamp, so an untouched ``.old`` would carry the date of a jar that had
    been sitting in the folder for a year — and every backup on the server would read as long
    expired on the day this feature shipped.
    """
    path = _write(tmp_path, "sodium.jar.old", size=10)
    stamp = time.time() - 400 * DAY
    os.utime(str(path), (stamp, stamp))
    assert list_backups(tmp_path)[0].age_days > 300

    os.utime(str(path), None)          # what the installer does
    assert list_backups(tmp_path)[0].age_days == 0


# -- what is offered for removal ---------------------------------------------------------


def test_the_threshold_selects_the_old_ones():
    items = [
        Backup("a.jar.old", 1, 0),
        Backup("b.jar.old", 1, 29),
        Backup("c.jar.old", 1, 30),
        Backup("d.jar.old", 1, 90),
    ]

    assert [item.file_name for item in expired(items, 30)] == ["c.jar.old", "d.jar.old"]
    assert [item.file_name for item in expired(items, 1)] == [
        "b.jar.old", "c.jar.old", "d.jar.old"
    ]


def test_a_threshold_of_zero_means_everything_rather_than_nothing():
    """``0`` is "I never want to keep backups", not "switch it off".

    The switch that turns the feature off is ``cleanup.enabled``. Reading zero as "off" as well
    would give two settings the same meaning, and the one that loses is the one an admin wrote
    down and then looked for.
    """
    items = [Backup("fresh.jar.old", 1, 0)]

    assert expired(items, 0) == items
    assert expired(items, -5) == items


# -- removing ----------------------------------------------------------------------------


def test_removing_takes_exactly_the_named_files(tmp_path):
    keep = _write(tmp_path, "keep.jar.old", size=11)
    drop = _write(tmp_path, "drop.jar.old", size=22)

    results = remove_backups(tmp_path, [Backup("drop.jar.old", 22, 40)])

    assert [item.file_name for item in results] == ["drop.jar.old"]
    assert results[0].removed
    assert not drop.exists()
    assert keep.exists(), "a file nobody named was deleted"


def test_removing_re_checks_the_name_at_the_moment_of_deletion(tmp_path):
    """The list is a snapshot; the check has to be the one that runs at the deletion.

    Between drawing up a plan and confirming it, an admin can rename a file. Whatever the name
    is at the moment of the ``unlink`` is what this predicate has to approve, or a plan drawn
    up honestly becomes a way to delete something that was never a backup.
    """
    victim = _write(tmp_path, "config.yml", size=10)

    results = remove_backups(tmp_path, [Backup("config.yml", 10, 99)])

    assert victim.exists(), "a file that is not a backup was deleted"
    assert [item.detail for item in results] == ["not-a-backup"]


def test_a_file_that_vanished_is_reported_rather_than_raising(tmp_path):
    results = remove_backups(tmp_path, [Backup("gone.jar.old", 1, 99)])

    assert [(item.removed, item.detail) for item in results] == [(False, "already-gone")]


def test_the_whole_folder_is_never_touched(tmp_path):
    """No path is ever built from a name, so a name cannot escape the folder."""
    outside = tmp_path.parent / "outside.jar.old"
    outside.write_bytes(b"x")
    try:
        results = remove_backups(tmp_path / "inner", [Backup("../outside.jar.old", 1, 99)])
        assert outside.exists()
        assert results[0].removed is False
    finally:
        outside.unlink()
