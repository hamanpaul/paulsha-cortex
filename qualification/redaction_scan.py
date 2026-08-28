#!/usr/bin/env python3
"""Fail if upload candidates contain protected credential material."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


REQUIRED_SECRET_ENV = (
    "CORTEX_RC_CODEX_AUTH",
    "CORTEX_RC_AGY_AUTH",
    "CORTEX_RC_COPILOT_AUTH",
    "CORTEX_RC_MANAGER_GITHUB_AUTH",
)
OPTIONAL_SECRET_ENV = ("CORTEX_RC_BUILDER_AGY_AUTH",)
# Keep one exported inventory for callers/tests while distinguishing the
# optional host-overlay credential from the four default canary inputs.
SECRET_ENV = REQUIRED_SECRET_ENV + OPTIONAL_SECRET_ENV
TOKEN_PATTERNS = (
    re.compile(rb"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._~+/-]{16,}"),
)


def _needles(profile: str) -> tuple[bytes, ...]:
    if profile == "release":
        return ()
    if profile != "deployment-canary":
        raise ValueError("profile must be release or deployment-canary")
    values: set[bytes] = set()
    for name in REQUIRED_SECRET_ENV:
        raw = os.environ.get(name)
        if not raw:
            raise ValueError(f"protected secret {name} is unavailable")
        encoded = raw.encode()
        if len(encoded) < 8:
            raise ValueError(f"protected secret {name} is unexpectedly short")
        values.add(encoded)
        for line in encoded.splitlines():
            stripped = line.strip()
            if len(stripped) >= 8:
                values.add(stripped)
    for name in OPTIONAL_SECRET_ENV:
        raw = os.environ.get(name)
        if not raw:
            continue
        encoded = raw.encode()
        if len(encoded) < 8:
            raise ValueError(f"protected secret {name} is unexpectedly short")
        values.add(encoded)
        for line in encoded.splitlines():
            stripped = line.strip()
            if len(stripped) >= 8:
                values.add(stripped)
    return tuple(sorted(values, key=len, reverse=True))


def scan(root: Path, *, profile: str) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("upload root must be a regular directory")
    needles = _needles(profile)
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"upload candidate contains a symlink: {path}")
        if not path.is_file():
            continue
        content = path.read_bytes()
        if any(needle in content for needle in needles) or any(
            pattern.search(content) for pattern in TOKEN_PATTERNS
        ):
            raise ValueError(f"credential-like material detected in {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument(
        "--profile", required=True, choices=("release", "deployment-canary")
    )
    args = parser.parse_args()
    try:
        scan(args.root, profile=args.profile)
    except (OSError, ValueError) as exc:
        print(f"redaction scan failed: {exc}", file=sys.stderr)
        return 1
    print("upload candidates passed credential redaction scan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
