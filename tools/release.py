#!/usr/bin/env python3
"""Tag, publish and verify a GitHub release for this plugin.

The release is the one step that cannot be undone quietly: a tag is public the moment it is
pushed, and an asset attached to it is what users download. So this tool does three things the
manual route kept getting wrong, and refuses to run when it cannot:

* **it packs a fresh artifact and checks it**, from the current commit — the file in the working
  tree may be from an earlier build, and shipping that means the release and the tag disagree;
* **the release body is the top section of ``CHANGELOG.md``**, which is already how this project
  keeps one release per file. Nothing is retyped, so the notes cannot drift from the changelog;
* **the uploaded asset is downloaded again and compared by sha256.** An upload that silently
  truncated would otherwise be discovered by a user.

The token is read the same way as :mod:`tools.publish` — ``$GITHUB_TOKEN``, ``$GH_TOKEN``, then
``~/.workbuddy/gh_token`` / ``<repo>/.secrets/gh_token`` — and is only ever put in a request
header or a single ``git`` child process's environment. It is never written to disk.

Usage::

    python tools/release.py --check     # verify everything, print the plan, change nothing
    python tools/release.py             # tag, create the release, upload, verify

Exit codes: 0 success, 1 a problem the user has to resolve.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO))

import pack  # noqa: E402
import publish  # noqa: E402  - reuses find_token / call / Failure

API = publish.API
Failure = publish.Failure

#: Section heading for one release in ``CHANGELOG.md``, e.g. ``## v1.0.1``.
_RELEASE_HEADING = re.compile(r"^##\s+v?(\S+)(.*)$")


def plugin_metadata() -> dict:
    return json.loads((REPO / "mcdreforged.plugin.json").read_text(encoding="utf-8"))


def release_notes(version: str) -> str:
    """The changelog section for ``version``, as the release body.

    ``CHANGELOG.md`` keeps exactly one released version (older entries live on the release
    page), so the section is looked up rather than assumed to be first — a mistake that would
    publish the *previous* release's notes under the new tag.
    """
    lines = (REPO / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    collected: list = []
    inside = False
    for line in lines:
        match = _RELEASE_HEADING.match(line)
        if match:
            if inside:
                break
            if match.group(1).lstrip("v") == version:
                inside = True
                continue
        if inside:
            collected.append(line)

    if not inside:
        raise Failure(
            "CHANGELOG.md has no section for version {} — the release body would be empty. "
            "Add it first (the file keeps only the latest release).".format(version)
        )
    body = "\n".join(collected).strip()
    if not body:
        raise Failure("the changelog section for {} is empty".format(version))
    return body


def working_tree_state() -> tuple:
    """``(branch, dirty, head)`` for the repository this release is being cut from."""
    branch = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"], cwd=str(REPO),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(REPO),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(REPO),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return branch, dirty, head


def remote_head(branch: str) -> str:
    result = subprocess.run(
        ["git", "ls-remote", "origin", "refs/heads/{}".format(branch)], cwd=str(REPO),
        capture_output=True, text=True,
    )
    return result.stdout.split()[0] if result.stdout.strip() else ""


def build_fresh_artifact(version: str) -> Path:
    """Pack from the current commit, check it, and leave it as the delivery copy.

    Built into a temporary directory and only copied next to the repository once it passes, so
    a failed check cannot leave a plausible-looking ``.mcdr`` sitting where someone would pick
    it up by hand.
    """
    with tempfile.TemporaryDirectory(prefix="muc_release_") as folder:
        candidate = Path(folder) / "candidate.mcdr"
        pack.build(candidate)

        check = subprocess.run(
            [sys.executable, str(REPO / "tools" / "check_artifact.py"), str(candidate)],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if check.returncode != 0:
            raise Failure(
                "the freshly packed artifact failed its own checks:\n{}{}".format(
                    check.stdout, check.stderr
                )
            )

        delivery = REPO / "ModUpdateChecker-v{}.mcdr".format(version)
        delivery.write_bytes(candidate.read_bytes())
    return delivery


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def repository_name(token: str) -> tuple:
    """``(owner, name)`` of this checkout's remote, as GitHub reports it."""
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=str(REPO),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", result)
    if not match:
        raise Failure("the origin remote does not look like a github repository: {}".format(result))
    owner, name = match.group(1), match.group(2)

    # Confirm it is reachable with this token, so "404" later cannot mean two different things.
    status, payload = publish.call("GET", "/repos/{}/{}".format(owner, name), token)
    if status != 200:
        raise Failure(
            "the token cannot see {}/{} (HTTP {}): {}".format(
                owner, name, status, (payload or {}).get("message", "no message")
            )
        )
    return owner, name


def tagged_already(token: str, owner: str, name: str, tag: str) -> bool:
    status, _payload = publish.call("GET", "/repos/{}/{}/git/ref/tags/{}".format(owner, name, tag), token)
    return status == 200


