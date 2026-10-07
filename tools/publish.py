#!/usr/bin/env python3
"""Put this branch on GitHub: push it, open a pull request, optionally merge it.

The trust boundary here is one function and one command. The token is read in
:func:`find_token`, placed into the environment for exactly one ``git`` invocation
(:func:`run_with_token`), and never printed, logged, or written anywhere — including
``.git/config``, which is checked afterwards to be sure.

A token **file** is the recommended source: it then never passes through a chat log, a shell
history, or a process listing. Lookup order:

1. ``$GITHUB_TOKEN``
2. ``$GH_TOKEN``
3. ``~/.workbuddy/gh_token``
4. ``<repo>/.secrets/gh_token``

``.secrets/`` is already in ``.gitignore``. The token needs the ``repo`` scope
(``public_repo`` is enough for a public repository); create a classic one at
https://github.com/settings/tokens — 7 days is plenty for a one-off push.

**Where the repository is** comes from the ``origin`` remote when one exists, not from the
token's account. That distinction is not academic: a token belonging to a person, pointed at a
repository that lives under an organisation, would otherwise look for — and offer to *create* —
a second repository under the person's own account.

Usage::

    python tools/publish.py --dry-run                  # report what would happen; change nothing
    python tools/publish.py                            # create the repository (if absent) and push
    python tools/publish.py --private                  # create it private instead
    python tools/publish.py --pr --title "..." --body-file pr.md
                                                       # push the branch and open a pull request
    python tools/publish.py --pr --title "..." --body-file pr.md --merge
                                                       # ... and merge it, then delete the branch

Exit codes: 0 success, 1 a problem the user has to resolve.
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
API = "https://api.github.com"

TOKEN_FILES = (
    Path.home() / ".workbuddy" / "gh_token",
    REPO / ".secrets" / "gh_token",
)

DEFAULT_NAME = "MCDR-ModUpdateChecker"
DEFAULT_BRANCH = "main"

DESCRIPTION = (
    "MCDR plugin that compares the server's installed Fabric mods against Modrinth and "
    "reports which ones are out of date."
)


class Failure(Exception):
    """Something the user has to resolve. Printed without a traceback."""


# --------------------------------------------------------------------------------------
# The token
# --------------------------------------------------------------------------------------


def find_token(explicit: str = "") -> str:
    """Locate the token, or raise with instructions. Never returns it to a log."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise Failure("no such token file: {}".format(path))
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise Failure("{} is empty".format(path))
        print("token source  : {} ({} chars)".format(path, len(value)))
        return value

    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = (os.environ.get(name) or "").strip()
        if value:
            print("token source  : ${} ({} chars)".format(name, len(value)))
            return value

    for path in TOKEN_FILES:
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                print("token source  : {} ({} chars)".format(path, len(value)))
                return value
            raise Failure("{} exists but is empty".format(path))

    raise Failure(
        "no token found. Put one in {}\n"
        "  (or set $GITHUB_TOKEN, or pass --token-file).\n"
        "  Create a classic token with the 'repo' scope at "
        "https://github.com/settings/tokens".format(TOKEN_FILES[0])
    )


