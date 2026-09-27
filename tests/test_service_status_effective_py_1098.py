"""#1098：service status／doctor 應以有效宣告解析 wrapper 的 ``PY`` 覆寫與顯示欄位。

installer 產生的 manager unit 形如
``ExecStart=/usr/bin/env bash <pkg>/scripts/service-manager.sh``，實際執行哪個
直譯器由 wrapper（``scripts/service-manager.sh``）讀環境變數 ``PY`` 決定；
drop-in 可以用 ``Environment=PY=/other/venv/bin/python`` 覆寫它，或覆寫
``PSC_MANAGER_INTERVAL_SECONDS``／``PSC_MANAGER_SPECS_DIR`` 等顯示欄位。修法前
``_declared_service_artifact`` 的 wrapper 分支完全沒看 ``PY``，永遠回報 wrapper
腳本自己所在的套件根；``cortex service status`` 的 ``env`` 顯示欄位也固定讀
``~/.agents/core/runtime/<instance>-manager.env`` 這份可能已過時的檔案，不是
systemd 有效宣告。

本檔不依賴主機真實 systemd：一律 monkeypatch
``paulsha_cortex.porcelain._runtime_probe`` 的 ``shutil.which``／
``subprocess.run``，讓 fake ``systemctl --user show`` 回傳依照真實 systemd
輸出順序組出的文字（``Id=`` 在區塊中間、``EnvironmentFiles`` 多值時每個檔案各
佔一行），與 ``tests/test_runtime_attestation.py``／
``tests/test_porcelain_service.py`` 既有慣例一致。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.runtime_attestation import (
    artifact_identity_from_package_root,
    artifact_identity_from_python,
    compare_runtime_state,
    configuration_revision,
    inspect_runtime_state,
    record_runtime_startup,
)


def _write_fake_install(site: Path, marker: str) -> Path:
    """在 ``site`` 底下造一份看起來像真的 ``paulsha_cortex`` 安裝（含
    dist-info），供 ``artifact_identity_from_package_root``／
    ``artifact_identity_from_python`` 辨識。沿用
    ``tests/test_runtime_attestation.py`` 的同名 helper 寫法。"""

    package_root = site / "paulsha_cortex"
    (package_root / "scripts").mkdir(parents=True)
    (package_root / "coordinator").mkdir()
    (package_root / "monitor").mkdir()
    (package_root / "__init__.py").write_text(f"MARKER = {marker!r}\n", encoding="utf-8")
    (package_root / "scripts" / "service-manager.sh").write_text(
        f"# {marker}\n", encoding="utf-8"
    )
    (package_root / "coordinator" / "manager_daemon.py").write_text(
        f"MARKER = {marker!r}\n", encoding="utf-8"
    )
    (package_root / "monitor" / "__init__.py").write_text(
        f"MARKER = {marker!r}\n", encoding="utf-8"
    )
    metadata = site / "paulsha_cortex-0.1.10.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: paulsha-cortex\nVersion: 0.1.10\n",
        encoding="utf-8",
    )
    return package_root


def _manager_block(
    unit: str,
    fragment: Path,
    *,
    exec_start_argv: str,
    environment: str = "",
    environment_files: tuple[str, ...] = (),
    main_pid: int = 0,
    dropin_paths: str = "",
) -> str:
    """組出單個 unit 的 ``systemctl --user show`` 區塊，欄位順序依真實輸出：
    ``ExecStart``／``Environment``／``EnvironmentFiles``（多值每檔一行）／
    ``WorkingDirectory``／``MainPID``，``Id`` 排在區塊中間，之後才是
    ``LoadState``／``ActiveState``／``SubState``／``FragmentPath``／
    ``DropInPaths``——與
    ``test_systemctl_show_parses_real_property_order_and_multi_environment_files``
    的順序一致，不把 ``Id=`` 當區塊起點。"""

    environment_files_lines = (
        "".join(f"EnvironmentFiles={path} (ignore_errors=no)\n" for path in environment_files)
        if environment_files
        else "EnvironmentFiles=\n"
    )
    return (
        f"ExecStart={{ path=/usr/bin/env ; argv[]={exec_start_argv} ; "
        "ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; "
        "code=(null) ; status=0/0 }\n"
        f"Environment={environment}\n"
        f"{environment_files_lines}"
        f"WorkingDirectory=/\nMainPID={main_pid}\n"
        f"Id={unit}\nLoadState=loaded\nActiveState=active\nSubState=running\n"
        f"FragmentPath={fragment}\nDropInPaths={dropin_paths}\n"
    )


def _not_found_block(unit: str) -> str:
    """尚未載入（或這個 instance 沒有）的 unit：``systemctl show`` 仍回 0，只給
    殘缺屬性。沿用
    ``test_systemd_not_found_units_fall_back_to_unit_files_not_unknown`` 的
    寫法，讓 manager timer／monitor service 不干擾本檔只關注 manager artifact
    的測試。"""

    return (
        f"MainPID=0\nEnvironment=\nWorkingDirectory=\nId={unit}\n"
        "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
        "FragmentPath=\nDropInPaths=\n"
    )


def _setup_manager_unit(tmp_path: Path, instance: str = "test") -> tuple[Path, Path]:
    """在假 ``home`` 底下建立 manager unit fragment 檔案，回傳
    ``(home, manager_unit_path)``。``probe_service_runtime`` 只信任
    ``FragmentPath`` 剛好等於這個路徑的 systemd 有效宣告（見
    ``_runtime_probe._probe_units_raw``），因此檔案必須真的存在於這個位置。"""

    home = tmp_path / "home"
    unit_root = home / ".config" / "systemd" / "user"
    unit_root.mkdir(parents=True)
    manager_unit_path = unit_root / f"{instance}-manager.service"
    manager_unit_path.write_text(
        "[Unit]\n[Service]\nExecStart=/usr/bin/env bash placeholder\n",
        encoding="utf-8",
    )
    return home, manager_unit_path


def _fake_systemctl(monkeypatch: pytest.MonkeyPatch, show_output: str) -> None:
    from paulsha_cortex.porcelain import _runtime_probe

    monkeypatch.setattr(
        _runtime_probe, "shutil", SimpleNamespace(which=lambda _name: "/usr/bin/systemctl")
    )
    monkeypatch.setattr(
        _runtime_probe.subprocess,
        "run",
        lambda _argv, **_kwargs: SimpleNamespace(returncode=0, stdout=show_output),
    )


def test_manager_wrapper_py_dropin_resolves_new_venv_and_breaks_stale_receipt_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """drop-in 以 ``Environment=PY=<new venv>`` 覆寫 wrapper 實際執行的直譯器
    時，manager artifact 必須依這個有效 ``PY`` 判定（新 venv 的匯入結果），不
    再固定回報 wrapper 腳本自己所在的舊套件根；對照舊 receipt（在覆寫之前、
    仍是舊 venv 時寫入）不得宣稱 match。"""
    from paulsha_cortex.porcelain import _runtime_probe

    old_site = tmp_path / "old-venv"
    old_root = _write_fake_install(old_site, "old")
    new_prefix = tmp_path / "new-venv"
    new_root = _write_fake_install(new_prefix / "lib" / "python3.12" / "site-packages", "new")
    new_python = new_prefix / "bin" / "python3.12"

    home, manager_unit_path = _setup_manager_unit(tmp_path)
    manager_script = old_root / "scripts" / "service-manager.sh"

    show_output = "\n".join(
        (
            _manager_block(
                "test-manager.service",
                manager_unit_path,
                exec_start_argv=f"/usr/bin/env bash {manager_script}",
                environment=f"PY={new_python}",
                main_pid=321,
            ),
            _not_found_block("test-manager.timer"),
            _not_found_block("test-monitor.service"),
        )
    )
    _fake_systemctl(monkeypatch, show_output)

    probe = _runtime_probe.probe_service_runtime("test", home=home)
    manager_declared = probe["service_declaration"]["manager"]

    new_artifact = artifact_identity_from_python(str(new_python))
    old_artifact = artifact_identity_from_package_root(old_root)
    assert new_artifact["kind"] == "installed-wheel"
    assert manager_declared["artifact"]["sha256"] == new_artifact["sha256"]
    assert manager_declared["artifact"]["sha256"] != old_artifact["sha256"]
    assert str(new_root) not in str(probe)

    # 舊 receipt：manager 上次啟動時仍是舊 venv，pid 恰好與這次探測相同。
    state_root = tmp_path / "manager-runtime"
    record_runtime_startup(
        service="manager",
        instance="test",
        state_root=state_root,
        configuration={"revision": "unchanged"},
        artifact=old_artifact,
        started_at="2026-09-26T00:00:00Z",
        pid=321,
    )
    receipt_state = inspect_runtime_state(state_root, service="manager", instance="test")
    comparison = compare_runtime_state(
        receipt_state,
        current_artifact=manager_declared["artifact"],
        declared_config_revision=configuration_revision({"revision": "unchanged"}),
        expected_pid=321,
        require_process_match=True,
    )

    assert comparison["artifact_status"] == "drift"
    assert comparison["status"] != "match"


def test_manager_wrapper_without_py_override_still_resolves_via_package_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既有／多數部署的實際狀態：``PY`` 未被任何一層（``Environment=``／
    ``EnvironmentFiles=``／ExecStart 內 ``env`` 前綴）宣告，wrapper 退回
    ``command -v python3``。這個預設無法安全驗證，維持既有假設（wrapper 與
    套件同根），不能因為新增 ``PY`` 判定就整體退化成 unknown。"""
    from paulsha_cortex.porcelain import _runtime_probe

    site = tmp_path / "venv"
    package_root = _write_fake_install(site, "solo")
    home, manager_unit_path = _setup_manager_unit(tmp_path)
    manager_script = package_root / "scripts" / "service-manager.sh"

    show_output = "\n".join(
        (
            _manager_block(
                "test-manager.service",
                manager_unit_path,
                exec_start_argv=f"/usr/bin/env bash {manager_script}",
                environment="",
                main_pid=321,
            ),
            _not_found_block("test-manager.timer"),
            _not_found_block("test-monitor.service"),
        )
    )
    _fake_systemctl(monkeypatch, show_output)

    probe = _runtime_probe.probe_service_runtime("test", home=home)
    manager_declared = probe["service_declaration"]["manager"]

    expected = artifact_identity_from_package_root(package_root)
    assert manager_declared["artifact"]["sha256"] == expected["sha256"]
    assert manager_declared["artifact"]["kind"] == "installed-wheel"


