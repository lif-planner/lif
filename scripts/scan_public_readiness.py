#!/usr/bin/env python3
"""Scan tracked files for private-looking references before a public export."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


TEXT_EXTENSIONS = {
    ".cfg",
    ".css",
    ".csv",
    ".example",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".lock",
    ".md",
    ".po",
    ".py",
    ".sh",
    ".svg",
    ".txt",
    ".xml",
    ".yml",
    ".yaml",
}

SKIP_PATH_PREFIXES = (
    "planner/static/vendor/",
    "staticfiles/",
)

SKIP_FILES = {
    "LICENSE",
    "scripts/scan_public_readiness.py",
}

ALLOWLIST = set()


@dataclass(frozen=True)
class Finding:
    path: Path
    line_number: int
    label: str
    value: str


PATTERNS = [
    ("personal macOS home path", re.compile(r"/Users/chr\b")),
    ("private username/email", re.compile(r"\bchr@")),
    ("private home domain", re.compile(r"\bstudio\.home\b")),
    ("private surname/domain", re.compile(r"\btreutler\b", re.IGNORECASE)),
    ("private launchd label", re.compile(r"\bonline\.yogitea\.lif\b")),
    ("private Mac host", re.compile(r"\bmacmini\.local\b")),
    ("private LAN subnet", re.compile(r"\b192\.168\.(?:82|83)\.\d{1,3}\b")),
    ("private repo URL", re.compile(r"git@github\.com:yogitea/LiF\.git")),
    ("private repo path", re.compile(r"github\.com/yogitea/LiF")),
]


def git(args: list[str]) -> str:
    result = subprocess.run(["git", *args], check=True, capture_output=True, text=True)
    return result.stdout


def tracked_files() -> list[Path]:
    return [Path(line) for line in git(["ls-files"]).splitlines() if line]


def should_scan(path: Path) -> bool:
    path_text = path.as_posix()
    if path_text in SKIP_FILES:
        return False
    if any(path_text.startswith(prefix) for prefix in SKIP_PATH_PREFIXES):
        return False
    return path.suffix.lower() in TEXT_EXTENSIONS or "." not in path.name


def file_content(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return None


def allowed(path: Path, value: str) -> bool:
    path_text = path.as_posix()
    return (path_text, value) in ALLOWLIST


def scan() -> list[Finding]:
    findings = []
    for path in tracked_files():
        if not should_scan(path):
            continue
        content = file_content(path)
        if content is None:
            continue
        for line_number, line in enumerate(content.splitlines(), start=1):
            for label, pattern in PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                value = match.group(0)
                if allowed(path, value):
                    continue
                findings.append(Finding(path=path, line_number=line_number, label=label, value=value))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--allow-open-decisions",
        action="store_true",
        help="Accepted for readability; open repo-name decisions are allowlisted by default.",
    )
    parser.parse_args()

    findings = scan()
    if not findings:
        print("Public readiness scan passed.")
        return 0

    print("Public readiness scan found private-looking references:", file=sys.stderr)
    for finding in findings:
        print(
            f"- {finding.path}:{finding.line_number}: {finding.label}: {finding.value}",
            file=sys.stderr,
        )
    print("Review or replace these before creating the public release branch.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
