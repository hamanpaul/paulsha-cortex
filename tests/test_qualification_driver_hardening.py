from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "qualification" / "driver.py"


def _load_driver():
    spec = importlib.util.spec_from_file_location(
        "qualification_driver_hardening", DRIVER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _result(driver, argv, *, stdout="", stderr="", returncode=0):
    return driver.CommandResult(tuple(argv), returncode, stdout, stderr)


def test_manager_github_probe_uses_only_installed_manager_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    home = tmp_path / "manager-home"
    home.mkdir()
    gitconfig = home / ".gitconfig"
    gitconfig.write_text(
        "[credential]\n\thelper = !/usr/bin/gh auth git-credential\n",
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    monkeypatch.setattr(
        driver.pwd,
        "getpwnam",
        lambda _account: SimpleNamespace(pw_dir=str(home), pw_uid=os.getuid()),
    )
    monkeypatch.setattr(
        driver, "_require_installed_manager_gitconfig", lambda path: None
    )
    calls: list[tuple[tuple[str, ...], str | None, dict[str, str]]] = []
    refs = "a" * 40 + "\trefs/heads/main\n"

    def fake_run(argv, *, user=None, env=None, timeout=120):
        command = tuple(argv)
        calls.append((command, user, dict(env or {})))
        if "config" in command:
            return _result(
                driver,
                command,
                stdout=f"global\tfile:{gitconfig}\t!/usr/bin/gh auth git-credential\n",
            )
        if command[:2] == ("/usr/bin/python3", "-c"):
            return _result(driver, command, stdout="credential-ok\n")
        if "ls-remote" in command:
            return _result(driver, command, stdout=refs)
        return _result(driver, command)

    monkeypatch.setattr(driver, "_run", fake_run)
    driver._manager_github_probe("owner/repo", "b" * 40, evidence, source_repo=repo)

    assert calls
    assert all(user == "cortex-manager" for _argv, user, _env in calls)
    assert all(env["HOME"] == str(home) for _argv, _user, env in calls)
    push = next(argv for argv, _user, _env in calls if "push" in argv)
    assert "-c" not in push
    assert not any("credential.helper" in value for value in push)
    assert any(argv[:2] == ("/usr/bin/python3", "-c") for argv, _user, _env in calls)
    payload = json.loads((evidence / "manager-github-auth.json").read_text())
    assert payload["remote_refs_unchanged"] is True
    assert payload["before_sha256"] == payload["after_sha256"]


@pytest.mark.parametrize(
    "config_output",
    [
        "global\tfile:/installed/.gitconfig\t!/usr/bin/gh auth git-credential\n"
        "local\tfile:/repo/.git/config\tstore\n",
        "global\tfile:/wrong/.gitconfig\t!/usr/bin/gh auth git-credential\n",
        "global\tfile:/installed/.gitconfig\tstore\n",
    ],
)
def test_manager_github_probe_rejects_ambiguous_or_uninstalled_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_output: str
) -> None:
    driver = _load_driver()
    home = Path("/installed")
    monkeypatch.setattr(
        driver.pwd,
        "getpwnam",
        lambda _account: SimpleNamespace(pw_dir=str(home), pw_uid=os.getuid()),
    )
    monkeypatch.setattr(
        driver, "_require_installed_manager_gitconfig", lambda path: None
    )

    def fake_run(argv, **_kwargs):
        if "config" in argv:
            return _result(driver, argv, stdout=config_output)
        return _result(driver, argv)

    monkeypatch.setattr(driver, "_run", fake_run)
    with pytest.raises(driver.QualificationFailure, match="credential helper"):
        driver._manager_github_probe(
            "owner/repo", "b" * 40, tmp_path, source_repo=tmp_path
        )


def test_manager_github_probe_does_not_emit_credential_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    home = Path("/installed")
    monkeypatch.setattr(
        driver.pwd,
        "getpwnam",
        lambda _account: SimpleNamespace(pw_dir=str(home), pw_uid=os.getuid()),
    )
    monkeypatch.setattr(
        driver, "_require_installed_manager_gitconfig", lambda path: None
    )

    def fake_run(argv, **_kwargs):
        if "config" in argv:
            return _result(
                driver,
                argv,
                stdout=(
                    "global\tfile:/installed/.gitconfig\t"
                    "!/usr/bin/gh auth git-credential\n"
                ),
            )
        if tuple(argv[:2]) == ("/usr/bin/python3", "-c"):
            return _result(driver, argv, stdout="username=manager\npassword=SECRET\n")
        return _result(driver, argv)

    monkeypatch.setattr(driver, "_run", fake_run)
    with pytest.raises(driver.QualificationFailure, match="credential probe"):
        driver._manager_github_probe(
            "owner/repo", "b" * 40, tmp_path, source_repo=tmp_path
        )
    assert not (tmp_path / "manager-github-auth.json").exists()


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write_json(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _canonical(value)
    path.write_bytes(content)
    path.chmod(0o600)
    return hashlib.sha256(content).hexdigest()


def _dispatch_fixture(tmp_path: Path, driver):
    coordinator = tmp_path / "coordinator"
    repo = tmp_path / "repo"
    repo.mkdir()
    candidate = "a" * 40
    run_id = "run-qualification"
    work_id = "qualification-work"
    repository = "owner/repo"
    issue = 42
    phases = ("claim", "define", "plan", "build", "verify", "review", "ship")
    steps = [
        {"phase": phase, "card": f"{phase}-card", "gate_result": "passed"}
        for phase in phases
    ]
    jobs = []
    artifacts: list[Path] = []
    for phase in ("plan", "build", "verify", "review", "ship"):
        job_id = f"{phase}-job"
        job = {
            "job_id": job_id,
            "status": "exited",
            "exit_code": 0,
            "workflow_run_id": run_id,
            "workflow_claim_key": "claim:v1:" + "1" * 64,
            "workflow_repo": repository,
            "workflow_card": f"{phase}-card",
            "workflow_phase": phase,
            "workflow_repo_root": str(repo),
            "workflow_inputs": [],
            "workflow_outputs": [],
            "workflow_output_baseline": [],
            "source_revision": "2" * 64,
            "subject_head": candidate if phase != "plan" else None,
            "worktree": str(tmp_path / "reclaimed" / job_id),
            "template_instance": job_id,
        }
        envelope = {
            "schema_version": 1,
            "kind": phase,
            "job": {
                "job_id": job_id,
                "run_id": run_id,
                "claim_key": job["workflow_claim_key"],
                "repo": repository,
                "source_revision": job["source_revision"],
                "card_id": job["workflow_card"],
                "phase": phase,
                "inputs": [],
                "outputs": [],
                "output_baseline": [],
            },
            "payload": {
                "schema_version": 1,
                "kind": (
                    "workflow-review-result" if phase == "review" else "workflow-card"
                ),
                "status": "passed",
                "run_id": run_id,
                "card_id": job["workflow_card"],
                "candidate": candidate,
                "outputs": [],
                **(
                    {
                        "state": "passed",
                        "builder_job_id": "build-job",
                        "reviewer_job_id": job_id,
                    }
                    if phase == "review"
                    else {}
                ),
            },
            "artifacts": [],
        }
        evidence_path = coordinator / "evidence" / "workflow" / f"{job_id}.json"
        evidence_hash = _write_json(evidence_path, envelope)
        artifacts.append(evidence_path)
        job["workflow_evidence"] = {
            "kind": phase,
            "path": evidence_path.relative_to(coordinator).as_posix(),
            "hash": evidence_hash,
        }
        if phase != "ship":
            control = coordinator / "control" / f"{job_id}.log"
            job["control_log_path"] = str(control)
            ledger = control.with_name(f"{control.stem}.gates.json")
            _write_json(
                ledger,
                {
                    "schema_version": 1,
                    "kind": "workflow-gate-ledger",
                    "slice_id": job_id,
                    "gates": [],
                },
            )
            artifacts.append(ledger)
        jobs.append(job)

    bundle = coordinator / "commit-spool" / "build-job" / "commits.bundle"
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(b"real git bundle placeholder")
    bundle.parent.chmod(0o500)
    artifacts.append(bundle)

    completion = {
        "schema_version": 1,
        "slice_id": work_id,
        "candidate": candidate,
        "work_authority": {
            "repo": repository,
            "work_id": work_id,
            "run_id": run_id,
            "mapped_issues": [issue],
            "merge_commit": "b" * 40,
        },
    }
    completion_path = coordinator / "evidence" / "completion" / "completion.json"
    _write_json(completion_path, completion)
    completion_hash = hashlib.sha256(
        json.dumps(completion, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    artifacts.append(completion_path)
    workflow = {
        "run_id": run_id,
        "work_id": work_id,
        "repo": repository,
        "workspace_root": str(repo),
        "current_phase": "ship",
        "steps": steps,
        "issue_refs": [f"{repository}#{issue}"],
        "evidence_refs": [
            str((coordinator / "evidence" / "workflow" / f"{phase}-job.json"))
            for phase in ("plan", "build", "verify", "review", "ship")
        ],
        "gate_refs": [
            {"kind": "foreign-review", "ref": "review", "sha256": "3" * 64},
            {"kind": "copilot", "ref": "delivery", "sha256": "4" * 64},
        ],
        "candidate_head": candidate,
        "verified_head": candidate,
        "facets": [],
        "gate_status": "passed",
        "status": "done",
        "completion_record_path": str(completion_path),
        "completion_record_hash": completion_hash,
        "completion_record_revision": candidate,
        "completion_source_revisions": {"openspec:qualification": "rev"},
        "pr_candidate": candidate,
        "merge_revision": "b" * 40,
    }
    registry = coordinator / "jobs.json"
    _write_json(
        registry,
        {
            "schema_version": 2,
            "seq": 9,
            "jobs": jobs,
            "slices": [],
            "workflows": [workflow],
            "legacy_records": {},
            "reclaim_resets": [],
        },
    )
    artifacts.append(registry)
    return {
        "coordinator": coordinator,
        "repo": repo,
        "candidate": candidate,
        "run_id": run_id,
        "work_id": work_id,
        "repository": repository,
        "issue": issue,
        "registry": registry,
        "artifacts": artifacts,
    }


def test_dispatch_closeout_rejects_forged_marker_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    forged = tmp_path / "forged.txt"
    forged.write_text(
        "qualification-work candidate bundle verdict ledger evidence completion",
        encoding="utf-8",
    )
    coordinator = tmp_path / "coordinator"
    coordinator.mkdir()
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    with pytest.raises(driver.QualificationFailure, match="registry"):
        driver._validate_dispatch_closeout(
            repository="owner/repo",
            work_id="qualification-work",
            issue=42,
            terminal={"status": "done"},
            coordinator_root=coordinator,
        )


def test_dispatch_closeout_binds_structured_authority_hashes_and_reclaim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())

    def fake_run(argv, **_kwargs):
        if "cat-file" in argv:
            return _result(driver, argv)
        if "worktree" in argv:
            return _result(driver, argv, stdout=f"worktree {fixture['repo']}\n")
        if "bundle" in argv and "verify" in argv:
            return _result(driver, argv)
        if "bundle" in argv and "list-heads" in argv:
            return _result(
                driver, argv, stdout=f"{fixture['candidate']} refs/heads/work\n"
            )
        raise AssertionError(argv)

    monkeypatch.setattr(driver, "_run", fake_run)
    markers, rows, workflow = driver._validate_dispatch_closeout(
        repository=fixture["repository"],
        work_id=fixture["work_id"],
        issue=fixture["issue"],
        terminal={
            "status": "done",
            "run_id": fixture["run_id"],
            "work_id": fixture["work_id"],
        },
        coordinator_root=fixture["coordinator"],
    )
    assert set(markers) == {
        "candidate",
        "bundle",
        "verdict",
        "ledger",
        "evidence",
        "completion",
    }
    assert workflow["run_id"] == fixture["run_id"]
    assert rows
    for row in rows:
        path = fixture["coordinator"].parent / row["path"]
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]


@pytest.mark.parametrize("mutation", ["wrong-hash", "wrong-work", "live-worktree"])
def test_dispatch_closeout_fails_closed_on_binding_or_reclaim_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    payload = json.loads(fixture["registry"].read_text())
    if mutation == "wrong-hash":
        payload["jobs"][0]["workflow_evidence"]["hash"] = "0" * 64
    elif mutation == "wrong-work":
        payload["workflows"][0]["work_id"] = "another-work"
    else:
        Path(payload["jobs"][1]["worktree"]).mkdir(parents=True)
    _write_json(fixture["registry"], payload)
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())

    def fake_run(argv, **_kwargs):
        if "cat-file" in argv or ("bundle" in argv and "verify" in argv):
            return _result(driver, argv)
        if "worktree" in argv:
            return _result(driver, argv, stdout=f"worktree {fixture['repo']}\n")
        if "bundle" in argv and "list-heads" in argv:
            return _result(
                driver, argv, stdout=f"{fixture['candidate']} refs/heads/work\n"
            )
        raise AssertionError(argv)

    monkeypatch.setattr(driver, "_run", fake_run)
    with pytest.raises(driver.QualificationFailure):
        driver._validate_dispatch_closeout(
            repository=fixture["repository"],
            work_id=fixture["work_id"],
            issue=fixture["issue"],
            terminal={
                "status": "done",
                "run_id": fixture["run_id"],
                "work_id": fixture["work_id"],
            },
            coordinator_root=fixture["coordinator"],
        )
