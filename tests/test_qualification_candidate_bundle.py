"""Hash-complete qualification candidate and typed artifact installation tests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.trust_root.install.backend import LocalInstallBackend, _tree_sha256
from paulsha_cortex.trust_root.install.core import _desired_digest
from qualification.prepare_candidate import _inside, _write_tree_archive, build
from qualification.verify_bundle import validate_bundle


def _identity() -> tuple[str, str]:
    import grp
    import pwd

    return pwd.getpwuid(os.getuid()).pw_name, grp.getgrgid(os.getgid()).gr_name


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*argv: str, cwd: Path) -> str:
    result = subprocess.run(
        ("git", *argv), cwd=cwd, check=True, text=True, capture_output=True
    )
    return result.stdout.strip()


def test_tree_archive_is_byte_reproducible_and_matches_installed_tree_hash(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "lib").mkdir(parents=True)
    (source / "lib").chmod(0o755)
    entrypoint = source / "lib/cli.js"
    entrypoint.write_text("#!/usr/bin/node\n", encoding="utf-8")
    entrypoint.chmod(0o755)
    (source / "current").symlink_to("lib")
    (source / "bin").mkdir()
    (source / "bin").chmod(0o755)
    (source / "bin/cli").symlink_to("../lib/cli.js")

    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    expected = _write_tree_archive(source, first)
    assert _write_tree_archive(source, second) == expected
    assert first.read_bytes() == second.read_bytes()

    owner, group = _identity()
    installed = tmp_path / "installed"
    step = {
        "step_id": "toolchain:srt",
        "kind": "toolchain",
        "name": "srt",
        "shape": "tree",
        "entrypoint": "lib/cli.js",
        "source": str(first),
        "source_sha256": _sha256(first),
        "path": str(installed),
        "owner": owner,
        "group": group,
        "mode": "0755",
        "desired_sha256": expected,
    }
    backend = LocalInstallBackend(require_root=False)
    outcome = backend.apply_step(step)

    assert outcome["installed_sha256"] == expected
    assert _tree_sha256(installed) == expected
    assert (installed / "current").readlink() == Path("lib")
    assert (installed / "bin/cli").readlink() == Path("../lib/cli.js")


def test_candidate_builder_emits_and_verifies_the_exact_six_tool_roster(
    tmp_path: Path,
) -> None:
    root = tmp_path / "candidate"
    (root / "dist").mkdir(parents=True)
    (root / "wheelhouse").mkdir()
    (root / "toolchain").mkdir()
    (root / "source").mkdir()
    wheel = root / "dist/candidate.whl"
    wheel.write_bytes(b"wheel")
    (root / "wheelhouse/candidate.whl").write_bytes(wheel.read_bytes())
    file_specs: list[str] = []
    for name in ("codex", "claude", "copilot", "agy"):
        tool = root / f"toolchain/{name}"
        tool.write_bytes(name.encode("ascii"))
        tool.chmod(0o755)
        file_specs.append(f"{name},1.0,{tool}")
    tree_specs: list[str] = []
    for name in ("srt", "openspec"):
        tree = tmp_path / f"{name}-tree"
        (tree / "bin").mkdir(parents=True)
        entrypoint = tree / "bin/cli.js"
        entrypoint.write_text("#!/usr/bin/node\n", encoding="utf-8")
        entrypoint.chmod(0o755)
        tree_specs.append(f"{name},1.0,{tree},bin/cli.js")
    source_bundle = root / "source/paulsha-cortex.bundle"
    source_bundle.write_bytes(b"source")
    candidate_sha = "a" * 40

    payload = build(
        SimpleNamespace(
            root=root,
            candidate_sha=candidate_sha,
            wheel=wheel,
            tool_file=file_specs,
            tool_tree=tree_specs,
            repository=[
                "paulsha-cortex,"
                f"{candidate_sha},https://github.com/hamanpaul/paulsha-cortex.git,"
                f"{source_bundle}"
            ],
        )
    )
    bundle = root / "bundle.json"
    bundle.write_text(json.dumps(payload), encoding="utf-8")
    (root / "install-config.yaml").write_text("{}\n", encoding="utf-8")

    assert {row["name"] for row in payload["toolchain"]} == {
        "codex",
        "claude",
        "copilot",
        "agy",
        "srt",
        "openspec",
    }
    validate_bundle(
        bundle,
        candidate_sha=candidate_sha,
        wheel_sha256=_sha256(wheel),
    )


def test_bundle_verifier_rejects_incomplete_or_hardlinked_install_input(
    tmp_path: Path,
) -> None:
    root = tmp_path / "candidate"
    for directory in ("dist", "wheelhouse", "toolchain", "source"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    bundle = root / "bundle.json"
    bundle.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="root inventory"):
        validate_bundle(
            bundle,
            candidate_sha="a" * 40,
            wheel_sha256="b" * 64,
        )

    config = root / "install-config.yaml"
    config.write_text("{}\n", encoding="utf-8")
    os.link(config, tmp_path / "install-config-copy.yaml")
    with pytest.raises(ValueError, match="single-link regular file"):
        validate_bundle(
            bundle,
            candidate_sha="a" * 40,
            wheel_sha256="b" * 64,
        )


def test_candidate_member_rejects_an_in_root_symlink_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "candidate"
    real = root / "real"
    real.mkdir(parents=True)
    (real / "tool").write_bytes(b"locked")
    (root / "alias").symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink ancestor"):
        _inside(root.resolve(), root / "alias/tool", label="tool")


def test_file_toolchain_install_rejects_source_hardlinks(tmp_path: Path) -> None:
    source = tmp_path / "codex"
    source.write_bytes(b"binary")
    os.link(source, tmp_path / "codex-copy")
    owner, group = _identity()
    step = {
        "step_id": "toolchain:codex",
        "kind": "toolchain",
        "name": "codex",
        "shape": "file",
        "source": str(source),
        "source_sha256": _sha256(source),
        "path": str(tmp_path / "installed/codex"),
        "owner": owner,
        "group": group,
        "mode": "0755",
        "desired_sha256": _sha256(source),
    }

    with pytest.raises(Exception, match="hash-bound"):
        LocalInstallBackend(require_root=False).apply_step(step)


def test_repository_bundle_installs_exact_clean_commit_and_detects_drift(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git("init", "-q", cwd=source)
    _git("config", "user.email", "qualification@example.invalid", cwd=source)
    _git("config", "user.name", "Qualification", cwd=source)
    (source / "README.md").write_text("exact source\n", encoding="utf-8")
    (source / "CURRENT.md").symlink_to("README.md")
    _git("add", "README.md", "CURRENT.md", cwd=source)
    _git("commit", "-qm", "fixture", cwd=source)
    commit = _git("rev-parse", "HEAD", cwd=source)
    bundle = tmp_path / "source.bundle"
    _git("bundle", "create", str(bundle), "HEAD", cwd=source)
    owner, group = _identity()
    installed = tmp_path / "installed"
    step = {
        "step_id": "repository:paulsha-cortex",
        "kind": "repository",
        "slug": "paulsha-cortex",
        "source": str(bundle),
        "source_sha256": _sha256(bundle),
        "commit": commit,
        "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
        "path": str(installed),
        "owner": owner,
        "group": group,
        "mode": "0755",
    }
    step["desired_sha256"] = _desired_digest(step)
    backend = LocalInstallBackend(require_root=False)

    outcome = backend.apply_step(step)
    assert outcome["installed_sha256"] == step["desired_sha256"]
    assert outcome["clean"] is True
    assert outcome["integrity"] is True
    assert outcome["tree_safe"] is True
    assert (installed / "CURRENT.md").readlink() == Path("README.md")

    (installed / "README.md").write_text("drift\n", encoding="utf-8")
    assert backend.inspect_step(step)["installed_sha256"] is None
