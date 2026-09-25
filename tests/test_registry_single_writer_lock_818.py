from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from paulsha_cortex.coordinator.registry import JobRegistry

WAIT_TIMEOUT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.05


def _seed_registry(state_path: Path, *slice_ids: str) -> str:
    registry = JobRegistry(state_path=state_path)
    for slice_id in slice_ids:
        registry.create_slice(
            slice_id=slice_id,
            spec_path=f"specs/{slice_id}.md",
            spec_hash=f"{slice_id}-spec-sha",
            plan_path=f"plans/{slice_id}.md",
            plan_hash=f"{slice_id}-plan-sha",
            target_branch=f"feature/{slice_id}",
            dispatch_base=f"{slice_id}-base-sha",
            builder_job_id=None,
            reviewer_job_id=None,
            candidate=None,
        )
    return hashlib.sha256(state_path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _wait_for_file(path: Path, proc: subprocess.Popen[str], *, label: str) -> Path:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if path.exists():
            return path
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            raise AssertionError(
                f"{label} exited before writing {path.name}: "
                f"code={proc.returncode}, stdout={stdout!r}, stderr={stderr!r}"
            )
        time.sleep(POLL_INTERVAL_SECONDS)
    proc.kill()
    stdout, stderr = proc.communicate()
    raise AssertionError(
        f"{label} timed out waiting for {path.name}: "
        f"stdout={stdout!r}, stderr={stderr!r}"
    )


def _wait_for_json_file(path: Path, proc: subprocess.Popen[str], *, label: str) -> dict[str, object]:
    return _read_json(_wait_for_file(path, proc, label=label))


def _spawn_worker(
    *,
    worker: str,
    state_path: Path,
    control_dir: Path,
    slice_id: str,
    action: str,
    actor: str,
    state: str | None = None,
    gate_state: str | None = None,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(REPO_ROOT)
        if not pythonpath
        else f"{REPO_ROOT}{os.pathsep}{pythonpath}"
    )
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--child",
        "--worker",
        worker,
        "--state-path",
        str(state_path),
        "--control-dir",
        str(control_dir),
        "--slice-id",
        slice_id,
        "--action",
        action,
        "--actor",
        actor,
    ]
    if state is not None:
        argv.extend(["--state", state])
    if gate_state is not None:
        argv.extend(["--gate-state", gate_state])
    return subprocess.Popen(
        argv,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _release_worker(control_dir: Path, worker: str) -> None:
    (control_dir / f"{worker}.go").write_text("go\n", encoding="utf-8")


def _finish_worker(
    proc: subprocess.Popen[str],
    result_path: Path,
    *,
    label: str,
) -> dict[str, object]:
    result = _wait_for_json_file(result_path, proc, label=label)
    stdout, stderr = proc.communicate()
    assert proc.returncode == 0, (
        f"{label} exited non-zero: code={proc.returncode}, stdout={stdout!r}, stderr={stderr!r}"
    )
    return result


def _slice_row(payload: dict[str, object], slice_id: str) -> dict[str, object]:
    for row in payload["slices"]:
        if row["slice_id"] == slice_id:
            return row
    raise AssertionError(f"slice {slice_id!r} not found in payload")


def test_different_slice_same_revision_writers_do_not_silently_lose_an_update(tmp_path: Path) -> None:
    state_path = tmp_path / "jobs.json"
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    starting_revision = _seed_registry(state_path, "slice-a", "slice-b")

    first = _spawn_worker(
        worker="writer-a",
        state_path=state_path,
        control_dir=control_dir,
        slice_id="slice-a",
        action="writer-a-started",
        actor="builder",
        state="running",
    )
    second = _spawn_worker(
        worker="writer-b",
        state_path=state_path,
        control_dir=control_dir,
        slice_id="slice-b",
        action="writer-b-started",
        actor="builder",
        state="running",
    )

    first_loaded = _wait_for_json_file(control_dir / "writer-a.loaded.json", first, label="writer-a")
    second_loaded = _wait_for_json_file(control_dir / "writer-b.loaded.json", second, label="writer-b")
    assert first_loaded["loaded_revision"] == starting_revision
    assert second_loaded["loaded_revision"] == starting_revision

    _release_worker(control_dir, "writer-a")
    first_result = _finish_worker(
        first, control_dir / "writer-a.result.json", label="writer-a"
    )
    _release_worker(control_dir, "writer-b")
    second_result = _finish_worker(
        second, control_dir / "writer-b.result.json", label="writer-b"
    )

    payload = _read_json(state_path)
    slice_a = _slice_row(payload, "slice-a")
    slice_b = _slice_row(payload, "slice-b")
    both_mutations_durable = (
        slice_a["state"] == "running"
        and slice_b["state"] == "running"
        and [entry["action"] for entry in slice_a["actions"]] == ["writer-a-started"]
        and [entry["action"] for entry in slice_b["actions"]] == ["writer-b-started"]
    )
    explicit_conflict = {
        first_result.get("error_type"),
        second_result.get("error_type"),
    } == {"RegistryRevisionConflict", None} or {
        first_result.get("error_type"),
        second_result.get("error_type"),
    } == {None, "RegistryRevisionConflict"}

    assert both_mutations_durable or explicit_conflict, (
        "two writers loaded the same revision for different slices, but the second writer "
        "silently overwrote the first mutation instead of preserving both mutations or "
        f"returning an explicit RegistryRevisionConflict: "
        f"first_result={first_result}, second_result={second_result}, "
        f"slice_a={slice_a}, slice_b={slice_b}"
    )


def test_same_slice_same_revision_second_writer_gets_conflict_not_success(tmp_path: Path) -> None:
    state_path = tmp_path / "jobs.json"
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    starting_revision = _seed_registry(state_path, "slice-a")

    first = _spawn_worker(
        worker="writer-a",
        state_path=state_path,
        control_dir=control_dir,
        slice_id="slice-a",
        action="builder-started",
        actor="builder",
        state="running",
    )
    second = _spawn_worker(
        worker="writer-b",
        state_path=state_path,
        control_dir=control_dir,
        slice_id="slice-a",
        action="operator-abandon",
        actor="operator",
        state="failed",
        gate_state="failed",
    )

    first_loaded = _wait_for_json_file(control_dir / "writer-a.loaded.json", first, label="writer-a")
    second_loaded = _wait_for_json_file(control_dir / "writer-b.loaded.json", second, label="writer-b")
    assert first_loaded["loaded_revision"] == starting_revision
    assert second_loaded["loaded_revision"] == starting_revision

    _release_worker(control_dir, "writer-a")
    first_result = _finish_worker(
        first, control_dir / "writer-a.result.json", label="writer-a"
    )
    _release_worker(control_dir, "writer-b")
    second_result = _finish_worker(
        second, control_dir / "writer-b.result.json", label="writer-b"
    )

    payload = _read_json(state_path)
    slice_a = _slice_row(payload, "slice-a")

    assert first_result["status"] == "ok"
    assert second_result.get("error_type") == "RegistryRevisionConflict", (
        "the second same-slice writer loaded a stale revision and must fail with an explicit "
        f"RegistryRevisionConflict instead of succeeding: second_result={second_result}, "
        f"raw_slice={slice_a}"
    )
    assert slice_a["state"] == "running"
    assert [entry["action"] for entry in slice_a["actions"]] == ["builder-started"]


def _wait_for_release(path: Path) -> None:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"timed out waiting for release signal {path}")


