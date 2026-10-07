#!/usr/bin/env python3
"""Create the GitHub repository and push to it, using a personal access token.

The trust boundary here is one function and one command. The token is read in
:func:`find_token`, placed into the environment for exactly one ``git`` invocation
(:func:`push_with_token`), and never printed, logged, or written anywhere — including
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

Usage::

    python tools/publish.py --dry-run        # report what would happen; change nothing
    python tools/publish.py                  # create the repository and push
    python tools/publish.py --private        # create it private instead

Exit codes: 0 success, 1 a problem the user has to resolve.
"""

import argparse
import base64
import json
import os
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

DESCRIPTION = (
    "MCDR plugin that compares the server's installed Fabric mods against Modrinth and "
    "CurseForge, and reports which ones are out of date."
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


def push_with_token(token: str, branch: str) -> None:
    """Push using the token, keeping it out of both ``argv`` and ``.git/config``.

    Two things are deliberately avoided:

    * putting the token in the remote URL, which would persist it in ``.git/config`` for
      anyone who reads the file later;
    * passing it on the command line, where it is visible in a process listing.

    Instead the credential is handed to git through ``http.extraheader`` supplied via
    ``GIT_CONFIG_*`` environment variables, which apply to this one child process only.
    Nothing is written to disk, so there is nothing to clean up afterwards.
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

    result = subprocess.run(
        ["git", "push", "--quiet", "origin", "HEAD:refs/heads/{}".format(branch)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--name", default=DEFAULT_NAME, help="repository name")
    parser.add_argument("--private", action="store_true", help="create it private")
    parser.add_argument("--dry-run", action="store_true", help="change nothing")
    parser.add_argument("--token-file", default="", help="read the token from this file")
    args = parser.parse_args()

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

    full_name = "{}/{}".format(login, args.name)
    print("target repo   : {}{}".format(full_name, " (private)" if args.private else ""))

    status_code, existing = call("GET", "/repos/{}".format(full_name), token)
    already_exists = status_code == 200
    if already_exists:
        print("state         : already exists — it will be reused, not recreated")
        print("                {}".format(existing["html_url"]))
    elif status_code == 404:
        print("state         : does not exist yet — it will be created")
    else:
        raise Failure(
            "could not check whether {} exists (HTTP {}): {}".format(
                full_name, status_code, (existing or {}).get("message", "no message")
            )
        )

    if args.dry_run:
        print()
        print("dry run: nothing was created and nothing was pushed")
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
    print()
    print("done: https://github.com/{}/tree/{}".format(full_name, branch))
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
