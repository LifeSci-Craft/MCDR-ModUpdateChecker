"""The admin's mapping file, and the validation that keeps it inside the data folder.

Two things are worth testing here and they are different in kind:

* **the parsing** — the file is hand-written, so every way of getting it wrong has to end in
  "ignore it and carry on" rather than in a failed check. A typo in someone's JSON must not be
  able to stop the plugin from reporting that Sodium has an update;
* **the path check** — ``sources.manual_map`` is a file name, not a path, on purpose. It is
  the same rule as ``download.folder_name`` and it is asserted from the outside, because the
  failure it prevents (reading somewhere the admin did not intend) is silent.
"""

import json

import pytest

from mod_update_checker.downloads import is_safe_component, resolve_folder
from mod_update_checker.projectmap import ProjectMap, resolve_map_file

SHA = "a" * 40


def write_map(tmp_path, payload, name="project-map.json"):
    path = tmp_path / name
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )
    return path


def loaded(tmp_path, payload):
    return ProjectMap(write_map(tmp_path, payload))


# --------------------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------------------


def test_both_sections_are_read(tmp_path):
    mapping = loaded(
        tmp_path,
        {
            "version": 1,
            "by_sha1": {SHA: "sodium"},
            "by_mod_id": {"mycustommod": "lithium"},
        },
    )

    assert mapping.error == ""
    assert mapping.hashes == 1
    assert mapping.mod_ids == 1
    assert len(mapping) == 2
    assert mapping.lookup(sha1=SHA) == "sodium"
    assert mapping.lookup(mod_id="mycustommod") == "lithium"


def test_the_hash_wins_when_the_file_is_listed_both_ways(tmp_path):
    """A hash names one file; a mod id names everything that ever carried it.

    Writing both is the *point*: the admin is saying "this build is the fork's, but anything
    else with this id is the upstream project". Reading the mod id first would throw that away.
    """
    mapping = loaded(
        tmp_path,
        {"by_sha1": {SHA: "the-fork"}, "by_mod_id": {"shared": "the-upstream"}},
    )

    assert mapping.lookup(sha1=SHA, mod_id="shared") == "the-fork"
    assert mapping.lookup(sha1="b" * 40, mod_id="shared") == "the-upstream"


def test_keys_are_matched_case_insensitively(tmp_path):
    """A hand-written file gets the hex and the id spelled however the author typed them."""
    mapping = loaded(
        tmp_path,
        {"by_sha1": {SHA.upper(): "sodium"}, "by_mod_id": {"MyCustomMod": "lithium"}},
    )

    assert mapping.lookup(sha1=SHA) == "sodium"
    assert mapping.lookup(mod_id="mycustommod") == "lithium"


def test_a_missing_file_is_not_an_error(tmp_path):
    """Opting out is not a misconfiguration, and it must not log a warning every check."""
    mapping = ProjectMap(tmp_path / "nothing-here.json")

    assert mapping.error == ""
    assert mapping.loaded is False
    assert mapping.lookup(sha1=SHA, mod_id="anything") == ""


def test_no_path_at_all_means_disabled(tmp_path):
    mapping = ProjectMap(None)

    assert mapping.enabled is False
    assert mapping.loaded is False
    assert mapping.lookup(sha1=SHA) == ""


@pytest.mark.parametrize(
    "payload,needle",
    [
        ("{ this is not json", "JSONDecodeError"),
        ("[1, 2, 3]", "JSON object"),
        ('{"version": 99, "by_sha1": {}}', "version"),
        ('{"by_sha1": []}', "by_sha1"),
        ('{"by_mod_id": "nope"}', "by_mod_id"),
    ],
)
def test_an_unusable_file_is_reported_and_ignored(tmp_path, payload, needle):
    """Every failure ends in the same place: no mappings, a reason, no exception."""
    mapping = loaded(tmp_path, payload)

    assert needle in mapping.error, mapping.error
    assert mapping.loaded is False
    assert mapping.lookup(sha1=SHA, mod_id="anything") == ""


def test_a_version_key_is_optional(tmp_path):
    """The file is written by a human, so the clearest possible form has to work."""
    mapping = loaded(tmp_path, {"by_sha1": {SHA: "sodium"}})

    assert mapping.error == ""
    assert mapping.lookup(sha1=SHA) == "sodium"


def test_unusable_rows_are_counted_rather_than_silently_dropped(tmp_path):
    """A dropped row is a mapping the admin believes is in force and is not.

    Five rows here, one of them good: a short hash, a hash with a non-hex character, a
    non-string project, and an empty mod id. The count is what the warning reports, so it has
    to be the number of *rejected* rows, not the number of rows.
    """
    mapping = loaded(
        tmp_path,
        {
            "by_sha1": {
                SHA: "sodium",
                "abc": "too-short",
                "z" * 40: "not-hex",
                "b" * 40: 42,
            },
            "by_mod_id": {"": "no-key"},
        },
    )

    assert mapping.hashes == 1
    assert mapping.mod_ids == 0
    assert mapping.rejected == 4


def test_a_project_value_is_not_validated_beyond_being_a_string(tmp_path):
    """A slug and a project id are both legal, and guessing which was meant is not our job.

    A reference that does not exist is reported when it is *used*, where the admin can do
    something about it — see the checker's ``note.manual_map_unknown_project``.
    """
    mapping = loaded(tmp_path, {"by_sha1": {SHA: "  some-slug  "}})

    assert mapping.lookup(sha1=SHA) == "some-slug"


# --------------------------------------------------------------------------------------
# The path check
# --------------------------------------------------------------------------------------


def test_a_plain_file_name_resolves_inside_the_data_folder(tmp_path):
    path, reason = resolve_map_file(tmp_path, "project-map.json")

    assert reason == ""
    assert path == tmp_path / "project-map.json"


def test_an_empty_name_means_off_rather_than_invalid(tmp_path):
    path, reason = resolve_map_file(tmp_path, "   ")

    assert path is None
    assert reason == "disabled"


@pytest.mark.parametrize(
    "value",
    [
        "sub/dir/map.json",
        "sub\\dir\\map.json",
        "..",
        "..\\escape.json",
        "../escape.json",
        "C:map.json",
        "/etc/passwd",
        "CON",
    ],
)
def test_a_name_that_is_really_a_path_is_refused(tmp_path, value):
    """The rule that makes "it only ever reads its own data folder" true by construction."""
    path, reason = resolve_map_file(tmp_path, value)

    assert path is None
    assert reason not in ("", "disabled")


def test_the_map_and_the_download_folder_share_one_name_check(tmp_path):
    """Two copies of "is this a path in disguise?" would eventually disagree.

    The disagreement would be the half that forgot to check for a drive letter, so the two
    callers are asserted to agree on a set of values rather than trusted to.
    """
    for value in ("ok", "sub/dir", "..", "C:thing", "CON", "a?b"):
        assert is_safe_component(value)[0] == (resolve_folder(tmp_path, value)[0] is not None)
        assert resolve_map_file(tmp_path, value)[0] is None or value == "ok"
