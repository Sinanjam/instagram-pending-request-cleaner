#!/usr/bin/env python3
"""Conservative repository privacy/secret preflight for CI and releases."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".direnv", "result", "__pycache__"}
FORBIDDEN_NAMES = {
    "progress.json", "deferred.json", "actions.jsonl",
    "pending_follow_requests.json", "cookies.sqlite", "logins.json", "key4.db",
}
BINARY_SUFFIXES = {".zip", ".png", ".jpg", ".jpeg", ".sqlite", ".jsonlz4"}
PATTERNS = {
    "absolute home path": re.compile("/" + r"home/[A-Za-z0-9._-]+/"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "generic bearer token": re.compile(r"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._~-]{16,}"),
}


def main() -> int:
    findings: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRS or part.startswith("result-") for part in relative.parts):
            continue
        if path.is_dir():
            continue
        if path.name in FORBIDDEN_NAMES or path.suffix.lower() in BINARY_SUFFIXES:
            findings.append(f"forbidden artifact: {relative}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(f"unexpected binary file: {relative}")
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{label}: {relative}")
    if findings:
        print("Privacy scan failed:", file=sys.stderr)
        print("\n".join(f"- {item}" for item in findings), file=sys.stderr)
        return 1
    print("Privacy scan passed: no forbidden local artifacts or known secret patterns.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