def test_manager_wrapper_py_conflict_between_environment_and_file_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Environment=`` 與 ``EnvironmentFiles=`` 宣告了互相衝突的 ``PY``
    時（例如兩個 drop-in 疊加打架），無法安全判定實際會用哪一個，必須回報
    unknown，不能挑一邊臆測。"""
    from paulsha_cortex.porcelain import _runtime_probe

    site = tmp_path / "venv"
    package_root = _write_fake_install(site, "solo")
    home, manager_unit_path = _setup_manager_unit(tmp_path)
    manager_script = package_root / "scripts" / "service-manager.sh"

    env_file = tmp_path / "manager-dropin.env"
    env_file.write_text("PY=/file-venv/bin/python\n", encoding="utf-8")

    show_output = "\n".join(
        (
            _manager_block(
                "test-manager.service",
                manager_unit_path,
                exec_start_argv=f"/usr/bin/env bash {manager_script}",
                environment="PY=/environment-directive-venv/bin/python",
                environment_files=(str(env_file),),
                main_pid=321,
            ),
            _not_found_block("test-manager.timer"),
            _not_found_block("test-monitor.service"),
        )
    )
    _fake_systemctl(monkeypatch, show_output)

    probe = _runtime_probe.probe_service_runtime("test", home=home)
    manager_declared = probe["service_declaration"]["manager"]

    assert manager_declared["artifact"]["kind"] == "unknown"
    assert manager_declared["artifact"]["sha256"] is None


def test_service_status_env_summary_reflects_effective_dropin_not_stale_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cortex service status`` 的 ``env``（executor／interval_seconds／
    specs_dir）顯示欄位在 manager 的有效環境來源確認是 ``systemd-effective``
    時，必須改用有效宣告，不能繼續固定讀
    ``~/.agents/core/runtime/<instance>-manager.env``——那份檔案在這裡故意寫
    成跟有效宣告不同的舊值，證明顯示欄位真的換了來源，不是恰好一致。"""
    from paulsha_cortex.porcelain import service as service_porcelain

    site = tmp_path / "venv"
    package_root = _write_fake_install(site, "solo")
    home, manager_unit_path = _setup_manager_unit(tmp_path)
    manager_script = package_root / "scripts" / "service-manager.sh"
    monkeypatch.setenv("HOME", str(home))

    stale_specs_dir = tmp_path / "stale-specs"
    stale_specs_dir.mkdir()
    runtime_dir = home / ".agents" / "core" / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "test-manager.env").write_text(
        "PY=/stale/python\n"
        "PSC_MANAGER_INTERVAL_SECONDS=60\n"
        f"PSC_MANAGER_SPECS_DIR={stale_specs_dir}\n",
        encoding="utf-8",
    )

    effective_python = tmp_path / "effective-venv" / "bin" / "python3.12"
    effective_specs_dir = tmp_path / "effective-specs"
    environment = (
        f"PY={effective_python} "
        "PSC_MANAGER_INTERVAL_SECONDS=777 "
        f"PSC_MANAGER_SPECS_DIR={effective_specs_dir}"
    )
    show_output = "\n".join(
        (
            _manager_block(
                "test-manager.service",
                manager_unit_path,
                exec_start_argv=f"/usr/bin/env bash {manager_script}",
                environment=environment,
                main_pid=321,
            ),
            _not_found_block("test-manager.timer"),
            _not_found_block("test-monitor.service"),
        )
    )
    _fake_systemctl(monkeypatch, show_output)

    payload = service_porcelain._status_payload("test")

    assert payload["mode"] == "systemd"
    assert payload["env"]["executor"] == str(effective_python)
    assert payload["env"]["interval_seconds"] == 777
    assert payload["env"]["specs_dir"] == str(effective_specs_dir)
    assert payload["env"]["executor"] != "/stale/python"
    assert payload["env"]["interval_seconds"] != 60
    assert payload["env"]["specs_dir"] != str(stale_specs_dir)


