from __future__ import annotations

import stat
from pathlib import Path
from datetime import datetime, timezone

import pytest

from paulsha_cortex.trust_root.install import backend, cli, legacy_purge
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