def _worker_main(args: argparse.Namespace) -> int:
    state_path = Path(args.state_path)
    control_dir = Path(args.control_dir)
    registry = JobRegistry(state_path=state_path)
    loaded_revision = hashlib.sha256(state_path.read_bytes()).hexdigest()
    _write_json(
        control_dir / f"{args.worker}.loaded.json",
        {
            "worker": args.worker,
            "loaded_revision": loaded_revision,
            "slice_id": args.slice_id,
        },
    )
    _wait_for_release(control_dir / f"{args.worker}.go")

    try:
        updated = registry.record_action(
            args.slice_id,
            action=args.action,
            actor=args.actor,
            state=args.state,
            gate_state=args.gate_state,
        )
    except Exception as exc:  # noqa: BLE001 - child must report exact exception surface.
        _write_json(
            control_dir / f"{args.worker}.result.json",
            {
                "worker": args.worker,
                "status": "error",
                "loaded_revision": loaded_revision,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            },
        )
        return 0

    _write_json(
        control_dir / f"{args.worker}.result.json",
        {
            "worker": args.worker,
            "status": "ok",
            "loaded_revision": loaded_revision,
            "state": updated["state"],
            "gate_state": updated["gate_state"],
            "actions": [entry["action"] for entry in updated["actions"]],
        },
    )
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--worker")
    parser.add_argument("--state-path")
    parser.add_argument("--control-dir")
    parser.add_argument("--slice-id")
    parser.add_argument("--action")
    parser.add_argument("--actor")
    parser.add_argument("--state")
    parser.add_argument("--gate-state")
    return parser.parse_args(argv)


if __name__ == "__main__":  # pragma: no cover
    parsed = _parse_args(sys.argv[1:])
    if parsed.child:
        raise SystemExit(_worker_main(parsed))
    raise SystemExit(pytest.main([str(Path(__file__).resolve())]))