def push_tag(token: str, tag: str, message: str) -> None:
    """Create an annotated tag and push just that tag, with the token kept out of argv."""
    subprocess.run(
        ["git", "tag", "-a", tag, "-m", message], cwd=str(REPO),
        capture_output=True, text=True, check=True,
    )
    publish.push_with_token(token, tag, ref_prefix="refs/tags/")


def upload_asset(token: str, upload_url: str, path: Path) -> dict:
    """Upload one asset to ``upload_url``, which the release response supplies.

    The URL comes from the API rather than being assembled here, and that is not merely tidier:
    asset uploads are **not** served by ``api.github.com``. Building the path against that host
    returns a bare ``404 Not Found`` — after the tag has been pushed and the release created, so
    the failure lands on a release that already exists and is missing its only file. The
    ``upload_url`` GitHub hands back points at ``uploads.github.com``, and the ``{?name,label}``
    on the end is an RFC 6570 template that has to be replaced rather than kept.
    """
    base = upload_url.split("{", 1)[0]
    url = "{}?{}".format(base, urllib.parse.urlencode({"name": path.name}))
    request = urllib.request.Request(url, data=path.read_bytes(), method="POST")
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Content-Type", "application/octet-stream")
    request.add_header("User-Agent", "MCDR-ModUpdateChecker-release")
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise Failure(
            "could not upload {} (HTTP {}): {}".format(path.name, error.code, body[:300])
        ) from error


def download_asset(url: str, destination: Path) -> None:
    request = urllib.request.Request(url)
    request.add_header("User-Agent", "MCDR-ModUpdateChecker-release")
    with urllib.request.urlopen(request, timeout=300) as response:
        destination.write_bytes(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--check", action="store_true", help="verify and print the plan only")
    parser.add_argument("--token-file", default="", help="read the token from this file")
    args = parser.parse_args()

    metadata = plugin_metadata()
    version = metadata["version"]
    tag = "v{}".format(version)
    notes = release_notes(version)
    branch, dirty, head = working_tree_state()

    print("plugin          : {} v{}".format(metadata["id"], version))
    print("tag             : {}".format(tag))
    print("branch          : {} ({})".format(branch, head[:12]))
    print("changelog notes : {} chars".format(len(notes)))
    print()

    if dirty:
        raise Failure(
            "the working tree has uncommitted changes, so the artifact would not match the "
            "tag:\n{}".format(dirty)
        )

    upstream = remote_head(branch)
    if upstream and upstream != head:
        raise Failure(
            "{} is not pushed: local {} but origin {}.\n"
            "Merge the pull request first, then release from the merged commit — an artifact "
            "built from an unpushed commit cannot be reproduced by anyone.".format(
                branch, head[:12], upstream[:12]
            )
        )
    if not upstream:
        raise Failure("origin has no branch {}; push it first".format(branch))

    artifact = build_fresh_artifact(version)
    digest = sha256(artifact)
    print("artifact        : {} ({} bytes)".format(artifact.name, artifact.stat().st_size))
    print("sha256          : {}".format(digest))
    print()

    if args.check:
        print("check only: nothing was tagged, published or uploaded")
        return 0

    token = publish.find_token(args.token_file)
    owner, name = repository_name(token)

    if tagged_already(token, owner, name, tag):
        raise Failure("tag {} already exists on {}/{}".format(tag, owner, name))

    push_tag(token, tag, "{} v{}".format(metadata["name"], version))
    print("tag pushed      : {}".format(tag))

    status, release = publish.call(
        "POST",
        "/repos/{}/{}/releases".format(owner, name),
        token,
        {
            "tag_name": tag,
            "name": "{} v{}".format(metadata["name"], version),
            "body": notes,
            "draft": False,
            "prerelease": False,
        },
    )
    if status not in (200, 201) or not release:
        raise Failure(
            "could not create the release (HTTP {}): {}".format(
                status, (release or {}).get("message", "no message")
            )
        )
    print("release         : {}".format(release["html_url"]))

    uploaded = upload_asset(token, release["upload_url"], artifact)
    print("asset           : {} ({} bytes reported)".format(
        uploaded["name"], uploaded.get("size", "?")
    ))

    # Downloaded again on purpose: an upload that truncated, or an asset that GitHub rewrote,
    # is otherwise only discovered by the user who unzips it into their plugins folder.
    with tempfile.TemporaryDirectory(prefix="muc_release_verify_") as folder:
        round_trip = Path(folder) / artifact.name
        download_asset(uploaded["browser_download_url"], round_trip)
        if sha256(round_trip) != digest:
            raise Failure(
                "the uploaded asset does not match the built one:\n  built {}\n  fetched {}".format(
                    digest, sha256(round_trip)
                )
            )
    print("verified        : re-downloaded asset matches sha256")
    print()
    print("done: {}".format(release["html_url"]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failure as error:
        print()
        print("FAILED: {}".format(error), file=sys.stderr)
        sys.exit(1)
