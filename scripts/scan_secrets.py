#!/usr/bin/env python3
"""Small local secret scanner for pre-commit checks.

This is intentionally dependency-free so it works before the Python/Django
environment is installed. It is not a replacement for a dedicated scanner such
as gitleaks, but it catches common accidental leaks before a commit is created.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path


SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github classic token", re.compile(r"\bghp_[A-Za-z0-9_]{36,}\b")),
    ("github fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("openai api key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("bearer token", re.compile(r"(?i)\bAuthorization\s*:\s*Bearer\s+[A-Za-z0-9._~+/=-]{20,}")),
]

SENSITIVE_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b(secret|password|passwd|token|api[_-]?key|private[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token)\b
    [^=\n:]{0,40}
    [:=]
    \s*
    ['"]([^'"]{8,})['"]
    """
)

ALLOWLIST_VALUES = {
    "django-insecure-local-dev-only",
}

ALLOWLIST_SUBSTRINGS = (
    "DJANGO_SECRET_KEY",
    "csrf_token",
)

TEXT_EXTENSIONS = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".lock",
    ".md",
    ".py",
    ".sh",
    ".txt",
    ".yml",
    ".yaml",
}


def git(args):
    result = subprocess.run(["git", *args], check=True, capture_output=True, text=True)
    return result.stdout


def tracked_files(staged=False):
    if staged:
        output = git(["diff", "--cached", "--name-only", "--diff-filter=ACMR"])
    else:
        output = git(["ls-files"])
    return [Path(line) for line in output.splitlines() if line]


def staged_content(path):
    try:
        return git(["show", f":{path.as_posix()}"])
    except subprocess.CalledProcessError:
        return None


def file_content(path, staged=False):
    if staged:
        return staged_content(path)
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError):
        return None


def should_scan(path):
    if path.parts and path.parts[0] in {".git", "__pycache__"}:
        return False
    if path.name in {"Pipfile.lock"}:
        return True
    return path.suffix.lower() in TEXT_EXTENSIONS or "." not in path.name


def allowed_line(line):
    return any(value in line for value in ALLOWLIST_SUBSTRINGS)


def scan_content(path, content):
    findings = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        if allowed_line(line):
            continue
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append((path, line_number, label))
        assignment = SENSITIVE_ASSIGNMENT.search(line)
        if assignment and assignment.group(2) not in ALLOWLIST_VALUES:
            findings.append((path, line_number, "sensitive-looking assignment"))
    return findings


def scan(staged=False):
    findings = []
    for path in tracked_files(staged=staged):
        if not should_scan(path):
            continue
        content = file_content(path, staged=staged)
        if content is None:
            continue
        findings.extend(scan_content(path, content))
    return findings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", action="store_true", help="Scan staged content instead of tracked files.")
    args = parser.parse_args()

    findings = scan(staged=args.staged)
    if not findings:
        print("Secret scan passed.")
        return 0

    print("Secret scan found potential issues:", file=sys.stderr)
    for path, line_number, label in findings:
        print(f"- {path}:{line_number}: {label}", file=sys.stderr)
    print("Review these lines before committing.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
