from __future__ import annotations

import os
import stat
from pathlib import Path
from datetime import datetime, timezone

import pytest

from paulsha_cortex.trust_root.install import backend, cli, core, legacy_purge
from paulsha_cortex.trust_root.install.core import (
    InstallDriftError,
    InstallReceipt,
    plan_sha256,
)


@pytest.fixture(autouse=True)
def _relax_quarantine_ancestor_owner(monkeypatch) -> None:
    monkeypatch.setattr(backend, "_validate_quarantine_ancestor", lambda *_args: None)
    monkeypatch.setattr(
        backend,
        "_validate_quarantine_directory",
        lambda observed, path: None
        if stat.S_ISDIR(observed.st_mode)
        and stat.S_IMODE(observed.st_mode) == 0o700
        else (_ for _ in ()).throw(InstallDriftError(f"not a private directory: {path}")),
    )


def test_legacy_purge_cli_binds_confirmation_to_report_digest() -> None:
    parser = cli._build_parser()

    dry_run = parser.parse_args(
        [
            "legacy",
            "purge",
            "--receipt",
            "/receipts/adoption.json",
            "--receipts-dir",
            "/receipts",
            "--report",
            "/tmp/purge-report.json",
        ]
    )
    assert dry_run.legacy_command == "purge"
    assert dry_run.confirm_sha256 is None

    confirmed = parser.parse_args(
        [
            "legacy",
            "purge",
            "--receipt",
            "/receipts/adoption.json",
            "--receipts-dir",
            "/receipts",
            "--report",
            "/tmp/purge-report.json",
            "--confirm-sha256",
            "a" * 64,
        ]
    )
    assert confirmed.confirm_sha256 == "a" * 64


def test_legacy_purge_confirmation_rejects_missing_report() -> None:
    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "legacy",
                "purge",
                "--receipt",
                "/receipts/adoption.json",
                "--receipts-dir",
                "/receipts",
                "--confirm-sha256",
                "a" * 64,
            ]
        )


def _receipt_documents(tmp_path: Path, *, successor_at: datetime | None):
    adoption_path = tmp_path / "adoption.json"
    successor_path = tmp_path / "successor.json"
    successor_path.write_text("fixture")
    adoption_plan = {
        "legacy_adoption": {
            "inventory_sha256": "a" * 64,
            "host_binding_sha256": "b" * 64,
        },
        "scheme": "test",
        "instance": "test",
        "roots": {"state": str(tmp_path / "state")},
        "repo_identity": {"remote": "repo", "commit": "old"},
        "candidate": {"revision": "old"},
        "apply_order": [],
    }
    adoption_plan_sha = plan_sha256(adoption_plan)
    successor_plan = {
        "scheme": "test",
        "instance": "test",
        "roots": {"state": str(tmp_path / "state")},
        "repo_identity": {"remote": "repo", "commit": "new"},
        "candidate": {"revision": "new"},
        "apply_order": [],
    }
    adoption = {
        "receipt_id": "adoption-id",
        "plan_sha256": adoption_plan_sha,
        "state": "applied",
        "qualified": True,
        "plan": adoption_plan,
        "legacy_adoption": {
            "inventory_sha256": "a" * 64,
            "apply_inventory_sha256": "a" * 64,
            "host_binding_sha256": "b" * 64,
            "quarantine_root": str(tmp_path / "quarantine"),
        },
        "journal": [
            {
                "step_id": "q-one",
                "status": "completed",
                "step": {
                    "kind": "legacy-quarantine",
                    "destination": str(tmp_path / "quarantine" / "one"),
                },
                "quarantine_identity": {
                    "device": 1,
                    "inode": 2,
                    "type": "directory",
                    "tree_sha256": "b" * 64,
                },
            }
        ],
    }
    successor = {
        "receipt_id": "successor-id",
        "plan_sha256": plan_sha256(successor_plan),
        "plan": successor_plan,
        "state": "applied",
        "qualified": successor_at is not None,
        "qualified_at": successor_at.isoformat().replace("+00:00", "Z")
        if successor_at
        else None,
        "parent_receipt": {
            "path": str(adoption_path),
            "receipt_id": "adoption-id",
            "plan_sha256": adoption_plan_sha,
        },
    }
    return adoption_path, adoption, successor_path, successor


