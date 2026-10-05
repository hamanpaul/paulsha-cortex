"""#1263：生效中的 receipt 只由 receipt chain 決定，不看檔名、mtime 或掃描順序。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install.core import (
    InstallError,
    InstallReceipt,
    new_install_receipt,
)
from paulsha_cortex.trust_root.install.receipt_chain import (
    effective_receipt,
    receipt_directory,
)


@pytest.fixture(autouse=True)
def _rootless_receipts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _o, _p: None)
    monkeypatch.setattr(install_core, "_validate_receipt_file", lambda _o, _p: None)


def _state_root(tmp_path: Path) -> Path:
    return (tmp_path / "var/lib/cortex").absolute()


def _plan(state_root: Path, *, wheel_digit: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "scheme": "four-way",
        "repo_identity": {
            "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
            "commit": "a" * 40,
        },
        "candidate": {"wheel_sha256": wheel_digit * 64, "bundle_sha256": "c" * 64},
        "accounts": [],
        "roots": {
            "deploy": str(state_root.parent.parent / "opt/cortex"),
            "state": str(state_root),
            "systemd": "/etc/systemd/system",
            "polkit": "/etc/polkit-1/rules.d",
        },
        "apply_order": [],
        "required_credentials": [],
    }


def _receipt(
    state_root: Path,
    name: str,
    *,
    wheel_digit: str,
    state: str = "applied",
    qualified: bool = True,
    parent: InstallReceipt | None = None,
) -> InstallReceipt:
    path = receipt_directory(state_root) / name
    receipt = new_install_receipt(_plan(state_root, wheel_digit=wheel_digit), path=path)
    receipt._document["state"] = state
    receipt._document["qualified"] = qualified
    if parent is not None:
        document = parent.to_dict()
        receipt._document["parent_receipt"] = {
            "path": str(parent.path),
            "receipt_id": document["receipt_id"],
            "plan_sha256": document["plan_sha256"],
        }
    receipt._persist()
    return receipt


def test_receipt_directory_follows_the_state_root_name(tmp_path: Path) -> None:
    assert receipt_directory(Path("/var/lib/cortex")) == Path(
        "/var/lib/cortex-install-receipts"
    )


def test_qualified_successor_is_the_effective_receipt(tmp_path: Path) -> None:
    state_root = _state_root(tmp_path)
    prior = _receipt(state_root, "prior.json", wheel_digit="1")
    successor = _receipt(state_root, "next.run-x.json", wheel_digit="2", parent=prior)

    assert effective_receipt(state_root).path == successor.path


def test_rolled_back_successor_returns_authority_to_the_prior_regardless_of_mtime(
    tmp_path: Path,
) -> None:
    state_root = _state_root(tmp_path)
    prior = _receipt(state_root, "prior.json", wheel_digit="1")
    rolled_back = _receipt(
        state_root,
        "next.run-x.json",
        wheel_digit="2",
        state="rolled-back",
        qualified=False,
        parent=prior,
    )
    os.utime(prior.path, (1, 1))
    os.utime(rolled_back.path, None)

    assert effective_receipt(state_root).path == prior.path


def test_unqualified_successor_does_not_supersede_the_prior(tmp_path: Path) -> None:
    state_root = _state_root(tmp_path)
    prior = _receipt(state_root, "prior.json", wheel_digit="1")
    _receipt(
        state_root, "next.run-x.json", wheel_digit="2", qualified=False, parent=prior
    )

    assert effective_receipt(state_root).path == prior.path


def test_two_unrelated_qualified_receipts_are_refused(tmp_path: Path) -> None:
    state_root = _state_root(tmp_path)
    _receipt(state_root, "a.json", wheel_digit="1")
    _receipt(state_root, "b.json", wheel_digit="2")

    with pytest.raises(InstallError, match="2 applied and qualified receipts"):
        effective_receipt(state_root)


def test_no_qualified_receipt_is_refused(tmp_path: Path) -> None:
    state_root = _state_root(tmp_path)
    _receipt(state_root, "a.json", wheel_digit="1", state="rolled-back", qualified=False)

    with pytest.raises(InstallError, match="0 applied and qualified receipts"):
        effective_receipt(state_root)


def test_an_unreadable_receipt_stops_the_decision(tmp_path: Path) -> None:
    state_root = _state_root(tmp_path)
    _receipt(state_root, "prior.json", wheel_digit="1")
    (receipt_directory(state_root) / "broken.json").write_text("{", encoding="utf-8")

    with pytest.raises(InstallError, match="broken.json is unreadable"):
        effective_receipt(state_root)


def test_missing_receipt_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InstallError, match="receipt directory is unavailable"):
        effective_receipt(_state_root(tmp_path))
