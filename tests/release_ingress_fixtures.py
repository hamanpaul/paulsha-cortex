"""Release-ingress fixtures: a qualification input tree and its local release source."""
from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from qualification.release_source import build_release_source

VERSION = "0.1.13"
CANDIDATE_SHA = "a" * 40
TOOL_NAMES = ("agy", "claude", "codex", "copilot", "openspec", "srt")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class InputTree:
    root: Path
    bundle: Path
    wheel_name: str
    wheel_sha256: str
    bundle_sha256: str


@dataclass(frozen=True)
class ReleaseFixture:
    source: Path
    tree: InputTree
    assets: dict[str, str]


def drop_group_other_write(root: Path) -> None:
    """Clear group/other write like a umask-022 host; the suite may run under 0002."""

    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(stat.S_IMODE(path.lstat().st_mode) & ~0o022)


def write_input_tree(
    root: Path, *, version: str = VERSION, candidate_sha: str = CANDIDATE_SHA
) -> InputTree:
    root.mkdir(parents=True)
    for directory in ("dist", "wheelhouse", "toolchain", "source"):
        (root / directory).mkdir()
    wheel_name = f"paulsha_cortex-{version}-py3-none-any.whl"
    wheel_bytes = f"candidate wheel {version}\n".encode()
    (root / "dist" / wheel_name).write_bytes(wheel_bytes)
    (root / "wheelhouse" / wheel_name).write_bytes(wheel_bytes)
    (root / "wheelhouse" / "PyYAML-6.0.2-py3-none-any.whl").write_bytes(b"pyyaml wheel\n")
    for name in TOOL_NAMES:
        tool = root / "toolchain" / name
        tool.write_bytes(f"{name} tool\n".encode())
        tool.chmod(0o755)
    (root / "source" / "paulsha-cortex.bundle").write_bytes(b"git bundle\n")
    (root / "install-config.yaml").write_text("schema_version: 1\n", encoding="utf-8")

    def row(relative: str) -> dict[str, str]:
        return {"path": relative, "sha256": _sha256(root / relative)}

    bundle = {
        "schema_version": 1,
        "candidate_sha": candidate_sha,
        "wheel": row(f"dist/{wheel_name}"),
        "wheelhouse": [
            row(f"wheelhouse/{wheel_name}"),
            row("wheelhouse/PyYAML-6.0.2-py3-none-any.whl"),
        ],
        "generated_artifacts": [],
        "toolchain": [
            {"name": name, "version": "1.0.0", "shape": "file", **row(f"toolchain/{name}")}
            for name in TOOL_NAMES
        ],
        "source_repositories": [
            {
                "slug": "paulsha-cortex",
                "commit": candidate_sha,
                "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
                **row("source/paulsha-cortex.bundle"),
            }
        ],
    }
    (root / "bundle.json").write_text(json.dumps(bundle, sort_keys=True), encoding="utf-8")
    drop_group_other_write(root)
    return InputTree(
        root=root,
        bundle=root / "bundle.json",
        wheel_name=wheel_name,
        wheel_sha256=hashlib.sha256(wheel_bytes).hexdigest(),
        bundle_sha256=_sha256(root / "bundle.json"),
    )


def write_release_source(tmp_path: Path, *, version: str = VERSION) -> ReleaseFixture:
    tree = write_input_tree(tmp_path / "artifacts", version=version)
    source = tmp_path / "release-source"
    assets = build_release_source(
        artifacts=tree.root,
        output=source,
        version=version,
        candidate_sha=CANDIDATE_SHA,
        wheel_sha256=tree.wheel_sha256,
        bundle_sha256=tree.bundle_sha256,
    )
    return ReleaseFixture(source=source, tree=tree, assets=assets)


def release_json_path(source: Path, version: str = VERSION) -> Path:
    return source / "api/repos/hamanpaul/paulsha-cortex/releases/tags" / f"v{version}.json"


def refresh_asset_metadata(source: Path, name: str, *, version: str = VERSION) -> None:
    release_path = release_json_path(source, version)
    document = json.loads(release_path.read_text(encoding="utf-8"))
    asset = source / "assets" / name
    for row in document["assets"]:
        if row["name"] == name:
            row["digest"] = f"sha256:{_sha256(asset)}"
            row["size"] = asset.stat().st_size
    release_path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")


def rewrite_manifest(release: ReleaseFixture, *, bundle_sha256: str) -> None:
    name = f"paulsha-cortex-{VERSION}-qualification.json"
    path = release.source / "assets" / name
    document = json.loads(path.read_text(encoding="utf-8"))
    document["bundle"]["sha256"] = bundle_sha256
    path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    refresh_asset_metadata(release.source, name)


def fake_venv_runner(
    calls: list[tuple[tuple[str, ...], dict[str, str]]],
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Stand in for `python3 -m venv --copies` and the offline pip install."""

    def run(argv, *, check=False, env=None, **_kwargs):
        argv = tuple(argv)
        calls.append((argv, dict(env or {})))
        if argv[:5] == ("/usr/bin/python3", "-I", "-S", "-m", "venv"):
            venv = Path(argv[-1])
            (venv / "bin").mkdir(parents=True)
            (venv / "lib").mkdir()
            python = venv / "bin" / "python"
            python.write_bytes(b"#!/bin/sh\n")
            python.chmod(0o755)
            (venv / "lib64").symlink_to("lib")
        elif "pip" in argv:
            venv = Path(argv[0]).parent.parent
            cli = venv / "bin" / "cortex"
            cli.write_text("#!/bin/sh\n", encoding="utf-8")
            cli.chmod(0o755)
            (venv / "lib" / "site.py").write_text("# site\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run
