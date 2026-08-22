#!/usr/bin/env python3
"""Verify every file named by the RC bundle before privileged use."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any


SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ROOT_KEYS = {
    "schema_version",
    "candidate_sha",
    "wheel",
    "wheelhouse",
    "generated_artifacts",
    "toolchain",
    "source_repositories",
}


def _entry(raw: Any, *, root: Path, label: str) -> str:
    if not isinstance(raw, dict) or set(raw) != {"path", "sha256"}:
        raise ValueError(f"{label} must contain only path and sha256")
    relative = raw["path"]
    expected = raw["sha256"]
    if not isinstance(relative, str) or not relative:
        raise ValueError(f"{label}.path must be a non-empty string")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\x00" in relative:
        raise ValueError(f"{label}.path is unsafe")
    if not isinstance(expected, str) or SHA256.fullmatch(expected) is None:
        raise ValueError(f"{label}.sha256 is invalid")
    path = root / pure
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label}.path is not a regular file")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(f"{label}.sha256 does not match {relative}")
    return relative


def validate_bundle(
    bundle: Path,
    *,
    candidate_sha: str,
    wheel_sha256: str,
) -> None:
    if bundle.is_symlink() or not bundle.is_file():
        raise ValueError("bundle must be a regular file")
    root = bundle.parent
    payload = json.loads(bundle.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != ROOT_KEYS:
        raise ValueError("bundle has missing or unknown root fields")
    if payload["schema_version"] != 1 or isinstance(payload["schema_version"], bool):
        raise ValueError("bundle schema_version must be 1")
    if payload["candidate_sha"] != candidate_sha or SHA40.fullmatch(candidate_sha) is None:
        raise ValueError("bundle candidate_sha does not match")
    wheel_path = _entry(payload["wheel"], root=root, label="wheel")
    if payload["wheel"]["sha256"] != wheel_sha256:
        raise ValueError("bundle wheel sha256 does not match the selected candidate")
    if not wheel_path.startswith("dist/"):
        raise ValueError("bundle wheel must be under dist/")

    wheelhouse = payload["wheelhouse"]
    if not isinstance(wheelhouse, list) or not wheelhouse:
        raise ValueError("bundle wheelhouse must be a non-empty array")
    if any(
        not isinstance(raw, dict)
        or not isinstance(raw.get("path"), str)
        or not raw["path"].endswith(".whl")
        for raw in wheelhouse
    ):
        raise ValueError("bundle wheelhouse must contain wheels only")
    declared_wheelhouse = {
        _entry(raw, root=root, label=f"wheelhouse[{index}]")
        for index, raw in enumerate(wheelhouse)
    }
    wheelhouse_root = root / "wheelhouse"
    actual_wheelhouse: set[str] = set()
    for path in wheelhouse_root.iterdir():
        if path.is_symlink() or not path.is_file():
            raise ValueError("wheelhouse contains a symlink or non-file entry")
        actual_wheelhouse.add(path.relative_to(root).as_posix())
    if declared_wheelhouse != actual_wheelhouse:
        raise ValueError("wheelhouse inventory is incomplete or contains an undeclared file")

    generated = payload["generated_artifacts"]
    if not isinstance(generated, list):
        raise ValueError("generated_artifacts must be an array")
    generated_paths = {
        _entry(raw, root=root, label=f"generated_artifacts[{index}]")
        for index, raw in enumerate(generated)
    }
    if len(generated_paths) != len(generated):
        raise ValueError("bundle contains duplicate generated artifact paths")

    tools = payload["toolchain"]
    if not isinstance(tools, list) or not tools:
        raise ValueError("toolchain must be a non-empty array")
    tool_names: set[str] = set()
    tool_paths: set[str] = set()
    for index, raw in enumerate(tools):
        if not isinstance(raw, dict):
            raise ValueError(f"toolchain[{index}] must be an object")
        required = {"name", "version", "shape", "path", "sha256"}
        if raw.get("shape") == "tree":
            required |= {"entrypoint", "installed_sha256"}
        if set(raw) != required:
            raise ValueError(f"toolchain[{index}] has missing or unknown fields")
        name = raw.get("name")
        version = raw.get("version")
        if (
            not isinstance(name, str)
            or not name
            or "/" in name
            or name in tool_names
            or not isinstance(version, str)
            or not version
            or raw.get("shape") not in {"file", "tree"}
        ):
            raise ValueError(f"toolchain[{index}] identity is invalid")
        if raw.get("shape") == "tree":
            entrypoint = raw.get("entrypoint")
            pure_entrypoint = PurePosixPath(str(entrypoint))
            if (
                not isinstance(entrypoint, str)
                or pure_entrypoint.is_absolute()
                or ".." in pure_entrypoint.parts
                or not isinstance(raw.get("installed_sha256"), str)
                or SHA256.fullmatch(raw["installed_sha256"]) is None
            ):
                raise ValueError(f"toolchain[{index}] tree metadata is invalid")
        tool_names.add(name)
        tool_paths.add(
            _entry(
                {"path": raw["path"], "sha256": raw["sha256"]},
                root=root,
                label=f"toolchain[{index}]",
            )
        )
    if tool_names != {"codex", "claude", "copilot", "agy", "srt", "openspec"}:
        raise ValueError("toolchain inventory is incomplete")
    actual_tool_paths = {
        path.relative_to(root).as_posix()
        for path in (root / "toolchain").iterdir()
        if path.is_file() and not path.is_symlink()
    }
    if tool_paths != actual_tool_paths:
        raise ValueError("toolchain directory has undeclared or missing artifacts")

    repositories = payload["source_repositories"]
    if not isinstance(repositories, list) or not repositories:
        raise ValueError("source_repositories must be a non-empty array")
    repository_paths: set[str] = set()
    slugs: set[str] = set()
    for index, raw in enumerate(repositories):
        if not isinstance(raw, dict) or set(raw) != {
            "slug", "commit", "remote", "path", "sha256"
        }:
            raise ValueError(f"source_repositories[{index}] fields are invalid")
        slug = raw.get("slug")
        if (
            not isinstance(slug, str)
            or not slug
            or "/" in slug
            or slug in slugs
            or not isinstance(raw.get("commit"), str)
            or SHA40.fullmatch(raw["commit"]) is None
            or not isinstance(raw.get("remote"), str)
            or not raw["remote"].startswith("https://")
        ):
            raise ValueError(f"source_repositories[{index}] identity is invalid")
        slugs.add(slug)
        repository_paths.add(
            _entry(
                {"path": raw["path"], "sha256": raw["sha256"]},
                root=root,
                label=f"source_repositories[{index}]",
            )
        )
    actual_repository_paths = {
        path.relative_to(root).as_posix()
        for path in (root / "source").iterdir()
        if path.is_file() and not path.is_symlink()
    }
    if repository_paths != actual_repository_paths:
        raise ValueError("source directory has undeclared or missing artifacts")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    args = parser.parse_args()
    try:
        validate_bundle(
            args.bundle,
            candidate_sha=args.candidate_sha,
            wheel_sha256=args.wheel_sha256,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"bundle validation failed: {exc}", file=sys.stderr)
        return 1
    print("bundle inventory and hashes are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
