"""#841 AC5：真 Trust Root rollback receipt 串接 loaded-runtime receipt，且 rollback 不清除 job。

既有 ``test_runtime_attestation.py`` 的 rollback 測試只用手寫 ``trust_root`` dict，或把
``InstallReceipt.load`` monkeypatch 成假物件。這裡改走正式 transactional installer：
``new_install_receipt`` → ``apply_plan`` → ``activate_receipt`` → ``verify_receipt`` →
``rollback_receipt``，receipt 是真的落在磁碟上的 ``InstallReceipt``，
``trust_root_receipt_summary`` 讀的是同一個檔案（不 monkeypatch ``load``）。

OS seam 只把「需要 root 的事實」（帳號、owner／ACL、systemd start／stop）放在記憶體；
本 receipt 建立的目錄真的建在 tmp tree，rollback 對該目錄的處置與殘留 state 盤點交給
正式 ``LocalInstallBackend``（``require_root=False``）的 ``rollback_step``／
``list_unknown_state``；in-flight 事實由 installer preflight 同一個
``_durable_in_flight_job_count`` 讀真的 durable ``jobs.json``。
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from paulsha_cortex.runtime_attestation import (
    compare_runtime_state,
    configuration_revision,
    inspect_runtime_state,
    record_runtime_startup,
    trust_root_receipt_summary,
)
from paulsha_cortex.trust_root.install import (
    InstallReceipt,
    activate_receipt,
    apply_plan,
    new_install_receipt,
    plan_sha256,
    rollback_receipt,
    verify_receipt,
)
from paulsha_cortex.trust_root.install import backend as install_backend
from paulsha_cortex.trust_root.install import core as install_core


_SERVICES = (
    "cortex-egress-proxy.service",
    "cortex-manager.service",
    "cortex-monitor.service",
)
_MANAGER_CONFIG = {
    "arguments": {"poll_interval": 5.0},
    "environment": {"PSC_INSTANCE": "cortex"},
}


@pytest.fixture(autouse=True)
def _synthetic_plan_and_unprivileged_receipt_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """與 ``test_trust_root_install_verify.py`` 相同：這些 plan 只帶本測試需要的步驟，
    略過完整 plan envelope 驗證；receipt 權威檔的 root-owned 檢查是主機事實，非 root
    測試無法滿足，其餘 transaction／rollback／receipt 讀寫都走正式實作。"""

    for name, replacement in (
        ("_validate_repo_identity", lambda plan: plan["repo_identity"]),
        ("_validate_apply_account_inventories", lambda _plan: {}),
        ("_validate_required_credentials", lambda _plan: None),
        ("_validate_candidate_venv", lambda _plan, _steps: None),
        ("_validate_account_step_bijection", lambda _rows, _steps: None),
        ("_validate_repository_step_bijection", lambda _plan, _steps, _identity: None),
        ("_validate_canonical_receipt_path", lambda _plan: None),
        ("_validate_finalized_apply_surfaces", lambda _plan, _steps: None),
        ("_validate_receipt_parent", lambda _observed, _path: None),
        ("_validate_receipt_file", lambda _observed, _path: None),
    ):
        monkeypatch.setattr(install_core, name, replacement)


def _unit(user: str, exec_start: str) -> dict[str, str]:
    return {
        "content": f"# generated\n[Service]\nUser={user}\nExecStart={exec_start}\n",
        "owner": "root",
        "group": "root",
        "mode": "0644",
    }


def _generated_inventory() -> dict[str, dict[str, dict[str, str]]]:
    return {
        "units": {
            "cortex-egress-proxy.service": _unit(
                "cortex-egress-proxy", "/opt/cortex/venv/bin/cortex egress-proxy"
            ),
            "cortex-manager.service": _unit(
                "cortex-manager", "/opt/cortex/venv/bin/cortex service run"
            ),
            "cortex-monitor.service": _unit(
                "cortex-manager", "/opt/cortex/venv/bin/cortex monitor"
            ),
        },
        "shim": {},
        "polkit": {},
        "gitconfigs": {},
        "toolchain_wrappers": {},
        "environment": {},
        "enforcement": {},
    }


def _service_identities() -> dict[str, dict[str, str]]:
    return {
        service: {
            "user": "cortex-egress-proxy" if "egress" in service else "cortex-manager",
            "exec_path": "/opt/cortex/venv/bin/cortex",
            "exec_sha256": "d" * 64,
            "active_state": "active",
        }
        for service in _SERVICES
    }


def _plan(
    tmp_path: Path, state_root: Path, apply_order: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "scheme": "four-way",
        "repo_identity": {
            "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
            "commit": "a" * 40,
        },
        "candidate": {"wheel_sha256": "b" * 64, "bundle_sha256": "c" * 64},
        "accounts": [],
        "roots": {
            "deploy": str(tmp_path / "opt" / "cortex"),
            "state": str(state_root),
            "systemd": str(tmp_path / "etc" / "systemd" / "system"),
            "polkit": str(tmp_path / "etc" / "polkit-1" / "rules.d"),
        },
        "required_credentials": [],
        "apply_order": apply_order,
        "generated": _generated_inventory(),
    }


def _directory_step(step_id: str, path: Path) -> dict[str, object]:
    return {
        "step_id": step_id,
        "kind": "asset",
        "asset_type": "directory",
        "path": str(path),
        "owner": "cortex-manager",
        "group": "cortex-manager",
        "mode": "0700",
        "acls": [],
        "operations": ["snapshot", "chown", "chmod"],
        "desired_sha256": hashlib.sha256(step_id.encode("utf-8")).hexdigest(),
        "durable": True,
    }


def _unit_file_step(path: Path) -> dict[str, object]:
    return {
        "step_id": "asset:manager-unit",
        "kind": "asset",
        "path": str(path),
        "owner": "root",
        "group": "root",
        "mode": "0644",
        "acls": [],
        "operations": ["snapshot", "chown", "chmod"],
        "desired_sha256": "2" * 64,
    }


class FilesystemSeamBackend:
    """需要 root 的 owner／ACL／systemd 事實放記憶體；本 receipt 建立的目錄真的建在磁碟，
    rollback 與殘留盤點交給正式 ``LocalInstallBackend``。"""

    def __init__(self) -> None:
        self.states: dict[str, dict[str, object]] = {}
        self.creation_identities: dict[str, dict[str, object]] = {}
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.systemd_reloads = 0
        self._local = install_backend.LocalInstallBackend(require_root=False)

    def preflight_facts(self, plan) -> dict[str, object]:
        return {
            "systemd": True,
            "polkit": True,
            "cgroup_v2": True,
            "acl": True,
            "disk_free_bytes": 2 * 1024 * 1024 * 1024,
            "cortex_account_universal_nopasswd": {"accounts": [], "unproven": None},
            # 安裝當下 durable registry 還不存在：由正式 reader 讀出 0。
            "in_flight_jobs": install_backend._durable_in_flight_job_count(plan),
            "services": {service: "inactive" for service in _SERVICES},
            "accounts": {},
            "paths": {
                str(step["path"]): {
                    "exists": Path(str(step["path"])).exists(),
                    "is_symlink": False,
                }
                for step in plan["apply_order"]
            },
        }

    def inspect_step(self, step) -> dict[str, object]:
        return deepcopy(self.states.get(step["step_id"], {"exists": False}))

    def _apply(self, step, creation_checkpoint=None) -> dict[str, object]:
        prior = self.inspect_step(step)
        if not prior.get("exists"):
            path = Path(str(step["path"]))
            if step.get("asset_type") == "directory":
                path.mkdir(mode=0o700)
            else:
                path.write_text("# candidate unit\n", encoding="utf-8")
            authority = {
                "device": 1,
                "inode": len(self.creation_identities) + 100,
                "file_type": step.get("asset_type", "file"),
            }
            self.creation_identities[step["step_id"]] = authority
            if creation_checkpoint is not None:
                creation_checkpoint(authority)
        state = {
            "exists": True,
            "installed_sha256": step["desired_sha256"],
            "owner": step["owner"],
            "group": step["group"],
            "mode": step["mode"],
            "acl": deepcopy(step["acls"]),
        }
        self.states[step["step_id"]] = state
        return {"prior": prior, **state}

    def apply_step(self, step) -> dict[str, object]:
        return self._apply(step)

    def apply_step_checkpointed(self, step, expected_prior, creation_checkpoint):
        assert self.inspect_step(step) == expected_prior
        return self._apply(step, creation_checkpoint)

    def creation_authority_matches(self, step, authority) -> bool:
        return authority == self.creation_identities.get(step["step_id"])

    def cleanup_prepared_replacement(self, _entry) -> None:
        return None

    def service_identities(self) -> dict[str, dict[str, str]]:
        return _service_identities()

    def start_service(self, name: str) -> None:
        self.started.append(name)

    def stop_service(self, name: str) -> None:
        self.stopped.append(name)

    def rollback_step(self, entry) -> None:
        # 正式 backend：本 receipt 建立的檔案刪除；建立的目錄只有空的才移除。
        self._local.rollback_step(entry)
        prior = deepcopy(entry["prior"])
        if prior.get("exists"):
            self.states[entry["step_id"]] = prior
        else:
            self.states.pop(entry["step_id"], None)

    def reload_systemd_units(self) -> None:
        self.systemd_reloads += 1

    def list_unknown_state(self, receipt) -> tuple[str, ...]:
        return tuple(self._local.list_unknown_state(receipt))

    def rollback_credentials(self, _receipt):
        return ()


def _install_verified(
    tmp_path: Path, plan: dict[str, object]
) -> tuple[Path, FilesystemSeamBackend]:
    backend = FilesystemSeamBackend()
    receipt_path = (tmp_path / "etc" / "cortex" / "install-receipt.json").absolute()
    receipt_path.parent.mkdir(parents=True)
    receipt = new_install_receipt(plan, path=receipt_path)
    apply_plan(plan, confirm_sha256=plan_sha256(plan), receipt=receipt, backend=backend)
    activate_receipt(receipt, backend=backend)
    result = verify_receipt(
        receipt,
        plan=plan,
        expected_inventory=_generated_inventory(),
        installed_inventory=_generated_inventory(),
        service_identities=_service_identities(),
        evidence_path=tmp_path / "evidence" / "trust-root-install.json",
    )
    assert result.ok, result.report.to_dict()
    return receipt_path, backend


def _write_durable_jobs(coordinator_root: Path) -> bytes:
    """Manager 在安裝後派出的 durable job：兩筆 in-flight、一筆已結束。"""

    payload = {
        "jobs": [
            {"job_id": "job-running", "status": "running"},
            {"job_id": "job-dispatched", "status": "dispatched"},
            {"job_id": "job-exited", "status": "exited"},
        ]
    }
    path = coordinator_root / "jobs.json"
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path.read_bytes()


def _manager_startup(
    coordinator_root: Path,
    *,
    artifact_digest: str,
    pid: int,
    trust_root,
    started_at: str,
) -> Path:
    return record_runtime_startup(
        service="manager",
        instance="cortex",
        state_root=coordinator_root,
        configuration=_MANAGER_CONFIG,
        artifact={
            "kind": "installed-wheel",
            "package": "paulsha-cortex",
            "package_version": "0.1.11",
            "source_revision": "unknown",
            "sha256": artifact_digest,
        },
        started_at=started_at,
        pid=pid,
        trust_root=trust_root,
    )


def test_completed_install_rollback_receipt_chains_prior_and_current_loaded_receipts_without_clearing_jobs(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "var" / "lib" / "cortex"
    coordinator_root = state_root / "coordinator"
    coordinator_root.mkdir(parents=True)
    unit_path = tmp_path / "etc" / "systemd" / "system" / "cortex-manager.service"
    unit_path.parent.mkdir(parents=True)
    plan = _plan(tmp_path, state_root, [_unit_file_step(unit_path)])
    receipt_path, backend = _install_verified(tmp_path, plan)

    installed = trust_root_receipt_summary(receipt_path)
    assert installed["status"] == "verified"
    assert installed["plan_sha256"] == plan_sha256(plan)
    assert str(receipt_path) not in json.dumps(installed)

    # 被 activation 啟動的 candidate Manager 寫下 loaded receipt，之後派出 durable job。
    candidate = _manager_startup(
        coordinator_root,
        artifact_digest="1" * 64,
        pid=4101,
        trust_root=installed,
        started_at="2026-09-29T01:00:00Z",
    )
    candidate_bytes = candidate.read_bytes()
    jobs_bytes = _write_durable_jobs(coordinator_root)
    assert install_backend._durable_in_flight_job_count(plan) == 2

    report = rollback_receipt(InstallReceipt.load(receipt_path), backend=backend)

    assert report.to_dict()["retained_drift"] == []
    assert report.to_dict()["retained_unknown"] == []
    assert backend.stopped == list(reversed(_SERVICES))
    assert backend.systemd_reloads == 1
    assert not unit_path.exists()
    assert InstallReceipt.load(receipt_path).to_dict()["state"] == "rolled-back"
    rolled_back = trust_root_receipt_summary(receipt_path)
    assert rolled_back["status"] == "rolled-back"
    assert rolled_back["receipt_id"] == installed["receipt_id"]
    assert rolled_back["plan_sha256"] == installed["plan_sha256"]
    assert len(rolled_back["rollback_revision"]) == 64
    assert rolled_back["receipt_sha256"] != installed["receipt_sha256"]

    # rollback 後以 prior artifact 重新啟動的 Manager：新 receipt 串回同一張 install receipt。
    restored = _manager_startup(
        coordinator_root,
        artifact_digest="0" * 64,
        pid=4102,
        trust_root=rolled_back,
        started_at="2026-09-29T02:00:00Z",
    )
    state = inspect_runtime_state(coordinator_root, service="manager", instance="cortex")
    assert state["status"] == "attested"
    assert state["latest"]["receipt_id"] != state["previous_process_start"]["receipt_id"]
    assert state["previous_process_start"]["artifact"]["sha256"] == "1" * 64
    assert state["previous_process_start"]["trust_root"]["status"] == "verified"
    assert state["latest"]["artifact"]["sha256"] == "0" * 64
    assert state["latest"]["trust_root"]["status"] == "rolled-back"
    assert (
        state["latest"]["trust_root"]["receipt_id"]
        == state["previous_process_start"]["trust_root"]["receipt_id"]
        == installed["receipt_id"]
    )
    assert state["latest"]["trust_root"]["rollback_revision"] == rolled_back["rollback_revision"]
    assert candidate.read_bytes() == candidate_bytes
    assert state["latest"]["receipt_id"] in restored.name

    # rollback 不清 job：durable registry 原封不動，installer 的 in-flight 事實仍為 2；
    # 以真實數字比對時只標 blocked-in-flight，從不宣稱可以安全更新。
    assert (coordinator_root / "jobs.json").read_bytes() == jobs_bytes
    in_flight = install_backend._durable_in_flight_job_count(plan)
    assert in_flight == 2
    comparison = compare_runtime_state(
        state,
        current_artifact=state["latest"]["artifact"],
        declared_config_revision=configuration_revision(_MANAGER_CONFIG),
        expected_pid=4102,
        in_flight_jobs=in_flight,
    )
    assert comparison["status"] == "match"
    assert comparison["transition_disposition"] == "blocked-in-flight"
    assert comparison["transition_safe"] is False
    assert (coordinator_root / "jobs.json").read_bytes() == jobs_bytes


def test_rollback_of_install_owned_state_tree_retains_jobs_and_is_not_summarized_as_rolled_back(
    tmp_path: Path,
) -> None:
    """state tree 由本 receipt 建立時，Manager 事後寫進去的 jobs.json 與 loaded receipt
    是 rollback 無權刪除的 durable state：正式 backend 保留整棵目錄並把它們列為
    retained unknown，receipt 因而停在 ``rollback-blocked``。loaded receipt 引用的 Trust Root 摘要
    必須反映這個結果，不能把沒完成的 rollback 寫成 ``rolled-back``。"""

    state_root = tmp_path / "var" / "lib" / "cortex"
    state_root.parent.mkdir(parents=True)
    plan = _plan(tmp_path, state_root, [_directory_step("asset:state-root", state_root)])
    receipt_path, backend = _install_verified(tmp_path, plan)
    assert state_root.is_dir()
    # Manager 在安裝建立的 state tree 內建立自己的 coordinator root（installer 預設
    # ``<state>/coordinator`` 就是 in-flight 事實讀取的 durable registry 位置）。
    coordinator_root = state_root / "coordinator"
    coordinator_root.mkdir(mode=0o700)

    installed = trust_root_receipt_summary(receipt_path)
    assert installed["status"] == "verified"
    candidate = _manager_startup(
        coordinator_root,
        artifact_digest="1" * 64,
        pid=4201,
        trust_root=installed,
        started_at="2026-09-29T01:00:00Z",
    )
    candidate_bytes = candidate.read_bytes()
    jobs_bytes = _write_durable_jobs(coordinator_root)

    report = rollback_receipt(InstallReceipt.load(receipt_path), backend=backend)

    retained = set(report.to_dict()["retained_unknown"])
    assert str(coordinator_root / "jobs.json") in retained
    assert str(candidate) in retained
    assert coordinator_root.is_dir() and state_root.is_dir()
    assert InstallReceipt.load(receipt_path).to_dict()["state"] == "rollback-blocked"
    assert (coordinator_root / "jobs.json").read_bytes() == jobs_bytes
    assert candidate.read_bytes() == candidate_bytes
    assert install_backend._durable_in_flight_job_count(plan) == 2

    blocked = trust_root_receipt_summary(receipt_path)
    assert blocked["status"] == "unknown"
    assert blocked["reason"] == "install-rollback-blocked"
    assert blocked["receipt_id"] == installed["receipt_id"]
    assert "rollback_revision" in blocked

    _manager_startup(
        coordinator_root,
        artifact_digest="0" * 64,
        pid=4202,
        trust_root=blocked,
        started_at="2026-09-29T02:00:00Z",
    )
    state = inspect_runtime_state(coordinator_root, service="manager", instance="cortex")
    assert state["previous_process_start"]["trust_root"]["status"] == "verified"
    assert state["latest"]["trust_root"]["status"] == "unknown"
    assert state["latest"]["trust_root"]["reason"] == "install-rollback-blocked"
    assert state["latest"]["trust_root"]["receipt_id"] == installed["receipt_id"]
    assert candidate.read_bytes() == candidate_bytes


def test_interrupted_rollback_receipt_is_neither_verified_nor_rolled_back(
    tmp_path: Path,
) -> None:
    """rollback 已開始停服務、還沒完成時中斷：receipt 停在 ``rolling-back``，
    ``qualified`` 尚未清掉、activation journal 已被逐筆移除。摘要不得回頭把它當成
    ``verified``。"""

    state_root = tmp_path / "var" / "lib" / "cortex"
    (state_root / "coordinator").mkdir(parents=True)
    unit_path = tmp_path / "etc" / "systemd" / "system" / "cortex-manager.service"
    unit_path.parent.mkdir(parents=True)
    plan = _plan(tmp_path, state_root, [_unit_file_step(unit_path)])
    receipt_path, backend = _install_verified(tmp_path, plan)
    assert trust_root_receipt_summary(receipt_path)["status"] == "verified"

    def crash_on_rollback(_entry) -> None:
        raise RuntimeError("simulated crash while restoring the unit file")

    backend.rollback_step = crash_on_rollback
    with pytest.raises(RuntimeError, match="simulated crash"):
        rollback_receipt(InstallReceipt.load(receipt_path), backend=backend)

    document = InstallReceipt.load(receipt_path).to_dict()
    assert document["state"] == "rolling-back"
    assert document["qualified"] is True
    assert document["activation_journal"] == []
    interrupted = trust_root_receipt_summary(receipt_path)
    assert interrupted["status"] == "unknown"
    assert interrupted["reason"] == "install-rollback-incomplete"
    assert interrupted["receipt_id"] == document["receipt_id"]
    assert unit_path.exists()
