from __future__ import annotations

import sys
from pathlib import Path

from paulsha_cortex.coordinator import gate_ledger
from paulsha_cortex.coordinator.launcher import build_wrapper_script
from paulsha_cortex.coordinator.preflight import _preflight_environment


PYTHON_PATH_ENVIRONMENT = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
)


def test_gate_cannot_import_fixture_available_only_from_daemon_pin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pin = tmp_path / "pin"
    worktree = tmp_path / "worktree"
    pin.mkdir()
    worktree.mkdir()
    (pin / "pythonpath_fixture.py").write_text("VALUE = 'pin'\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(pin))

    rows = gate_ledger.run_gates(
        [
            gate_ledger.GateSpec(
                "pytest",
                (
                    sys.executable,
                    "-c",
                    "import pythonpath_fixture; assert pythonpath_fixture.VALUE == 'worktree'",
                ),
            )
        ],
        worktree=worktree,
    )

    assert rows[0]["status"] == "failed"
    assert rows[0]["exit_code"] == 1
    assert "ModuleNotFoundError" in rows[0]["detail"]
    assert "pythonpath_fixture" in rows[0]["detail"]


def test_gate_subprocess_drops_python_startup_and_path_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    for name in PYTHON_PATH_ENVIRONMENT:
        monkeypatch.setenv(name, f"/operator/{name.lower()}")
    observed: dict[str, str] = {}

    def runner(argv, **kwargs):
        observed.update(kwargs["env"])
        import subprocess

        return subprocess.CompletedProcess(argv, 0, "", "")

    rows = gate_ledger.run_gates(
        [gate_ledger.GateSpec("pytest", (sys.executable, "-c", "pass"))],
        worktree=tmp_path,
        runner=runner,
    )

    assert rows[0]["status"] == "passed"
    assert not any(name in observed for name in PYTHON_PATH_ENVIRONMENT)
    assert observed["PATH"]


def test_gate_wrapper_keeps_runtime_import_path_but_drops_other_python_overrides(
    tmp_path: Path,
) -> None:
    script = build_wrapper_script(
        inner_argv=["true"],
        sentinel=str(tmp_path / "job.exit"),
        ledger=str(tmp_path / "job.gates.json"),
        worktree=str(tmp_path),
        repo_root="/runtime/pin",
        run_gates=True,
    )

    assert (
        "env -u PYTHONHOME -u PYTHONSTARTUP -u PYTHONUSERBASE "
        "PYTHONPATH=/runtime/pin python3 -m "
        "paulsha_cortex.coordinator.gate_ledger"
    ) in script


def test_preflight_environment_drops_python_startup_and_path_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    for name in PYTHON_PATH_ENVIRONMENT:
        monkeypatch.setenv(name, f"/operator/{name.lower()}")

    environment = _preflight_environment(disposable_home=tmp_path)

    assert not any(name in environment for name in PYTHON_PATH_ENVIRONMENT)
    assert environment["HOME"] == str(tmp_path)
