#!/usr/bin/env python3
"""Build a deterministic, hash-complete RC bundle manifest from staged inputs."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import stat
import tarfile
from pathlib import Path, PurePosixPath


SHA40 = re.compile(r"^[0-9a-f]{40}$")


def _relative_symlink_stays_inside(relative: PurePosixPath, target: PurePosixPath) -> bool:
    if target.is_absolute():
        return False
    depth = 0
    for component in (*relative.parent.parts, *target.parts):
        if component in {"", "."}:
            continue
        if component == "..":
            if depth == 0:
                return False
            depth -= 1
        else:
            depth += 1
    return True


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda row: row.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        observed = path.lstat()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(format(stat.S_IMODE(observed.st_mode), "04o").encode("ascii") + b"\0")
        if stat.S_ISLNK(observed.st_mode):
            target = os.readlink(path)
            pure_target = PurePosixPath(target)
            if not _relative_symlink_stays_inside(PurePosixPath(relative), pure_target):
                raise ValueError(f"tree symlink escapes its root: {relative} -> {target}")
            digest.update(b"L\0" + target.encode("utf-8") + b"\0")
        elif stat.S_ISREG(observed.st_mode):
            if observed.st_nlink != 1:
                raise ValueError(f"tree contains a hard-linked file: {relative}")
            digest.update(b"F\0" + sha256_file(path).encode("ascii") + b"\0")
        elif stat.S_ISDIR(observed.st_mode):
            digest.update(b"D\0")
        else:
            raise ValueError(f"tree contains an unsupported object: {relative}")
    return digest.hexdigest()


def _regular(path: Path, *, label: str) -> Path:
    observed = path.lstat()
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise ValueError(f"{label} must be a single-link regular file")
    return path.resolve(strict=True)


def _inside(root: Path, path: Path, *, label: str) -> Path:
    absolute = path.absolute()
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the candidate root") from exc
    cursor = root
    for component in relative.parts[:-1]:
        cursor /= component
        if cursor.is_symlink():
            raise ValueError(f"{label} has a symlink ancestor: {cursor}")
    resolved = _regular(absolute, label=label)
    resolved.relative_to(root)
    return resolved


def _entry(root: Path, path: Path) -> dict[str, str]:
    resolved = _inside(root, path, label=str(path))
    return {
        "path": resolved.relative_to(root).as_posix(),
        "sha256": sha256_file(resolved),
    }


def _parse(raw: str, count: int, *, label: str) -> list[str]:
    values = raw.split(",", count - 1)
    if len(values) != count or any(not value for value in values):
        raise ValueError(f"{label} must have {count} comma-separated values")
    return values


def _write_tree_archive(source: Path, destination: Path) -> str:
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"tool tree must be a real directory: {source}")
    expected = tree_sha256(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as raw_stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_stream, mtime=0) as compressed:
            with tarfile.open(
                fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
            ) as archive:
                for path in sorted(
                    source.rglob("*"),
                    key=lambda row: row.relative_to(source).as_posix(),
                ):
                    relative = path.relative_to(source).as_posix()
                    info = archive.gettarinfo(str(path), arcname=relative)
                    info.uid = 0
                    info.gid = 0
                    info.uname = "root"
                    info.gname = "root"
                    info.mtime = 0
                    if info.isfile():
                        with path.open("rb") as stream:
                            archive.addfile(info, stream)
                    else:
                        archive.addfile(info)
    return expected


def build(args: argparse.Namespace) -> dict[str, object]:
    root = args.root.resolve(strict=True)
    if SHA40.fullmatch(args.candidate_sha) is None:
        raise ValueError("candidate SHA must be 40 lowercase hex characters")
    wheel = _inside(root, args.wheel, label="candidate wheel")
    wheelhouse_dir = root / "wheelhouse"
    wheelhouse = sorted(wheelhouse_dir.iterdir())
    if not wheelhouse or any(path.suffix != ".whl" for path in wheelhouse):
        raise ValueError("wheelhouse must be a non-empty wheel-only directory")

    tools: list[dict[str, str]] = []
    names: set[str] = set()
    for raw in args.tool_file:
        name, version, raw_path = _parse(raw, 3, label="--tool-file")
        if name in names or "/" in name:
            raise ValueError(f"invalid or duplicate tool name: {name}")
        names.add(name)
        tools.append(
            {
                "name": name,
                "version": version,
                "shape": "file",
                **_entry(root, Path(raw_path)),
            }
        )
    for raw in args.tool_tree:
        name, version, raw_path, entrypoint = _parse(raw, 4, label="--tool-tree")
        if name in names or "/" in name:
            raise ValueError(f"invalid or duplicate tool name: {name}")
        names.add(name)
        relative_entrypoint = PurePosixPath(entrypoint)
        if relative_entrypoint.is_absolute() or ".." in relative_entrypoint.parts:
            raise ValueError(f"unsafe tool entrypoint: {name}")
        source = Path(raw_path).resolve(strict=True)
        if not (source / entrypoint).is_file():
            raise ValueError(f"tool entrypoint is missing: {name}/{entrypoint}")
        archive = root / "toolchain" / f"{name}.tar.gz"
        installed_sha = _write_tree_archive(source, archive)
        tools.append(
            {
                "name": name,
                "version": version,
                "shape": "tree",
                "entrypoint": entrypoint,
                "installed_sha256": installed_sha,
                **_entry(root, archive),
            }
        )
    if names != {"codex", "claude", "copilot", "agy", "srt", "openspec"}:
        raise ValueError("toolchain must contain exactly codex, claude, copilot, agy, srt, openspec")

    repositories: list[dict[str, str]] = []
    for raw in args.repository:
        slug, commit, remote, raw_path = _parse(raw, 4, label="--repository")
        if "/" in slug or SHA40.fullmatch(commit) is None or not remote.startswith("https://"):
            raise ValueError(f"invalid repository identity: {slug}")
        repositories.append(
            {
                "slug": slug,
                "commit": commit,
                "remote": remote,
                **_entry(root, Path(raw_path)),
            }
        )

    return {
        "schema_version": 1,
        "candidate_sha": args.candidate_sha,
        "wheel": _entry(root, wheel),
        "wheelhouse": [_entry(root, path) for path in wheelhouse],
        "generated_artifacts": [],
        "toolchain": sorted(tools, key=lambda row: row["name"]),
        "source_repositories": sorted(repositories, key=lambda row: row["slug"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--tool-file", action="append", default=[])
    parser.add_argument("--tool-tree", action="append", default=[])
    parser.add_argument("--repository", action="append", default=[])
    args = parser.parse_args()
    try:
        payload = build(args)
        output = args.root.resolve(strict=True) / "bundle.json"
        output.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError, tarfile.TarError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
