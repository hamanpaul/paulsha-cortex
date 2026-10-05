#!/usr/bin/env python3
"""Write a GitHub-REST-shaped local release source for RC `cortex upgrade` drills.

The release profile runs with ``--network none`` and qualifies a single
candidate, so the drill cannot fetch a published release.  This helper packs
the mounted qualification input exactly as the release workflow does (one
``qualification-input/`` prefix), writes a passed release qualification
manifest naming the candidate, and lays out the REST documents that
``cortex upgrade --release-source`` reads:

    api/repos/<owner>/<repo>/git/ref/tags/v<version>.json
    api/repos/<owner>/<repo>/git/tags/<tag object sha>.json
    api/repos/<owner>/<repo>/releases/tags/v<version>.json
    assets/<asset name>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tarfile
from pathlib import Path

REPOSITORY = "hamanpaul/paulsha-cortex"
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
SHA40 = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize(member: tarfile.TarInfo) -> tarfile.TarInfo:
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    member.mtime = 0
    return member


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )


def build_release_source(
    *,
    artifacts: Path,
    output: Path,
    version: str,
    candidate_sha: str,
    wheel_sha256: str,
    bundle_sha256: str,
    repository: str = REPOSITORY,
) -> dict[str, str]:
    if VERSION.fullmatch(version) is None:
        raise ValueError("version must be MAJOR.MINOR.PATCH")
    if SHA40.fullmatch(candidate_sha) is None:
        raise ValueError("candidate SHA must be 40 lowercase hex characters")
    for label, value in (("wheel", wheel_sha256), ("bundle", bundle_sha256)):
        if SHA256.fullmatch(value) is None:
            raise ValueError(f"{label} SHA-256 must be 64 lowercase hex characters")
    if output.exists() or output.is_symlink():
        raise ValueError(f"release source output already exists: {output}")
    wheel_name = f"paulsha_cortex-{version}-py3-none-any.whl"
    wheel = artifacts / "dist" / wheel_name
    if wheel.is_symlink() or not wheel.is_file() or _sha256(wheel) != wheel_sha256:
        raise ValueError("the artifacts do not hold the candidate wheel for this version")
    if _sha256(artifacts / "bundle.json") != bundle_sha256:
        raise ValueError("the artifacts bundle.json does not match the candidate bundle")
    assets = output / "assets"
    assets.mkdir(parents=True, mode=0o700)
    shutil.copyfile(wheel, assets / wheel_name)
    archive_name = f"paulsha-cortex-{version}-install-input.tar.gz"
    with tarfile.open(assets / archive_name, mode="w:gz") as archive:
        archive.add(str(artifacts), arcname="qualification-input", filter=_normalize)
    manifest_name = f"paulsha-cortex-{version}-qualification.json"
    _write_json(
        assets / manifest_name,
        {
            "schema_version": 2,
            "profile": "release",
            "status": "passed",
            "candidate_sha": candidate_sha,
            "wheel": {"filename": wheel_name, "sha256": wheel_sha256},
            "bundle": {"sha256": bundle_sha256},
        },
    )
    tag = f"v{version}"
    tag_object = hashlib.sha1(
        f"cortex-release-source:{repository}:{tag}".encode(), usedforsecurity=False
    ).hexdigest()
    api = output / "api" / "repos" / repository
    _write_json(
        api / "git" / "ref" / "tags" / f"{tag}.json",
        {"ref": f"refs/tags/{tag}", "object": {"type": "tag", "sha": tag_object}},
    )
    _write_json(
        api / "git" / "tags" / f"{tag_object}.json",
        {
            "tag": tag,
            "sha": tag_object,
            "message": f"{tag}\n",
            "object": {"type": "commit", "sha": candidate_sha},
        },
    )
    digests = {
        name: _sha256(assets / name) for name in (wheel_name, archive_name, manifest_name)
    }
    _write_json(
        api / "releases" / "tags" / f"{tag}.json",
        {
            "tag_name": tag,
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": name,
                    "size": (assets / name).stat().st_size,
                    "digest": f"sha256:{digest}",
                    "browser_download_url": (
                        f"https://github.com/{repository}/releases/download/{tag}/{name}"
                    ),
                }
                for name, digest in sorted(digests.items())
            ],
        },
    )
    return digests


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--bundle-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        digests = build_release_source(
            artifacts=args.artifacts,
            output=args.output,
            version=args.version,
            candidate_sha=args.candidate_sha,
            wheel_sha256=args.wheel_sha256,
            bundle_sha256=args.bundle_sha256,
        )
    except (OSError, ValueError, tarfile.TarError) as exc:
        print(f"release source failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"output": str(args.output), "assets": digests}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
