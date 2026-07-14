#!/usr/bin/env python3
"""Guard public LiF commits against accidental personal committer metadata."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass


EXPECTED_NAME = os.environ.get("LIF_PUBLIC_GIT_NAME", "LiF Maintainers")
EXPECTED_EMAIL = os.environ.get(
    "LIF_PUBLIC_GIT_EMAIL", "yogitea@users.noreply.github.com"
)
PUBLIC_BRANCH = os.environ.get("LIF_PUBLIC_BRANCH", "public-release")
ZERO_SHA = "0" * 40


@dataclass(frozen=True)
class Identity:
    name: str
    email: str


def run_git(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def parse_ident(value: str) -> Identity:
    match = re.match(r"^(.*?) <([^>]+)> ", value)
    if not match:
        raise ValueError(f"Could not parse Git identity: {value!r}")
    return Identity(match.group(1), match.group(2))


def expected(identity: Identity) -> bool:
    return identity.name == EXPECTED_NAME and identity.email == EXPECTED_EMAIL


def format_expected() -> str:
    return f"{EXPECTED_NAME} <{EXPECTED_EMAIL}>"


def current_branch() -> str:
    try:
        return run_git(["branch", "--show-current"])
    except subprocess.CalledProcessError:
        return ""


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def check_public_branch_committer() -> None:
    branch = current_branch()
    require = os.environ.get("LIF_REQUIRE_MAINTAINER_IDENTITY") == "1"
    if branch != PUBLIC_BRANCH and not require:
        return

    committer = parse_ident(run_git(["var", "GIT_COMMITTER_IDENT"]))
    if expected(committer):
        return

    fail(
        "\n".join(
            [
                "Refusing to commit with the wrong public committer identity.",
                f"Expected: {format_expected()}",
                f"Found:    {committer.name} <{committer.email}>",
                "",
                "For public commits, use:",
                f"  git config user.name {EXPECTED_NAME!r}",
                f"  git config user.email {EXPECTED_EMAIL!r}",
                "",
                "Or for a one-off commit/amend:",
                f"  GIT_AUTHOR_NAME={EXPECTED_NAME!r} \\",
                f"  GIT_AUTHOR_EMAIL={EXPECTED_EMAIL!r} \\",
                f"  GIT_COMMITTER_NAME={EXPECTED_NAME!r} \\",
                f"  GIT_COMMITTER_EMAIL={EXPECTED_EMAIL!r} \\",
                "  git commit --amend --no-edit --reset-author",
            ]
        )
    )


def commit_metadata(commit: str) -> tuple[Identity, Identity]:
    raw = run_git(["show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce", commit])
    author_name, author_email, committer_name, committer_email = raw.split("\0")
    return Identity(author_name, author_email), Identity(committer_name, committer_email)


def commits_in_range(base: str, head: str) -> list[str]:
    if head == ZERO_SHA:
        return []
    if base == ZERO_SHA:
        return run_git(["rev-list", head]).splitlines()
    return run_git(["rev-list", f"{base}..{head}"]).splitlines()


def check_public_range(base: str, head: str, label: str) -> None:
    bad: list[str] = []
    total_bad = 0
    for commit in commits_in_range(base, head):
        author, committer = commit_metadata(commit)
        if not expected(author) or not expected(committer):
            total_bad += 1
            if len(bad) < 10:
                bad.append(
                    "\n".join(
                        [
                            f"- {commit[:12]} in {label}",
                            f"  author:    {author.name} <{author.email}>",
                            f"  committer: {committer.name} <{committer.email}>",
                        ]
                    )
                )

    if bad:
        remaining = total_bad - len(bad)
        if remaining:
            bad.append(f"... plus {remaining} more commit(s).")
        fail(
            "\n".join(
                [
                    "Refusing to push public history with unexpected Git identity.",
                    f"Expected every public author and committer to be: {format_expected()}",
                    "",
                    *bad,
                    "",
                    "Fix the commits before pushing, for example:",
                    f"  GIT_AUTHOR_NAME={EXPECTED_NAME!r} \\",
                    f"  GIT_AUTHOR_EMAIL={EXPECTED_EMAIL!r} \\",
                    f"  GIT_COMMITTER_NAME={EXPECTED_NAME!r} \\",
                    f"  GIT_COMMITTER_EMAIL={EXPECTED_EMAIL!r} \\",
                    "  git commit --amend --no-edit --reset-author",
                ]
            )
        )


def check_public_tag(head: str, label: str) -> None:
    if head == ZERO_SHA:
        return

    commit = run_git(["rev-parse", f"{head}^{{commit}}"])
    author, committer = commit_metadata(commit)
    if expected(author) and expected(committer):
        return

    fail(
        "\n".join(
            [
                "Refusing to push public tag with unexpected target commit identity.",
                f"Expected tag target author and committer to be: {format_expected()}",
                "",
                f"- {commit[:12]} in {label}",
                f"  author:    {author.name} <{author.email}>",
                f"  committer: {committer.name} <{committer.email}>",
                "",
                "Move the tag to a public-safe commit before pushing.",
            ]
        )
    )


def is_public_destination(remote_name: str, remote_url: str, remote_ref: str) -> bool:
    public_markers = (
        "github.com:lif-planner/lif.git",
        "github.com/lif-planner/lif.git",
        "github.com/lif-planner/lif",
    )
    return (
        remote_name == "public"
        or any(marker in remote_url for marker in public_markers)
        or remote_ref == "refs/heads/main"
        and current_branch() == PUBLIC_BRANCH
    )


def check_pre_push(remote_name: str, remote_url: str) -> None:
    for line in sys.stdin:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        if not is_public_destination(remote_name, remote_url, remote_ref):
            continue
        if remote_ref.startswith("refs/tags/"):
            check_public_tag(local_sha, f"{local_ref} -> {remote_ref}")
            continue
        check_public_range(remote_sha, local_sha, f"{local_ref} -> {remote_ref}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pre-commit",
        action="store_true",
        help="Reject commits on the public branch with the wrong committer.",
    )
    parser.add_argument(
        "--pre-push",
        nargs=2,
        metavar=("REMOTE_NAME", "REMOTE_URL"),
        help="Reject public pushes with wrong author or committer metadata.",
    )
    args = parser.parse_args()

    if args.pre_commit:
        check_public_branch_committer()
    if args.pre_push:
        check_pre_push(args.pre_push[0], args.pre_push[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