def _report(monkeypatch, tmp_path, *, successor_at, actual_identity=None):
    adoption_path, adoption, successor_path, successor = _receipt_documents(
        tmp_path, successor_at=successor_at
    )
    documents = {adoption_path: adoption, successor_path: successor}
    monkeypatch.setattr(legacy_purge, "_read_receipt", lambda p: documents[p])
    if actual_identity is not None:
        monkeypatch.setattr(
            legacy_purge.backend,
            "_legacy_quarantine_identity",
            lambda _path: actual_identity,
        )
    return legacy_purge.build_legacy_purge_report(
        adoption_path,
        now=datetime(2026, 9, 29, tzinfo=timezone.utc),
    )


def test_purge_retains_without_qualified_successor_in_receipt_chain(
    monkeypatch, tmp_path
) -> None:
    report = _report(monkeypatch, tmp_path, successor_at=None)
    assert report["entries"][0]["action"] == "retain"
    assert report["entries"][0]["reason"] == "no-qualified-successor-in-receipt-chain"


def test_purge_retains_until_qualified_successor_is_30_days_old(
    monkeypatch, tmp_path
) -> None:
    report = _report(
        monkeypatch,
        tmp_path,
        successor_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
        actual_identity={
            "device": 1,
            "inode": 2,
            "type": "directory",
            "tree_sha256": "b" * 64,
        },
    )
    assert report["entries"][0]["action"] == "retain"
    assert report["entries"][0]["reason"] == "qualified-successor-retention-under-30-days"


def test_purge_reports_drift_instead_of_deleting(monkeypatch, tmp_path) -> None:
    report = _report(
        monkeypatch,
        tmp_path,
        successor_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        actual_identity={
            "device": 1,
            "inode": 99,
            "type": "directory",
            "tree_sha256": "d" * 64,
        },
    )
    assert report["entries"][0]["action"] == "retain"
    assert report["entries"][0]["reason"] == "quarantine-drift"


def test_purge_selects_only_matching_receipt_bound_objects(monkeypatch, tmp_path) -> None:
    identity = {
        "device": 1,
        "inode": 2,
        "type": "directory",
        "tree_sha256": "b" * 64,
    }
    report = _report(
        monkeypatch,
        tmp_path,
        successor_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        actual_identity=identity,
    )
    assert report["entries"][0]["action"] == "delete"
    assert report["entries"][0]["identity"] == identity


