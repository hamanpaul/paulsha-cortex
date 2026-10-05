"""#716：canary 證據由 driver 自己的 writer 產生，再交給 validate.py 的 canary profile。

canary run 37331921480 與 37357552758 都跑完全部 live 階段，卻在 validate 才因 driver
與 validator 的欄位形狀不一致而失敗（`persisted_variants`、preflight 的
`returncode`／`skipped`）。原因是 validator 的測試 fixture 是照 validator 手寫的，driver
的測試又把 `_provider_preflight` 整個換掉，兩邊從來沒有接在一起跑過。

這裡以 `driver.main()` 走 canary profile 的真實流程：每份證據都由 driver 的 writer
（`_capture_one_command_upgrade`、`_installed_checks`、`_provider_smokes` 與真實的
`_provider_preflight`、`_manager_github_probe`、`_full_dispatch` 與真實的
`_validate_dispatch_closeout`、`_artifact_inventory`、`_service_rows`）寫出，只把
subprocess／JSON-RPC／帳號查詢換成假的，最後以 `validate.validate(...,
require_canary_profile=True)` 驗收。

唯一不經 driver writer 的是 `attack-matrix.json`：`_permission_attack_matrix` 以 root
在真實主機路徑（`/run`、`/var/lib/cortex`）上做破壞性探測，無法在測試裡執行；它與
release profile 共用，canary run 37357552758 的 validator 已逐項通過這份檔案。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_qualification_driver_hardening as hardening  # noqa: E402
import test_qualification_driver_service_status as service_status  # noqa: E402
from qualification import validate as validate_module  # noqa: E402
from qualification.contract import (  # noqa: E402
    PROVIDERS as PROVIDER_CONTRACTS,
    TOOLCHAIN,
    render_model_identity_overlay,
)


CANDIDATE_SHA = "c" * 40  # 與 service_status 的 receipt plan／loaded runtime 同一個 commit
WHEEL_SHA256 = "b" * 64
BUNDLE_SHA256 = "e" * 64
IMAGE_DIGEST = "sha256:" + "4" * 64
WHEEL_FILENAME = "paulsha_cortex-0.1.13-py3-none-any.whl"
TOOLCHAIN_BIN = "/opt/cortex/toolchain/bin/"
SERVICE_ACCOUNTS = {
    "cortex-egress-proxy.service": ("cortex-egress", 995),
    "cortex-manager.service": ("cortex-manager", 991),
    "cortex-monitor.service": ("cortex-manager", 991),
}


def _attack_matrix_writer(driver):
    """`_permission_attack_matrix` 的替身：寫出 driver 同一組欄位的通過結果。"""

    families = ("capability", "durable-state", "enforcement-plane", "process", "gate")
    required = {
        "capability": ("T1.1", "T1.2", "T1.3", "T1.4"),
        "enforcement-plane": tuple(f"T3.{index}" for index in range(1, 11)),
        "process": ("T4.1", "T4.2", "T4.3", "T4.4"),
        "gate": tuple(f"T5.{index}" for index in range(1, 11)),
    }

    def write(_receipt, evidence_dir: Path) -> None:
        cases = [
            {
                "family": family,
                "case": f"{case_id}-probe",
                "principal": "cortex-builder",
                "status": "passed",
                "returncode": 13,
            }
            for family, case_ids in required.items()
            for case_id in case_ids
        ]
        cases += [
            {
                "family": "durable-state",
                "case": f"jobs-registry:{operation}",
                "principal": principal,
                "status": "passed",
                "returncode": 13,
            }
            for operation in sorted(validate_module.R9_MUTATIONS)
            for principal in sorted(validate_module.R9_HEADLESS_PRINCIPALS)
        ]
        driver._write_json(
            evidence_dir / "attack-matrix.json",
            {
                "schema_version": 1,
                "status": "passed",
                "families": sorted(families),
                "cases": cases,
                "negative_controls": [
                    {
                        "family": family,
                        "case": f"{family}-control",
                        "principal": "cortex-manager",
                        "status": "passed",
                        "returncode": 0,
                    }
                    for family in families
                ],
                "authorized_mutations": [],
                "deny_only_assets": [],
                "covered_assets": 1,
                "registry_asset_ids": ["jobs-registry"],
            },
        )

    return write


def _install_evidence(path: Path) -> None:
    """`cortex install trust-root verify --evidence` 的輸出形狀（installer 產生，driver 原樣轉存）。"""

    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "result": "pass",
                "plan_sha256": "d" * 64,
                "receipt_id": "upgraded",
                "candidate": {"wheel_sha256": WHEEL_SHA256, "bundle_sha256": BUNDLE_SHA256},
                "service_identities": {
                    name: {"user": user, "active_state": "active"}
                    for name, (user, _uid) in SERVICE_ACCOUNTS.items()
                },
                "artifact_hashes": {"units/cortex-manager.service": "1" * 64},
                "attestation": {"ok": True, "warnings": [], "failures": []},
            }
        ),
        encoding="utf-8",
    )


class _LiveSurface:
    """canary 全流程共用的一支 `_run` 假身分：依 argv 分派到各階段的真實輸出形狀。"""

    def __init__(self, driver, *, dispatch, home_root: Path, agy_status_rc: int = 0):
        self.driver = driver
        self.dispatch = dispatch
        self.home_root = home_root
        self.agy_status_rc = agy_status_rc
        self.dispatch_git = hardening._dispatch_fixture_fake_run(driver, dispatch)
        self.terminal = hardening._work_show_envelope(
            dispatch["work_id"],
            state="on-going",
            run_id=dispatch["run_id"],
            run_status="done",
        )
        self.calls: list[tuple[str, ...]] = []

    def account_env(self, account: str) -> dict[str, str]:
        return {"HOME": str(self.home_root / account), "PATH": "/usr/bin:/bin"}

    def ok(self, argv, stdout: str = "", returncode: int = 0):
        return self.driver.CommandResult(tuple(argv), returncode, stdout, "")

    def run(self, argv, *, user=None, env=None, timeout=120):
        argv = tuple(argv)
        self.calls.append(argv)
        executable = argv[0]
        if executable == "systemctl":
            if argv[1] == "is-active":
                return self.ok(argv, "active\n")
            user_name, _uid = SERVICE_ACCOUNTS[argv[2]]
            return self.ok(argv, f"User={user_name}\nGroup={user_name}\nMainPID=4242\n")
        if executable == "/opt/cortex/venv/bin/python":
            if "selfcheck" in argv:
                return self.ok(argv, json.dumps({"ok": True, "job_writable_count": 0}))
            return self.ok(argv, json.dumps({"ok": True}))
        if argv[:3] == ("/opt/cortex/venv/bin/cortex", "service", "status"):
            return self.ok(argv, json.dumps(service_status._upgraded_status_payload()))
        if argv[:4] == ("/opt/cortex/venv/bin/cortex", "run", "work", "intake"):
            return self.ok(argv, json.dumps({"work_id": self.dispatch["work_id"]}) + "\n")
        if argv[:3] == ("/opt/cortex/venv/bin/cortex", "work", "show"):
            return self.ok(argv, json.dumps(self.terminal) + "\n")
        if executable.startswith(TOOLCHAIN_BIN):
            return self._provider(argv)
        if argv[:3] == ("/usr/bin/python3", "-I", "-c"):
            # `_agy_persisted_model_variants`：以帳號身分讀 agy 持久化的對話。
            return self.ok(argv, json.dumps([hardening._agy_variant()]) + "\n")
        if argv[:2] == ("/usr/bin/python3", "-c"):
            return self.ok(argv, "credential-ok\n")
        if argv[:3] == ("/usr/bin/gh", "auth", "status"):
            return self.ok(argv)
        if executable == "/usr/bin/git":
            if "config" in argv:
                gitconfig = Path(self.account_env("cortex-manager")["HOME"]) / ".gitconfig"
                return self.ok(argv, hardening._installed_helper_rows(gitconfig))
            if "ls-remote" in argv:
                return self.ok(argv, "a" * 40 + "\trefs/heads/main\n")
            if "push" in argv:
                return self.ok(argv)
            return self.dispatch_git(argv)
        raise AssertionError(f"unexpected live command: {argv}")

    def _provider(self, argv):
        provider = Path(argv[0]).name
        if argv[-1] == "--version":
            return self.ok(argv, f"{provider} version {TOOLCHAIN[provider]['version']}\n")
        if provider == "agy" and argv[1:3] == ("-p", "/quota"):
            quota = {
                "status": "SUCCESS",
                "command": {
                    "name": "usage",
                    "data": {"groups": [{"buckets": [{"remaining_fraction": 0.5}]}]},
                },
            }
            return self.ok(argv, json.dumps(quota) + "\n", returncode=self.agy_status_rc)
        return self.ok(argv, hardening._smoke(provider))


def _codex_exchange(_command, **_kwargs):
    return (
        {"id": 2, "result": {"account": {"type": "chatgpt", "planType": "pro"}}},
        {"id": 3, "result": hardening._codex_live_rate_limits(ordinary_usage_allowed=True)},
    )


def _copilot_exchange(_command, **_kwargs):
    return (
        {"id": 2, "result": {"authInfo": {"type": "user", "login": "redacted-user"}}},
        {"id": 3, "result": {"quotaSnapshots": hardening._copilot_live_snapshots()}},
    )


def _codex_thread_exchange(_command, *, thread_id, **_kwargs):
    return (
        {"id": 2, "result": {"thread": {"id": thread_id, "modelProvider": "openai"}}},
        {
            "id": 3,
            "result": {
                "thread": {"id": thread_id},
                "model": PROVIDER_CONTRACTS["codex"]["model_id"],
                "reasoningEffort": PROVIDER_CONTRACTS["codex"]["effort"],
                "modelProvider": "openai",
            },
        },
    )


def _run_canary_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **surface_options
) -> tuple[object, Path, _LiveSurface]:
    driver = hardening._load_driver()
    state = tmp_path / "state"
    state.mkdir()
    dispatch = hardening._dispatch_fixture(state, driver)
    surface = _LiveSurface(
        driver, dispatch=dispatch, home_root=tmp_path / "home", **surface_options
    )
    gitconfig = Path(surface.account_env("cortex-manager")["HOME"]) / ".gitconfig"
    gitconfig.parent.mkdir(parents=True)
    gitconfig.write_text(hardening._INSTALLED_MANAGER_CREDENTIAL_SECTION, encoding="utf-8")

    config_root = tmp_path / "project-config"
    config_root.mkdir()
    (config_root / "model-identities.yaml").write_text(
        render_model_identity_overlay(), encoding="utf-8"
    )
    # `_dispatch_fixture` 已把 driver 的 installed runtime env 換成 installer 產生的那份；
    # 只把 coordinator／project-config 根目錄指到測試的 state。
    fixture_env = driver._installed_runtime_env()
    runtime_env = {
        **fixture_env,
        "PSC_COORDINATOR_ROOT": str(dispatch["coordinator"]),
        "PSC_PROJECT_CONFIG_ROOT": str(config_root),
    }

    uids = {user: uid for user, uid in SERVICE_ACCOUNTS.values()}
    monkeypatch.setattr(
        driver.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=uids[name], pw_dir="/")
    )
    import grp

    monkeypatch.setattr(grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=uids[name]))
    monkeypatch.setattr(driver, "_run", surface.run)
    monkeypatch.setattr(driver, "_account_env", surface.account_env)
    monkeypatch.setattr(driver, "_installed_runtime_env", lambda: dict(runtime_env))
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    monkeypatch.setattr(driver, "_require_installed_manager_gitconfig", lambda _path: None)
    monkeypatch.setattr(driver, "_codex_app_server_exchange", _codex_exchange)
    monkeypatch.setattr(driver, "_copilot_app_server_exchange", _copilot_exchange)
    monkeypatch.setattr(driver, "_codex_thread_resume_exchange", _codex_thread_exchange)
    monkeypatch.setattr(driver, "_permission_attack_matrix", _attack_matrix_writer(driver))
    # probe repo 的 clone／登記／等 authority 不寫任何證據（遠端與 Manager 互動）。
    monkeypatch.setattr(driver, "_prepare_probe_dispatch", lambda **_kwargs: None)
    monkeypatch.setattr(driver, "SYSTEM_STATUS_SETTLE_SECONDS", 0)
    monkeypatch.setattr(driver.time, "sleep", lambda _seconds: None)

    prior, upgraded, drill, report = service_status._upgrade_inputs(tmp_path)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    receipt_path = inputs / "upgraded-receipt.json"
    report["receipt"]["path"] = str(receipt_path)
    for name, document in (
        ("prior-receipt.json", prior),
        ("upgraded-receipt.json", upgraded),
        ("upgrade-drill-report.json", drill),
        ("upgrade-report.json", report),
    ):
        (inputs / name).write_text(json.dumps(document), encoding="utf-8")
    install_evidence = inputs / "install-verification.json"
    _install_evidence(install_evidence)

    output_root = tmp_path / "qualification-output"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cortex-release-qualification",
            "--receipt", str(receipt_path),
            "--prior-receipt", str(inputs / "prior-receipt.json"),
            "--upgrade-drill-report", str(inputs / "upgrade-drill-report.json"),
            "--upgrade-report", str(inputs / "upgrade-report.json"),
            "--install-evidence", str(install_evidence),
            "--candidate-sha", CANDIDATE_SHA,
            "--wheel-sha256", WHEEL_SHA256,
            "--bundle-sha256", BUNDLE_SHA256,
            "--image-digest", IMAGE_DIGEST,
            "--wheel-filename", WHEEL_FILENAME,
            "--profile", "deployment-canary",
            "--probe-repository", dispatch["repository"],
            "--probe-work-id", dispatch["work_id"],
            "--probe-issue", str(dispatch["issue"]),
            "--output", str(output_root / "qualification.json"),
            "--evidence-dir", str(output_root / "evidence"),
        ],
    )
    return driver, output_root, surface


def _validate_canary(output_root: Path, dispatch_identity: dict) -> None:
    payload = json.loads((output_root / "qualification.json").read_text(encoding="utf-8"))
    validate_module.validate(
        payload,
        candidate_sha=CANDIDATE_SHA,
        wheel_sha256=WHEEL_SHA256,
        bundle_sha256=BUNDLE_SHA256,
        evidence_root=output_root,
        require_canary_profile=True,
        canary_repository=dispatch_identity["repository"],
        canary_work_id=dispatch_identity["work_id"],
        canary_issue=dispatch_identity["issue"],
    )


def _dispatch_identity(surface: _LiveSurface) -> dict:
    return {
        key: surface.dispatch[key] for key in ("repository", "work_id", "issue")
    }


def test_canary_driver_evidence_passes_the_canary_profile_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    driver, output_root, surface = _run_canary_driver(tmp_path, monkeypatch)

    assert driver.main() == 0, capsys.readouterr().err
    _validate_canary(output_root, _dispatch_identity(surface))

    evidence = output_root / "evidence"
    assert {path.name for path in evidence.iterdir()} == {
        "artifact-inventory.json",
        "attack-matrix.json",
        "dispatch-closeout.json",
        "generated-installed-attestation.json",
        "install-semantic-checks.json",
        "install-verification.json",
        "manager-github-auth.json",
        "one-command-upgrade.json",
        "provider-capabilities.json",
        "rollback-loaded-runtime-status.json",
        "system-loaded-runtime-status.json",
    }
    providers = json.loads((evidence / "provider-capabilities.json").read_text())["providers"]
    # 三個 provider 的 preflight 都由真實的 `_provider_preflight` 產生（canary run
    # 37357552758 在 agy 這裡失敗；codex／copilot 走 app-server，形狀必須相同）。
    for name in ("agy", "codex", "copilot"):
        assert providers[name]["preflight"] == {
            "returncode": 0,
            "status": "ready",
            "authenticated": True,
            "quota": "available",
            "fallback": False,
            "skipped": False,
        }
    # status probe 真的被呼叫：agy 走一次性 `/quota`，其餘走 app-server（已換成假的）。
    assert any(call[1:3] == ("-p", "/quota") for call in surface.calls)


def _rewrite_provider_preflight(driver, output_root: Path, mutate) -> None:
    """改 provider-capabilities 後以 driver 自己的 inventory writer 重新綁定雜湊。"""

    evidence = output_root / "evidence"
    path = evidence / "provider-capabilities.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document["providers"])
    driver._write_json(path, document)
    # driver 只在全新的 evidence 目錄呼叫一次 inventory writer；重寫前先移除舊的那份。
    (evidence / "artifact-inventory.json").unlink()
    qualification_path = output_root / "qualification.json"
    qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
    qualification["artifacts"] = driver._artifact_inventory(evidence)
    driver._write_json(qualification_path, qualification)


@pytest.mark.parametrize("provider", ["agy", "codex", "copilot"])
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda row: row.pop("returncode"), "missing required fields: returncode"),
        (lambda row: row.pop("skipped"), "missing required fields: skipped"),
        (lambda row: row.update(returncode=1), "did not return successful native metadata"),
        (lambda row: row.update(skipped=True), "did not return successful native metadata"),
        (lambda row: row.update(probe="extra"), "has unknown fields: probe"),
    ],
)
def test_canary_validator_rejects_driver_preflight_without_returncode_or_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider: str,
    mutation,
    message: str,
) -> None:
    driver, output_root, surface = _run_canary_driver(tmp_path, monkeypatch)
    assert driver.main() == 0, capsys.readouterr().err

    _rewrite_provider_preflight(
        driver, output_root, lambda providers: mutation(providers[provider]["preflight"])
    )

    with pytest.raises(validate_module.ValidationError, match=message):
        _validate_canary(output_root, _dispatch_identity(surface))


def test_canary_driver_fails_closed_before_writing_evidence_when_a_status_probe_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    driver, output_root, _surface = _run_canary_driver(
        tmp_path, monkeypatch, agy_status_rc=1
    )

    assert driver.main() == 1
    assert "provider agy structured status probe" in capsys.readouterr().err
    assert not (output_root / "evidence" / "provider-capabilities.json").exists()
    assert not (output_root / "qualification.json").exists()


@pytest.mark.parametrize(
    ("verdict", "returncode", "message"),
    [
        ({"status": "ready", "authenticated": True, "quota": "available"}, 0, "shape"),
        (
            {
                "status": "ready",
                "authenticated": True,
                "quota": "available",
                "fallback": False,
                "skipped": True,
            },
            0,
            "shape",
        ),
        (
            {"status": "ready", "authenticated": True, "quota": "available", "fallback": False},
            2,
            "rc=2",
        ),
        (
            {"status": "ready", "authenticated": True, "quota": "available", "fallback": False},
            True,
            "rc=True",
        ),
    ],
)
def test_preflight_evidence_refuses_a_partial_verdict_or_failed_probe(
    verdict: dict, returncode: object, message: str
) -> None:
    driver = hardening._load_driver()

    with pytest.raises(driver.QualificationFailure, match=message):
        driver._preflight_evidence(verdict, returncode=returncode)