def call(method: str, path: str, token: str, payload=None):
    """A GitHub API call. The token only ever appears in the request header."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(API + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "MCDR-ModUpdateChecker-publish")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
            return response.status, (json.loads(body) if body.strip() else None)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"message": body[:300]}
        return error.code, parsed
    except urllib.error.URLError as error:
        raise Failure("could not reach api.github.com: {}".format(error.reason)) from error


# --------------------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------------------


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git in the repository. ``check=False`` returns the failure instead of raising."""
    return subprocess.run(
        ["git", *args],
        cwd=str(REPO),
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def run_with_token(token: str, args: list, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run one ``git`` command with the token supplied out-of-band.

    Three things are deliberately avoided:

    * putting the token in the remote URL, which would persist it in ``.git/config`` for
      anyone who reads the file later;
    * passing it on the command line, where it is visible in a process listing;
    * writing it anywhere, so there is nothing to clean up afterwards.

    Instead the credential is handed to git through ``http.extraheader`` supplied via
    ``GIT_CONFIG_*`` environment variables, which apply to this one child process only.
    """
    basic = base64.b64encode(
        "x-access-token:{}".format(token).encode("utf-8")
    ).decode("ascii")

    environment = dict(os.environ)
    environment.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": "Authorization: Basic " + basic,
            # Never fall back to a stored credential helper that might prompt or use another
            # account; this push is meant to use exactly the token we were given.
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
        }
    )

    return subprocess.run(
        ["git", *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=environment,
    )


def push_with_token(token: str, ref: str, ref_prefix: str = "refs/heads/") -> None:
    """Push ``HEAD`` to one ref on ``origin``, explaining a rejection in plain words.

    ``ref_prefix`` is what makes this usable for a tag as well as a branch — the credential
    handling is identical and worth having in one place.
    """
    result = run_with_token(token, ["push", "--quiet", "origin", "HEAD:{}".format(ref_prefix + ref)])
    if result.returncode != 0:
        message = ((result.stderr or "") + (result.stdout or "")).replace(token, "***")
        hint = ""
        if "non-fast-forward" in message or "rejected" in message:
            hint = (
                "\n  The remote already has commits this repository does not. Nothing was "
                "overwritten.\n  Either push to a new branch, or reconcile the histories "
                "deliberately — do not force-push."
            )
        raise Failure("push rejected:\n{}{}".format(message.strip(), hint))


def assert_no_token_in_config(token: str) -> None:
    """Prove the token did not leak into ``.git/config``."""
    config = (REPO / ".git" / "config").read_text(encoding="utf-8", errors="replace")
    if token in config:
        raise Failure(
            "the token ended up in .git/config, which must not happen. "
            "Remove the 'origin' remote and revoke the token at "
            "https://github.com/settings/tokens"
        )
    print("token leak    : none in .git/config")


def describe_local_state() -> tuple:
    branch = git("symbolic-ref", "--short", "HEAD").stdout.strip()
    commits = git("rev-list", "--count", "HEAD").stdout.strip()
    files = git("ls-files").stdout.strip().splitlines()
    dirty = git("status", "--porcelain").stdout.strip()
    return branch, commits, files, dirty


# --------------------------------------------------------------------------------------


def origin_name() -> str:
    """``owner/name`` of the ``origin`` remote, or ``""`` when there is none.

    The owner comes from the remote rather than from the token's account on purpose: this
    repository lives under an organisation while the token belongs to a person, and deriving
    the target from the login would send the tool looking for ``<person>/<repo>`` and then
    offering to *create* it — a second, empty repository that looks like the real one.
    """
    remotes = git("remote").stdout.split()
    if "origin" not in remotes:
        return ""
    url = git("remote", "get-url", "origin").stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
    return "{}/{}".format(match.group(1), match.group(2)) if match else ""


def open_pull_request(token: str, full_name: str, head: str, base: str,
                      title: str, body: str) -> dict:
    status, payload = call(
        "POST",
        "/repos/{}/pulls".format(full_name),
        token,
        {"title": title, "body": body, "head": head, "base": base},
    )
    if status not in (200, 201) or not payload:
        raise Failure("could not open the pull request (HTTP {}): {}".format(
            status, (payload or {}).get("message", "no message")
        ))
    return payload


def merge_pull_request(token: str, full_name: str, number: int, title: str) -> dict:
    """Merge with a merge commit, so the branch's own commits stay visible in the history."""
    status, payload = call(
        "PUT",
        "/repos/{}/pulls/{}/merge".format(full_name, number),
        token,
        {
            "merge_method": "merge",
            "commit_title": "{} (#{})".format(title, number),
        },
    )
    if status != 200 or not (payload or {}).get("merged"):
        raise Failure("could not merge the pull request (HTTP {}): {}".format(
            status, (payload or {}).get("message", "no message")
        ))
    return payload


def delete_branch(token: str, full_name: str, branch: str) -> None:
    status, payload = call("DELETE", "/repos/{}/git/refs/heads/{}".format(full_name, branch), token)
    if status not in (204, 200):
        # Not fatal: the pull request is merged and the branch is harmless.
        print("warning       : could not delete the branch: {}".format(
            (payload or {}).get("message", status)
        ))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--name", default=DEFAULT_NAME, help="repository name, if creating one")
    parser.add_argument("--private", action="store_true", help="create it private")
    parser.add_argument("--dry-run", action="store_true", help="change nothing")
    parser.add_argument("--token-file", default="", help="read the token from this file")
    parser.add_argument("--pr", action="store_true",
                        help="open a pull request instead of pushing straight to the default branch")
    parser.add_argument("--merge", action="store_true", help="also merge the pull request (--pr)")
    parser.add_argument("--base", default=DEFAULT_BRANCH, help="the branch to merge into")
    parser.add_argument("--title", default="", help="pull request title")
    parser.add_argument("--body-file", default="", help="file holding the pull request body")
    args = parser.parse_args()

    if args.merge and not args.pr:
        parser.error("--merge only makes sense with --pr")
    if args.pr and not (args.title and args.body_file):
        parser.error("--pr needs --title and --body-file")

    branch, commits, files, dirty = describe_local_state()
    print("repository    : {}".format(REPO))
    print("branch        : {}".format(branch))
    print("commits       : {}".format(commits))
    print("tracked files : {}".format(len(files)))
    if dirty:
        print()
        print("UNCOMMITTED CHANGES (these will NOT be pushed):")
        for line in dirty.splitlines():
            print("  " + line)

    token = find_token(args.token_file)

    status_code, user = call("GET", "/user", token)
    if status_code != 200:
        raise Failure(
            "the token was rejected (HTTP {}): {}".format(
                status_code, (user or {}).get("message", "no message from GitHub")
            )
        )
    login = user["login"]
    print("authenticated : {}".format(login))

    configured = origin_name()
    full_name = configured or "{}/{}".format(login, args.name)
    print("target repo   : {}{}".format(
        full_name,
        " (from origin)" if configured else " (from the token's account)",
    ))

    status_code, existing = call("GET", "/repos/{}".format(full_name), token)
    already_exists = status_code == 200
    if already_exists:
        print("state         : already exists — it will be reused, not recreated")
        print("                {}".format(existing["html_url"]))
    elif status_code == 404:
        if configured:
            raise Failure(
                "origin points at {}, which this token cannot see. Either the token needs "
                "access to it, or the remote is wrong — this tool will not create a second "
                "repository under {}.".format(full_name, login)
            )
        print("state         : does not exist yet — it will be created")
    else:
        raise Failure(
            "could not check whether {} exists (HTTP {}): {}".format(
                full_name, status_code, (existing or {}).get("message", "no message")
            )
        )

    body = ""
    if args.pr:
        body_path = Path(args.body_file).expanduser()
        if not body_path.is_file():
            raise Failure("no such file: {}".format(body_path))
        body = body_path.read_text(encoding="utf-8")

    if args.dry_run:
        print()
        print("dry run: nothing was created and nothing was pushed")
        if args.pr:
            print("dry run: would open a pull request {} -> {} titled {!r}".format(
                branch, args.base, args.title
            ))
            if args.merge:
                print("dry run: and would merge it with a merge commit")
        return 0

    if not already_exists:
        status_code, created = call(
            "POST",
            "/user/repos",
            token,
            {
                "name": args.name,
                "private": bool(args.private),
                "has_issues": True,
                "has_wiki": False,
                "has_projects": False,
                "auto_init": False,
                "description": DESCRIPTION,
            },
        )
        if status_code not in (200, 201):
            raise Failure(
                "could not create the repository (HTTP {}): {}".format(
                    status_code, (created or {}).get("message", "no message")
                )
            )
        print("created       : {}".format(created["html_url"]))
        clean_url = created["clone_url"]
    else:
        clean_url = existing["clone_url"]

    # The remote always holds the clean URL; the credential is supplied per-push instead.
    if "origin" in git("remote").stdout.split():
        git("remote", "set-url", "origin", clean_url)
        print("remote        : origin updated")
    else:
        git("remote", "add", "origin", clean_url)
        print("remote        : origin added")

    push_with_token(token, branch)
    assert_no_token_in_config(token)

    # Create the remote-tracking ref so the local branch has an upstream to compare against.
    # Done after the push, from the clean URL, so the upstream is never a token-bearing URL.
    fetch = git("fetch", "--quiet", "origin", check=False)
    if fetch.returncode == 0:
        git("branch", "--set-upstream-to=origin/{}".format(branch), branch, check=False)
        print("upstream      : {}/{}".format("origin", branch))

    print("pushed        : {} commit(s) to origin/{}".format(commits, branch))

    if not args.pr:
        print()
        print("done: https://github.com/{}/tree/{}".format(full_name, branch))
        print()
        print("When you are finished: delete the token file and revoke the token at")
        print("https://github.com/settings/tokens")
        return 0

    pull = open_pull_request(token, full_name, branch, args.base, args.title, body)
    print("pull request  : #{} {}".format(pull["number"], pull["html_url"]))

    if args.merge:
        merged = merge_pull_request(token, full_name, pull["number"], args.title)
        print("merged        : {}".format(merged["sha"][:12]))
        delete_branch(token, full_name, branch)
        # Local ``main`` is now behind: move it onto the merge commit so the next release is
        # cut from what is actually on the remote.
        git("fetch", "--quiet", "origin", check=False)
        git("checkout", args.base, check=False)
        git("merge", "--ff-only", "origin/{}".format(args.base), check=False)
        print("local {}  : {}".format(args.base, git("rev-parse", "HEAD").stdout.strip()[:12]))

    print()
    print("done: {}".format(pull["html_url"]))
    print()
    print("When you are finished: delete the token file and revoke the token at")
    print("https://github.com/settings/tokens")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failure as error:
        print()
        print("FAILED: {}".format(error), file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as error:
        print()
        print(
            "FAILED: git exited {}: {}".format(
                error.returncode, (error.stderr or "").strip()
            ),
            file=sys.stderr,
        )
        sys.exit(1)
