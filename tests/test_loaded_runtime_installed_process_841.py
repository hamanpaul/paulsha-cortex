"""#841 AC1／AC2／AC4：由隔離安裝 prefix 的已安裝程序產生 loaded receipt。

``test_runtime_attestation.py`` 的 receipt 產生測試是在 pytest 行程內從工作樹 import
``manager_daemon.main``／``monitor.main``（run_loop 為 fake），票面明文「只在工作樹
import 不算通過」。這裡把 checkout ``pip install`` 進隔離 venv，從 checkout 外的 cwd、
乾淨環境（無 PYTHONPATH、無 user site）啟動 **已安裝** 的長駐 Manager／Monitor 程序，
receipt 由它們自己寫下；再用同一 prefix 的已安裝 ``cortex service status --json``
（systemd 以 PATH 上的假 ``systemctl`` 回報這兩個程序的 MainPID 與有效宣告）比對
loaded↔installed。

同一組真程序再驗：磁碟 artifact／config 更新但程序未重啟 → drift 且 receipt 不被改寫；
受控重啟後新 receipt 對齊新 identity，舊 receipt bytes 不變。
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MANAGER_MODULE = "paulsha_cortex.coordinator.manager_daemon"
_MONITOR_MODULE = "paulsha_cortex.monitor"
_PROCESS_TIMEOUT_SECONDS = 60.0


@pytest.fixture(scope="module")
def installed_prefix(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """把 checkout 以 ``pip install --no-deps`` 裝進隔離 venv（不是 editable、不靠
    PYTHONPATH）。"""

    import yaml

    root = tmp_path_factory.mktemp("installed-prefix-841")
    prefix = root / "venv"
    created = subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(prefix)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert created.returncode == 0, created.stderr
    python = prefix / "bin" / "python"
    # 與 test_runtime_attestation 的隔離安裝測試同一判斷：看得到 build backend 才離線
    # --no-build-isolation，否則交給 pip build isolation。
    backend_probe = subprocess.run(
        [str(python), "-c", "import setuptools.build_meta"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    isolation_args = ["--no-build-isolation"] if backend_probe.returncode == 0 else []
    installed = subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", *isolation_args, str(_PROJECT_ROOT)],
        cwd=root,
        env={
            **os.environ,
            "PIP_CACHE_DIR": str(root / "pip-cache"),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert installed.returncode == 0, installed.stderr
    site_packages = next(prefix.glob("lib/python*/site-packages"))
    # 長駐程序以乾淨環境啟動（PYTHONNOUSERSITE、無 PYTHONPATH）：唯一的第三方相依
    # PyYAML 直接放進這個 prefix，讓 service 宣告不必帶 PYTHONPATH 也能執行。
    if not (site_packages / "yaml").exists():
        shutil.copytree(Path(yaml.__file__).resolve().parent, site_packages / "yaml")
    return SimpleNamespace(
        prefix=prefix,
        python=python,
        cli=prefix / "bin" / "cortex",
        package_root=site_packages / "paulsha_cortex",
    )


def _tree_sha256(package_root: Path) -> str:
    """獨立於受測程式計算已安裝套件樹的內容摘要（排除 bytecode）。"""

    rows: list[list[str]] = []
    for path in package_root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        rows.append(
            [
                path.relative_to(package_root).as_posix(),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            ]
        )
    rows.sort()
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class _Deployment:
    """一個 user 級 instance：unit 宣告、有效環境、長駐程序與假 systemctl。"""

    def __init__(self, tmp_path: Path, socket_dir: Path, installed: SimpleNamespace) -> None:
        self.installed = installed
        self.tmp_path = tmp_path
        self.home = tmp_path / "home"
        self.units_dir = self.home / ".config" / "systemd" / "user"
        self.units_dir.mkdir(parents=True)
        self.outside = tmp_path / "outside-checkout"
        self.outside.mkdir()
        self.fakebin = tmp_path / "fakebin"
        self.fakebin.mkdir()
        agents = tmp_path / "agents"
        self.coordinator_root = agents / "coordinator"
        self.monitor_root = agents / "monitor"
        self.specs_dir = tmp_path / "specs-must-not-be-echoed"
        (tmp_path / "workspace").mkdir()
        self.monitor_config = tmp_path / "config" / "project-cortex.yaml"
        self.monitor_config.parent.mkdir()
        self.socket_path = socket_dir / "monitor.sock"
        self.write_monitor_config(poll_interval_seconds=60)
        # 服務只看得到 unit 宣告的 PSC_*；假 systemctl 回報的 Environment= 與此逐字相同。
        self.service_environment = {
            "PSC_INSTANCE": "cortex",
            "PSC_AGENTS_ROOT": str(agents),
            "PSC_COORDINATOR_ROOT": str(self.coordinator_root),
            "PSC_MONITOR_STATE_ROOT": str(self.monitor_root),
            "PSC_MONITOR_CONFIG": str(self.monitor_config),
            "PSC_CONFIG_ROOT": str(tmp_path / "operator-config"),
            "PSC_TRUST_ROOT_SELFCHECK": "off",
        }
        # 任何 gh 呼叫都在本機失敗，不出網。
        gh = self.fakebin / "gh"
        gh.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        gh.chmod(0o755)
        self.show_output = tmp_path / "systemctl-show.txt"
        systemctl = self.fakebin / "systemctl"
        systemctl.write_text(
            f"#!/bin/sh\nexec cat {shlex.quote(str(self.show_output))}\n", encoding="utf-8"
        )
        systemctl.chmod(0o755)
        self.base_environment = {
            "PATH": f"{self.fakebin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
        }
        self.manager_argv = [
            str(installed.python),
            "-m",
            _MANAGER_MODULE,
            "--tick-interval",
            "3600",
            "--poll-interval",
            "0.2",
            "--specs-dir",
            str(self.specs_dir),
        ]
        self.monitor_argv = [str(installed.python), "-m", _MONITOR_MODULE]
        self.processes: dict[str, subprocess.Popen] = {}
        self._logs: list[object] = []

    def write_monitor_config(self, *, poll_interval_seconds: int) -> None:
        self.monitor_config.write_text(
            "workspaces:\n"
            "  - name: test\n"
            f"    path: {self.tmp_path / 'workspace'}\n"
            "monitor:\n"
            f"  poll_interval_seconds: {poll_interval_seconds}\n"
            "  github_refresh_interval_seconds: 300\n"
            f"  socket_path: {self.socket_path}\n",
            encoding="utf-8",
        )

    def start(self, service: str) -> subprocess.Popen:
        argv = self.manager_argv if service == "manager" else self.monitor_argv
        log = open(self.tmp_path / f"{service}-{len(self._logs)}.log", "wb")
        self._logs.append(log)
        process = subprocess.Popen(
            argv,
            cwd=self.outside,
            env={**self.base_environment, **self.service_environment},
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self.processes[service] = process
        return process

    def wait_ready(self, service: str, *, receipts: int) -> list[Path]:
        """等到 service 自己寫完第 ``receipts`` 份 receipt，且已進入主迴圈。"""

        process = self.processes[service]
        root = self.coordinator_root if service == "manager" else self.monitor_root
        ready_marker = (
            self.coordinator_root.parent / "control" / "status.json"
            if service == "manager"
            else self.socket_path
        )
        directory = root / "runtime-attestations"
        deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(
                    f"installed {service} exited early: rc={process.returncode}\n"
                    f"{self.log_text()}"
                )
            exists = directory.is_dir()
            files = sorted(directory.glob(f"{service}-*.json")) if exists else []
            pending = list(directory.glob(".runtime-attestation-*.tmp")) if exists else []
            if len(files) >= receipts and not pending and ready_marker.exists():
                return files
            time.sleep(0.05)
        pytest.fail(f"installed {service} did not become ready\n{self.log_text()}")

    def stop(self, service: str) -> None:
        process = self.processes.pop(service)
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=_PROCESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            pytest.fail(f"installed {service} ignored SIGTERM\n{self.log_text()}")

    def close(self) -> None:
        for service in list(self.processes):
            process = self.processes.pop(service)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        for log in self._logs:
            log.close()

    def log_text(self) -> str:
        return "\n".join(
            path.read_text(encoding="utf-8", errors="replace")[-4000:]
            for path in sorted(self.tmp_path.glob("*.log"))
        )

    def declare_units(self) -> None:
        """寫 unit 檔，並讓假 systemctl 以真實 ``systemctl show`` 形狀回報兩個長駐程序。"""

        blocks: list[str] = []
        for service, argv in (("manager", self.manager_argv), ("monitor", self.monitor_argv)):
            unit = f"cortex-{service}.service"
            fragment = self.units_dir / unit
            environment_lines = "".join(
                f"Environment={key}={value}\n"
                for key, value in sorted(self.service_environment.items())
            )
            fragment.write_text(
                "[Service]\n"
                f"WorkingDirectory={self.outside}\n"
                f"{environment_lines}"
                f"ExecStart={shlex.join(argv)}\n",
                encoding="utf-8",
            )
            exec_start = (
                f"{{ path={argv[0]} ; argv[]={shlex.join(argv)} ; ignore_errors=no ; "
                "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
            )
            environment = " ".join(
                shlex.quote(f"{key}={value}")
                for key, value in sorted(self.service_environment.items())
            )
            # 依真實輸出順序：Id 在區塊中間。
            blocks.append(
                f"ExecStart={exec_start}\n"
                f"Environment={environment}\n"
                f"WorkingDirectory={self.outside}\n"
                f"MainPID={self.processes[service].pid}\n"
                f"Id={unit}\n"
                "LoadState=loaded\nActiveState=active\nSubState=running\n"
                f"FragmentPath={fragment}\n"
                "DropInPaths=\nEnvironmentFiles=\n"
            )
        self.show_output.write_text("\n".join(blocks), encoding="utf-8")

    def service_status(self) -> tuple[dict, str]:
        self.declare_units()
        result = subprocess.run(
            [str(self.installed.cli), "service", "status", "--json"],
            cwd=self.outside,
            env=dict(self.base_environment),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout), result.stdout


@pytest.fixture
def deployment(tmp_path: Path, socket_dir: Path, installed_prefix: SimpleNamespace):
    scaffold = _Deployment(tmp_path, socket_dir, installed_prefix)
    try:
        yield scaffold
    finally:
        scaffold.close()


def _read_receipt(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_installed_manager_and_monitor_write_receipts_matched_by_installed_cli(
    deployment: _Deployment, installed_prefix: SimpleNamespace
) -> None:
    installed_digest = _tree_sha256(installed_prefix.package_root)
    version = (_PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    manager = deployment.start("manager")
    monitor = deployment.start("monitor")
    (manager_receipt_path,) = deployment.wait_ready("manager", receipts=1)
    (monitor_receipt_path,) = deployment.wait_ready("monitor", receipts=1)

    # receipt 由已安裝程序寫下：artifact 是 prefix 內的 wheel，不是 checkout。
    for receipt_path, process, root in (
        (manager_receipt_path, manager, deployment.coordinator_root),
        (monitor_receipt_path, monitor, deployment.monitor_root),
    ):
        receipt = _read_receipt(receipt_path)
        assert receipt["pid"] == process.pid
        assert receipt["instance"] == "cortex"
        assert receipt["instance_root_sha256"] == hashlib.sha256(os.fsencode(str(root))).hexdigest()
        assert receipt["artifact"] == {
            "kind": "installed-wheel",
            "package": "paulsha-cortex",
            "package_version": version,
            # 從目錄安裝的 wheel 沒有 VCS commit：維持 unknown，不回頭讀 checkout 猜測。
            "source_revision": "unknown",
            "sha256": installed_digest,
        }
        assert receipt["trust_root"] == {
            "status": "unknown",
            "reason": "install-receipt-unconfigured",
        }
    manager_components = _read_receipt(manager_receipt_path)["config"]["components"]
    assert set(manager_components) == {
        "effective_revision",
        "environment_revision",
        "invocation_revision",
    }

    payload, raw = deployment.service_status()

    runtime = payload["service"]["loaded_runtime"]
    assert payload["service"]["mode"] == "systemd"
    assert runtime["operator_cli"]["artifact"]["kind"] == "installed-wheel"
    assert runtime["operator_cli"]["artifact"]["sha256"] == installed_digest
    for service, process, receipt_path in (
        ("manager", manager, manager_receipt_path),
        ("monitor", monitor, monitor_receipt_path),
    ):
        declaration = runtime["service_declaration"][service]
        assert declaration["environment_source"] == "systemd-effective"
        assert declaration["artifact"]["kind"] == "installed-wheel"
        assert declaration["artifact"]["sha256"] == installed_digest
        assert declaration["pid"] == process.pid
        report = runtime[service]
        assert report["status"] == "match", report
        assert report["comparison"]["artifact_status"] == "match"
        assert report["comparison"]["config_status"] == "match"
        assert report["comparison"]["process_status"] == "match"
        assert report["comparison"]["transition_safe"] is False
        assert report["loaded"]["receipt_id"] == _read_receipt(receipt_path)["receipt_id"]
        assert report["loaded"]["pid"] == process.pid
        assert report["loaded"]["artifact"]["sha256"] == installed_digest
        assert report["installed_artifact"]["sha256"] == installed_digest
    assert runtime["manager"]["comparison"]["config_components"] == {
        "environment_revision": "match",
        "invocation_revision": "match",
    }
    assert runtime["monitor"]["comparison"]["config_components"]["effective_revision"] == "match"
    # 只輸出摘要：argv 內的路徑值與 service 環境值都不回顯。
    assert str(deployment.specs_dir) not in raw
    assert str(deployment.monitor_config) not in raw


def test_installed_processes_show_disk_drift_until_controlled_restart(
    deployment: _Deployment, installed_prefix: SimpleNamespace
) -> None:
    original_digest = _tree_sha256(installed_prefix.package_root)
    deployment.start("manager")
    deployment.start("monitor")
    (manager_first,) = deployment.wait_ready("manager", receipts=1)
    (monitor_first,) = deployment.wait_ready("monitor", receipts=1)
    first_bytes = {path: path.read_bytes() for path in (manager_first, monitor_first)}
    baseline, _raw = deployment.service_status()
    assert baseline["service"]["loaded_runtime"]["manager"]["status"] == "match"
    assert baseline["service"]["loaded_runtime"]["monitor"]["status"] == "match"

    # 磁碟上的安裝 artifact 更新、Monitor 設定檔改動，但兩個程序都沒有重啟。
    upgrade_marker = installed_prefix.package_root / "_upgrade_marker_841.py"
    try:
        upgrade_marker.write_text("UPGRADED = True\n", encoding="utf-8")
        upgraded_digest = _tree_sha256(installed_prefix.package_root)
        assert upgraded_digest != original_digest
        deployment.write_monitor_config(poll_interval_seconds=45)

        drift, _raw = deployment.service_status()
        runtime = drift["service"]["loaded_runtime"]
        for service in ("manager", "monitor"):
            report = runtime[service]
            assert report["status"] == "drift", report
            assert report["comparison"]["artifact_status"] == "drift"
            assert report["loaded"]["artifact"]["sha256"] == original_digest
            assert report["installed_artifact"]["sha256"] == upgraded_digest
        assert runtime["manager"]["comparison"]["config_status"] == "match"
        # 只改 config file 不冒充已 reload：宣告值變了，loaded revision 仍是啟動時的值。
        assert runtime["monitor"]["comparison"]["config_status"] == "drift"
        assert runtime["monitor"]["reason"] == "config-drift"
        assert (
            runtime["monitor"]["effective_config_revision"]
            == baseline["service"]["loaded_runtime"]["monitor"]["effective_config_revision"]
        )
        manager_directory = deployment.coordinator_root / "runtime-attestations"
        monitor_directory = deployment.monitor_root / "runtime-attestations"
        assert sorted(manager_directory.glob("manager-*.json")) == [manager_first]
        assert sorted(monitor_directory.glob("monitor-*.json")) == [monitor_first]
        assert {path: path.read_bytes() for path in first_bytes} == first_bytes

        # 受控重啟：新程序載入新 artifact／config，寫新 receipt；舊 receipt 不改寫。
        old_pids = {}
        for service in ("manager", "monitor"):
            old_pids[service] = deployment.processes[service].pid
            deployment.stop(service)
            deployment.start(service)
        manager_receipts = deployment.wait_ready("manager", receipts=2)
        monitor_receipts = deployment.wait_ready("monitor", receipts=2)
        assert manager_first in manager_receipts and monitor_first in monitor_receipts

        restarted, _raw = deployment.service_status()
        runtime = restarted["service"]["loaded_runtime"]
        for service, first in (("manager", manager_first), ("monitor", monitor_first)):
            report = runtime[service]
            assert report["status"] == "match", report
            assert report["loaded"]["pid"] == deployment.processes[service].pid
            assert report["loaded"]["pid"] != old_pids[service]
            assert report["loaded"]["artifact"]["sha256"] == upgraded_digest
            previous = report["previous_process_start"]
            assert previous["receipt_id"] == _read_receipt(first)["receipt_id"]
            assert report["loaded"]["process_id"] != report["previous_process_start"]["process_id"]
        assert (
            runtime["monitor"]["effective_config_revision"]
            != baseline["service"]["loaded_runtime"]["monitor"]["effective_config_revision"]
        )
        assert {path: path.read_bytes() for path in first_bytes} == first_bytes
    finally:
        upgrade_marker.unlink(missing_ok=True)
