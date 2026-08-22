#!/usr/bin/env python3
"""Fail if upload candidates contain protected credential material."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


SECRET_ENV = (
    "CORTEX_RC_CODEX_AUTH",
    "CORTEX_RC_AGY_AUTH",
    "CORTEX_RC_COPILOT_AUTH",
    "CORTEX_RC_MANAGER_GITHUB_AUTH",
)
TOKEN_PATTERNS = (
    re.compile(rb"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._~+/-]{16,}"),
)


def _needles() -> tuple[bytes, ...]:
    values: set[bytes] = set()
    for name in SECRET_ENV:
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
    return tuple(sorted(values, key=len, reverse=True))


def scan(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("upload root must be a regular directory")
    needles = _needles()
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
    args = parser.parse_args()
    try:
        scan(args.root)
    except (OSError, ValueError) as exc:
        print(f"redaction scan failed: {exc}", file=sys.stderr)
        return 1
    print("upload candidates passed credential redaction scan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
