from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import posix_acl_temp_root
from posix_acl_temp_root import dir_acl_probe, file_acl_probe, pick_posix_acl_temp_root


def test_pick_posix_acl_temp_root_falls_through_failed_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(
        posix_acl_temp_root,
        "_candidate_temp_bases",
        lambda: (str(first), str(second)),
    )
    monkeypatch.setattr(posix_acl_temp_root.shutil, "which", lambda _name: "/usr/bin/tool")

    def _run(argv, **_kwargs):
        target = Path(argv[-1])
        if target.is_relative_to(first):
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(posix_acl_temp_root.subprocess, "run", _run)

    root = pick_posix_acl_temp_root(
        prefix="psc-acl-root-",
        root_mode=0o711,
        probes=(
            dir_acl_probe("-m", "u:daemon:--x"),
            file_acl_probe("-m", "u:daemon:r--,m::r--"),
        ),
        skip_reason="unused",
    )
    try:
        assert root.parent == second
        assert list(first.iterdir()) == []
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_pick_posix_acl_temp_root_skips_when_all_bases_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    only = tmp_path / "only"
    only.mkdir()
    monkeypatch.setattr(posix_acl_temp_root, "_candidate_temp_bases", lambda: (str(only),))
    monkeypatch.setattr(posix_acl_temp_root.shutil, "which", lambda _name: "/usr/bin/tool")
    monkeypatch.setattr(
        posix_acl_temp_root.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )

    with pytest.raises(pytest.skip.Exception, match="gate TMPDIR mounts may reject"):
        pick_posix_acl_temp_root(
            prefix="psc-acl-root-",
            root_mode=0o755,
            probes=(dir_acl_probe("-m", "u:daemon:rx"),),
            skip_reason=(
                "split-UID ACL integration tests require a temp root on a POSIX ACL filesystem; "
                "gate TMPDIR mounts may reject `setfacl -m ...` with `Invalid argument`"
            ),
        )


def test_pick_posix_acl_temp_root_validator_can_reject_a_base_after_acl_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(
        posix_acl_temp_root,
        "_candidate_temp_bases",
        lambda: (str(first), str(second)),
    )
    monkeypatch.setattr(posix_acl_temp_root.shutil, "which", lambda _name: "/usr/bin/tool")
    monkeypatch.setattr(
        posix_acl_temp_root.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    visited: list[Path] = []

    def _validator(root: Path) -> bool:
        visited.append(root.parent)
        (root / "validator-artifact").write_text("x", encoding="utf-8")
        return root.parent == second

    root = pick_posix_acl_temp_root(
        prefix="psc-acl-root-",
        root_mode=0o711,
        probes=(dir_acl_probe("-m", "u:daemon:--x"),),
        skip_reason="unused",
        validator=_validator,
    )
    try:
        assert root.parent == second
        assert visited == [first, second]
        assert list(first.iterdir()) == []
    finally:
        shutil.rmtree(root, ignore_errors=True)
