"""R3（trust-root Phase 1）：Manager 啟動自檢，WARN-only。

驗收：自檢在現行部署上輸出的診斷與 spec 背景段盤點一致（反證盤點正確）——本測試
以一棵刻意複製部署現況（group-writable 的 Manager-owned 目錄）的合成樹驗證自檢能
把它們正確標為 job-writable，且 job-visible 樹不誤報、缺檔標 MISSING、永不 fail。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from paulsha_cortex.trust_root import selfcheck
from paulsha_cortex.trust_root.selfcheck import FindingStatus, run_self_check


def _mkdir(path: Path, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)


@pytest.fixture()
def deployment_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """複製 spec 背景段實測：control/coordinator/specs 為 group-writable。"""
    agents = tmp_path / "agents"
    # Manager-owned 樹：coordinator/control/specs group-writable（drwxrwxr-x）。
    _mkdir(agents / "coordinator", 0o775)
    _mkdir(agents / "control", 0o775)
    _mkdir(agents / "specs", 0o775)
    # monitor 為 drwxr-xr-x（新加、非 group-writable）。
    _mkdir(agents / "monitor", 0o755)
    monkeypatch.setenv("PSC_AGENTS_ROOT", str(agents))
    return tmp_path


def _finding(report: selfcheck.SelfCheckReport, asset_id: str):
    return next(f for f in report.findings if f.asset_id == asset_id)


def test_group_writable_manager_owned_flagged_job_writable(deployment_tree: Path) -> None:
    report = run_self_check(home=deployment_tree)
    for asset_id in ("coordinator-root-tree", "control-root-tree", "dispatch-specs-tree"):
        finding = _finding(report, asset_id)
        assert finding.status is FindingStatus.JOB_WRITABLE, (asset_id, finding.detail)
        assert "g+w" in finding.detail


def test_monitor_tree_not_flagged(deployment_tree: Path) -> None:
    finding = _finding(run_self_check(home=deployment_tree), "monitor-state-tree")
    assert finding.status is FindingStatus.OK


def test_job_visible_tree_not_reported_as_defect(deployment_tree: Path) -> None:
    """job-visible 樹即使 group/other 可寫也是 NOT_APPLICABLE，不誤報成缺陷。"""
    report = run_self_check(home=deployment_tree)
    for f in report.findings:
        if f.tree == "job-visible":
            assert f.status in {
                FindingStatus.NOT_APPLICABLE,
                FindingStatus.MISSING,
                FindingStatus.UNRESOLVED,
            }


def test_missing_path_reported_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSC_AGENTS_ROOT", str(tmp_path / "does-not-exist"))
    report = run_self_check(home=tmp_path)
    coord = _finding(report, "coordinator-root-tree")
    assert coord.status is FindingStatus.MISSING


def test_report_is_warn_only(deployment_tree: Path) -> None:
    """Phase 1：即使偵測到 job-writable，warn_only 為 True、不 fail-closed。"""
    report = run_self_check(home=deployment_tree)
    assert report.warn_only is True
    assert report.job_writable  # 有偵測到漂移
    assert report.ok is False   # ok 只供觀測，不 gate 啟動


def test_warning_lines_generated(deployment_tree: Path) -> None:
    report = run_self_check(home=deployment_tree)
    lines = report.warning_lines()
    assert lines
    assert all("trust-root WARN" in ln and "Phase 2" in ln for ln in lines)


def test_home_is_masked_in_findings(deployment_tree: Path) -> None:
    """診斷路徑遮蔽 $HOME（R-21 tier: shareable：不得吐個人絕對路徑）。"""
    report = run_self_check(home=deployment_tree)
    coord = _finding(report, "coordinator-root-tree")
    assert coord.path.startswith("$HOME")
    assert str(deployment_tree) not in coord.path


def test_emit_startup_warnings_never_raises_and_returns_report(
    deployment_tree: Path,
) -> None:
    captured: list[str] = []
    report = selfcheck.emit_startup_warnings(
        emit=captured.append, home=deployment_tree
    )
    assert report.job_writable
    assert captured  # 每個 job-writable 一行


def test_emit_startup_warnings_swallows_emit_errors(deployment_tree: Path) -> None:
    def boom(_line: str) -> None:
        raise RuntimeError("sink down")

    # 不得傳播——WARN-only 永不阻擋啟動。
    report = selfcheck.emit_startup_warnings(emit=boom, home=deployment_tree)
    assert isinstance(report, selfcheck.SelfCheckReport)


def test_to_dict_structure(deployment_tree: Path) -> None:
    payload = run_self_check(home=deployment_tree).to_dict()
    assert payload["check"] == "trust-root-selfcheck"
    assert payload["phase"] == 1
    assert payload["enforcement"] == "warn-only"
    assert isinstance(payload["findings"], list)