def test_doctor_loaded_runtime_manager_wrapper_py_dropin_does_not_report_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``doctor`` 的 loaded-runtime 判定與 ``cortex service status`` 共用同一套
    ``service_declaration_projection``／``_declared_service_artifact``：manager
    wrapper 被 drop-in 覆寫 ``PY`` 指向另一個 venv 時，doctor 也不能宣稱 manager
    仍是 match 舊 receipt。"""
    from paulsha_cortex import doctor as doctor_module
    from paulsha_cortex.porcelain import _runtime_probe

    old_site = tmp_path / "old-venv"
    old_root = _write_fake_install(old_site, "old")
    new_prefix = tmp_path / "new-venv"
    _write_fake_install(new_prefix / "lib" / "python3.12" / "site-packages", "new")
    new_python = new_prefix / "bin" / "python3.12"

    home, manager_unit_path = _setup_manager_unit(tmp_path)
    manager_script = old_root / "scripts" / "service-manager.sh"
    coordinator_root = tmp_path / "coordinator-root"

    environment = f"PY={new_python} PSC_COORDINATOR_ROOT={coordinator_root}"
    show_output = "\n".join(
        (
            _manager_block(
                "test-manager.service",
                manager_unit_path,
                exec_start_argv=f"/usr/bin/env bash {manager_script}",
                environment=environment,
                main_pid=321,
            ),
            _not_found_block("test-manager.timer"),
            _not_found_block("test-monitor.service"),
        )
    )
    _fake_systemctl(monkeypatch, show_output)

    old_artifact = artifact_identity_from_package_root(old_root)
    record_runtime_startup(
        service="manager",
        instance="test",
        state_root=coordinator_root,
        configuration={"PSC_COORDINATOR_ROOT": str(coordinator_root)},
        artifact=old_artifact,
        started_at="2026-09-26T00:00:00Z",
        pid=321,
    )

    probe_result = doctor_module._loaded_runtime_probe(
        instance="test", environment={"HOME": str(home)}
    )

    manager_report = probe_result.context["manager"]
    assert manager_report["comparison"]["artifact_status"] == "drift"
    assert manager_report["status"] != "match"