def _quarantine_tree(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    root = tmp_path / "quarantine"
    root.mkdir(mode=0o700)
    target = root / "entry"
    target.mkdir()
    (target / "data").write_text("legacy payload")
    identity = backend._legacy_quarantine_identity(target)
    return root, target, identity


def test_secure_purge_deletes_exact_staged_tree(tmp_path) -> None:
    root, target, identity = _quarantine_tree(tmp_path)
    backend._discard_legacy_quarantine(
        target, identity, quarantine_root=root, key="receipt:step"
    )
    assert not target.exists()


def test_secure_purge_records_and_deletes_stale_sockets_and_fifos(tmp_path) -> None:
    # #1282: a quarantined tree may hold a stale UNIX socket or a FIFO; they
    # are bound by type and mode (never opened) and purge removes them.
    root, target, _identity = _quarantine_tree(tmp_path)
    os.mknod(target / "agent.sock", 0o600 | stat.S_IFSOCK)
    os.mkfifo(target / "events.fifo", 0o640)
    identity = backend._legacy_quarantine_identity(target)
    assert identity["type"] == "directory"
    # The digest changes with the special member's type and mode.
    os.chmod(target / "events.fifo", 0o600)
    assert backend._legacy_quarantine_identity(target) != identity
    os.chmod(target / "events.fifo", 0o640)
    assert backend._legacy_quarantine_identity(target) == identity

    backend._discard_legacy_quarantine(
        target, identity, quarantine_root=root, key="receipt:specials"
    )
    assert not target.exists()


@pytest.mark.parametrize("kind", ["socket", "fifo"])
def test_secure_purge_deletes_a_quarantined_socket_or_fifo(tmp_path, kind: str) -> None:
    root = tmp_path / "quarantine"
    root.mkdir(mode=0o700)
    target = root / "entry"
    if kind == "socket":
        os.mknod(target, 0o600 | stat.S_IFSOCK)
    else:
        os.mkfifo(target, 0o600)
    identity = backend._legacy_quarantine_identity(target)
    assert identity["type"] == kind
    assert core._valid_quarantine_identity(identity)

    backend._discard_legacy_quarantine(target, identity, quarantine_root=root, key=f"k:{kind}")
    assert not os.path.lexists(target)


def test_secure_purge_keeps_tree_when_staging_is_cross_filesystem(
    monkeypatch, tmp_path
) -> None:
    root, target, identity = _quarantine_tree(tmp_path)
    monkeypatch.setattr(backend, "_filesystem_device", lambda _fd: -123)
    with pytest.raises(InstallDriftError, match="another filesystem"):
        backend._discard_legacy_quarantine(
            target, identity, quarantine_root=root, key="cross-fs"
        )
    assert target.is_dir()
    assert (target / "data").read_text() == "legacy payload"


def test_secure_purge_restores_tree_when_mount_boundary_is_detected(
    monkeypatch, tmp_path
) -> None:
    root, target, identity = _quarantine_tree(tmp_path)
    monkeypatch.setattr(
        backend,
        "_tree_digest_at",
        lambda *_args: (_ for _ in ()).throw(InstallDriftError("mount point detected")),
    )
    with pytest.raises(InstallDriftError, match="mount point detected"):
        backend._discard_legacy_quarantine(
            target, identity, quarantine_root=root, key="mounted-tree"
        )
    assert target.is_dir()
    assert (target / "data").read_text() == "legacy payload"


def test_qualified_receipt_without_adoption_parent_link_is_not_a_successor(
    monkeypatch, tmp_path
) -> None:
    adoption_path, adoption, successor_path, successor = _receipt_documents(
        tmp_path, successor_at=datetime(2026, 8, 1, tzinfo=timezone.utc)
    )
    successor["parent_receipt"]["receipt_id"] = "different-adoption"
    monkeypatch.setattr(
        legacy_purge,
        "_read_receipt",
        lambda path: adoption if path == adoption_path else successor,
    )
    report = legacy_purge.build_legacy_purge_report(
        adoption_path, now=datetime(2026, 9, 29, tzinfo=timezone.utc)
    )
    assert report["entries"][0]["action"] == "retain"
    assert report["entries"][0]["reason"] == "no-qualified-successor-in-receipt-chain"


def test_execution_deletes_only_report_items_and_records_receipt(monkeypatch) -> None:
    document = {
        "receipt_id": "adoption-id",
        "plan_sha256": "a" * 64,
        "plan": {},
        "legacy_adoption": {"quarantine_root": "/quarantine"},
        "legacy_purge_journal": [],
    }
    receipt = InstallReceipt(document)
    deleted: list[Path] = []
    monkeypatch.setattr(
        legacy_purge.backend,
        "_discard_legacy_quarantine",
        lambda path, *_args, **_kwargs: deleted.append(path),
    )
    results = legacy_purge.purge_legacy_entries(
        receipt,
        {
            "receipt_id": "adoption-id",
            "plan_sha256": "a" * 64,
            "report_sha256": "d" * 64,
            "entries": [
                {
                    "step_id": "q-one",
                    "path": "/quarantine/one",
                    "identity": {"device": 1},
                    "action": "delete",
                    "qualified_at": "2026-08-30T00:00:00Z",
                },
                {"step_id": "q-two", "path": "/quarantine/two", "action": "retain"},
            ],
        },
    )
    assert deleted == [Path("/quarantine/one")]
    assert results[0]["status"] == "deleted"
    assert receipt.to_dict()["legacy_purge_journal"][0]["report_sha256"] == "d" * 64


def _crash_after_staging(root: Path, target: Path, key: str) -> Path:
    """重現 purge 在物件搬進私有 staging 之後、刪除之前 crash 的狀態。"""

    staging = backend._discard_staging_path(root, key)
    staging.mkdir(parents=True, mode=0o700)
    (root / ".discard").chmod(0o700)
    os.rename(target, staging / "tree")
    return staging


def test_resume_finishes_a_purge_that_crashed_after_staging(tmp_path) -> None:
    root, target, identity = _quarantine_tree(tmp_path)
    staging = _crash_after_staging(root, target, "receipt:step")

    assert backend._resume_legacy_discard(
        target, identity, quarantine_root=root, key="receipt:step"
    ) is True
    assert not target.exists()
    assert not (staging / "tree").exists()
    # 再跑一次：沒有 staged 物件可接續。
    assert backend._resume_legacy_discard(
        target, identity, quarantine_root=root, key="receipt:step"
    ) is False


def test_resume_restores_a_staged_tree_that_drifted(tmp_path) -> None:
    root, target, identity = _quarantine_tree(tmp_path)
    staging = _crash_after_staging(root, target, "receipt:step")
    (staging / "tree" / "data").write_text("changed while staged")

    with pytest.raises(InstallDriftError):
        backend._resume_legacy_discard(
            target, identity, quarantine_root=root, key="receipt:step"
        )
    assert (target / "data").read_text() == "changed while staged"
    assert not (staging / "tree").exists()


def _pending_report(monkeypatch, tmp_path, *, staged: bool):
    adoption_path, adoption, successor_path, successor = _receipt_documents(
        tmp_path, successor_at=datetime(2026, 8, 1, tzinfo=timezone.utc)
    )
    adoption["legacy_purge_journal"] = [
        {"step_id": "q-one", "path": str(tmp_path / "quarantine" / "one"), "status": "pending"}
    ]
    if staged:
        staging = backend._discard_staging_path(
            tmp_path / "quarantine", "legacy-purge:adoption-id:q-one"
        )
        (staging / "tree").mkdir(parents=True)
    documents = {adoption_path: adoption, successor_path: successor}
    monkeypatch.setattr(legacy_purge, "_read_receipt", lambda p: documents[p])
    return legacy_purge.build_legacy_purge_report(
        adoption_path, now=datetime(2026, 9, 29, tzinfo=timezone.utc)
    )


def test_report_resumes_a_pending_purge_whose_object_is_staged(monkeypatch, tmp_path) -> None:
    report = _pending_report(monkeypatch, tmp_path, staged=True)
    assert report["entries"][0]["action"] == "resume"
    assert report["entries"][0]["reason"] is None


def test_report_finalizes_a_pending_purge_whose_object_is_already_gone(
    monkeypatch, tmp_path
) -> None:
    report = _pending_report(monkeypatch, tmp_path, staged=False)
    assert report["entries"][0]["action"] == "finalize"


def test_execution_resumes_and_finalizes_pending_purges(monkeypatch, tmp_path) -> None:
    """crash 後重跑：resume 走接續刪除，finalize 只在物件確實不在時補記 deleted；
    finalize 時物件又出現則保留並列 drift。"""

    document = {
        "receipt_id": "adoption-id",
        "plan_sha256": "a" * 64,
        "plan": {},
        "legacy_adoption": {"quarantine_root": str(tmp_path / "quarantine")},
        "legacy_purge_journal": [
            {"step_id": "q-resume", "status": "pending"},
            {"step_id": "q-gone", "status": "pending"},
            {"step_id": "q-back", "status": "pending"},
        ],
    }
    receipt = InstallReceipt(document)
    resumed: list[Path] = []

    def resume(path, *_args, **_kwargs):
        resumed.append(path)
        return path.name == "resume"

    monkeypatch.setattr(legacy_purge.backend, "_resume_legacy_discard", resume)
    reappeared = tmp_path / "quarantine" / "back"
    reappeared.mkdir(parents=True)
    identity = {"device": 1, "inode": 2, "type": "directory", "tree_sha256": "b" * 64}
    results = legacy_purge.purge_legacy_entries(
        receipt,
        {
            "receipt_id": "adoption-id",
            "plan_sha256": "a" * 64,
            "report_sha256": "d" * 64,
            "entries": [
                {"step_id": "q-resume", "path": str(tmp_path / "quarantine" / "resume"), "identity": identity, "action": "resume"},
                {"step_id": "q-gone", "path": str(tmp_path / "quarantine" / "gone"), "identity": identity, "action": "finalize"},
                {"step_id": "q-back", "path": str(reappeared), "identity": identity, "action": "finalize"},
            ],
        },
    )

    statuses = {row["step_id"]: row["status"] for row in results}
    assert statuses == {"q-resume": "deleted", "q-gone": "deleted", "q-back": "retained-drift"}
    assert reappeared.is_dir()
