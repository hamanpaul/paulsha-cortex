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


def _provider_name(argv) -> str:
    executable = Path(argv[0]).name
    return {"agy": "agy", "copilot": "copilot", "codex": "codex"}[executable]


def _preflight(**overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "ready",
        "authenticated": True,
        "quota": "available",
        "fallback": False,
        "skipped": False,
    }
    payload.update(overrides)
    return payload


def _smoke(provider: str, *, extra_model: str | None = None) -> str:
    models = {"agy": "gemini-3.7-flash", "copilot": "gpt-5.4", "codex": "gpt-5"}
    efforts = {"agy": "high", "copilot": "xhigh", "codex": "normal"}
    rows = [
        {
            "provider": provider,
            "runtime_model": models[provider],
            "runtime_effort": efforts[provider],
            "type": "final",
            "role": "assistant",
            "content": "QUALIFICATION_OK",
        }
    ]
    if extra_model is not None:
        rows.append(
            {
                "provider": provider,
                "runtime_model": extra_model,
                "runtime_effort": efforts[provider],
                "fallback": True,
            }
        )
    return "".join(json.dumps(row) + "\n" for row in rows)


def test_provider_smokes_use_live_preflight_and_unique_runtime_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_run(argv, **_kwargs):
        provider = _provider_name(argv)
        calls.append(("smoke", tuple(argv)))
        return _result(driver, argv, stdout=_smoke(provider))

    monkeypatch.setattr(driver, "_run", fake_run)
    monkeypatch.setattr(
        driver, "_provider_preflight", lambda _provider, _account: _preflight()
    )
    verdicts = driver._provider_smokes(tmp_path)
    assert [
        (row["provider"], row["runtime_model"], row["runtime_effort"])
        for row in verdicts
    ] == [
        ("agy", "gemini-3.7-flash", "high"),
        ("copilot", "gpt-5.4", "xhigh"),
        ("codex", "gpt-5", "normal"),
    ]
    assert [kind for kind, _argv in calls] == [
        "smoke",
        "smoke",
        "smoke",
    ]
    evidence = json.loads((tmp_path / "provider-capabilities.json").read_text())
    assert all(
        row["preflight"]["quota"] == "available"
        for row in evidence["providers"].values()
    )
    assert all(
        row["preflight"]["fallback"] is False for row in evidence["providers"].values()
    )


def test_provider_smokes_reject_requested_plus_fallback_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()

    def fake_run(argv, **_kwargs):
        provider = _provider_name(argv)
        return _result(
            driver, argv, stdout=_smoke(provider, extra_model="fallback-model")
        )

    monkeypatch.setattr(driver, "_run", fake_run)
    monkeypatch.setattr(
        driver, "_provider_preflight", lambda _provider, _account: _preflight()
    )
    with pytest.raises(driver.QualificationFailure, match="unique exact"):
        driver._provider_smokes(tmp_path)
    assert not (tmp_path / "provider-capabilities.json").exists()


def test_provider_preflight_uses_only_supported_pinned_argv() -> None:
    driver = _load_driver()
    adapters = driver.PROVIDER_PREFLIGHTS

    assert adapters["agy"].version == "1.1.18"
    assert adapters["agy"].version_command[-1] == "--version"
    assert adapters["agy"].status_command[-4:] == (
        "-p",
        "/quota",
        "--output-format",
        "json",
    )
    assert adapters["copilot"].version is None
    assert adapters["copilot"].status_command[-4:] == (
        "--headless",
        "--no-auto-update",
        "--stdio",
        "--no-auto-login",
    )
    assert adapters["copilot"].status_kind == "copilot-app-server"
    assert adapters["codex"].version == "0.149.0"
    assert adapters["codex"].status_command[-2:] == ("app-server", "--stdio")
    assert adapters["codex"].status_kind == "codex-app-server"
    serialized = repr(adapters)
    assert "status --output-format json" not in serialized
    assert "login status --json" not in serialized
    assert "doctor" not in serialized


def test_agy_preflight_accepts_machine_readable_quota_without_prompt_or_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    calls: list[tuple[str, ...]] = []
    quota = {
        "status": "SUCCESS",
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {"buckets": [{"remaining_fraction": 0.5}]},
                ]
            },
        },
    }

    def fake_run(argv, **_kwargs):
        calls.append(tuple(argv))
        if argv[-1] == "--version":
            return _result(driver, argv, stdout="agy version 1.1.18\n")
        return _result(driver, argv, stdout=json.dumps(quota))

    monkeypatch.setattr(driver, "_run", fake_run)
    assert driver._provider_preflight("agy", "cortex-reviewer-planner") == {
        "status": "ready",
        "authenticated": True,
        "quota": "available",
        "fallback": False,
    }
    assert len(calls) == 2
    assert calls[0][-1] == "--version"
    assert calls[1][-4:] == ("-p", "/quota", "--output-format", "json")


def test_agy_preflight_rejects_exhausted_machine_readable_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()

    def fake_run(argv, **_kwargs):
        if argv[-1] == "--version":
            return _result(driver, argv, stdout="agy version 1.1.18\n")
        return _result(
            driver,
            argv,
            stdout=json.dumps(
                {
                    "status": "SUCCESS",
                    "command": {
                        "name": "usage",
                        "data": {"groups": [{"buckets": [{"remaining_fraction": 0}]}]},
                    },
                }
            ),
        )

    monkeypatch.setattr(driver, "_run", fake_run)
    with pytest.raises(driver.QualificationFailure, match="no remaining capacity"):
        driver._provider_preflight("agy", "cortex-reviewer-planner")


def test_copilot_app_server_accepts_authenticated_quota_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    monkeypatch.setattr(driver, "_account_env", lambda _account: {})
    calls: list[tuple[str, ...]] = []

    def fake_run(argv, **_kwargs):
        calls.append(tuple(argv))
        return _result(driver, argv, stdout="Copilot CLI 1.0.80\n")

    monkeypatch.setattr(driver, "_run", fake_run)
    monkeypatch.setattr(
        driver,
        "_copilot_app_server_exchange",
        lambda _command, **_kwargs: (
            {
                "id": 2,
                "result": {
                    "authInfo": {"type": "user", "login": "redacted-user"}
                },
            },
            {
                "id": 3,
                "result": {
                    "quotaSnapshots": {
                        "premium_interactions": {
                            "remainingPercentage": 65,
                            "entitlementRequests": 100,
                            "usedRequests": 35,
                        },
                        "chat": {"isUnlimitedEntitlement": True},
                    }
                },
            },
        ),
    )

    assert driver._provider_preflight("copilot", "cortex-reviewer-planner") == {
        "status": "ready",
        "authenticated": True,
        "quota": "available",
        "fallback": False,
    }
    assert calls == [("/opt/cortex/toolchain/bin/copilot", "--version")]


def test_copilot_app_server_without_auth_or_quota_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    monkeypatch.setattr(driver, "_account_env", lambda _account: {})
    monkeypatch.setattr(
        driver,
        "_run",
        lambda argv, **_kwargs: _result(driver, argv, stdout="Copilot CLI 1.0.80\n"),
    )
    monkeypatch.setattr(
        driver,
        "_copilot_app_server_exchange",
        lambda _command, **_kwargs: (
            {"id": 2, "result": {}},
            {
                "id": 3,
                "error": {
                    "code": -32603,
                    "message": "Not authenticated",
                },
            },
        ),
    )
    with pytest.raises(driver.QualificationFailure, match="authenticated account"):
        driver._provider_preflight("copilot", "cortex-reviewer-planner")


def test_codex_app_server_accepts_authenticated_rate_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    monkeypatch.setattr(driver, "_account_env", lambda _account: {})
    calls: list[tuple[str, ...]] = []

    def fake_run(argv, **_kwargs):
        calls.append(tuple(argv))
        return _result(driver, argv, stdout="codex-cli 0.149.0\n")

    monkeypatch.setattr(driver, "_run", fake_run)
    monkeypatch.setattr(
        driver,
        "_codex_app_server_exchange",
        lambda command, **_kwargs: (
            {
                "id": 2,
                "result": {
                    "account": {
                        "type": "chatgpt",
                        "email": "redacted@example.invalid",
                        "planType": "pro",
                    },
                    "requiresOpenaiAuth": False,
                },
            },
            {
                "id": 3,
                "result": {
                    "rateLimits": {
                        "primary": {
                            "usedPercent": 25,
                            "windowDurationMins": 300,
                            "resetsAt": 1_900_000_000,
                        },
                        "secondary": None,
                        "rateLimitReachedType": None,
                        "spendControlReached": None,
                    }
                },
            },
        ),
    )

    assert driver._provider_preflight("codex", "cortex-builder") == {
        "status": "ready",
        "authenticated": True,
        "quota": "available",
        "fallback": False,
    }
    assert calls == [("/opt/cortex/toolchain/bin/codex", "--version")]


def test_codex_app_server_without_live_account_or_rate_limits_fails_closed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    monkeypatch.setattr(driver, "_account_env", lambda _account: {})
    calls: list[tuple[str, ...]] = []

    def fake_run(argv, **_kwargs):
        calls.append(tuple(argv))
        return _result(driver, argv, stdout="codex-cli 0.149.0\n")

    monkeypatch.setattr(driver, "_run", fake_run)
    monkeypatch.setattr(
        driver,
        "_codex_app_server_exchange",
        lambda _command, **_kwargs: (
            {"id": 2, "result": {"account": None, "requiresOpenaiAuth": True}},
            {
                "id": 3,
                "error": {
                    "code": -32600,
                    "message": "codex account authentication required",
                },
            },
        ),
    )
    with pytest.raises(driver.QualificationFailure, match="authenticated account"):
        driver._provider_preflight("codex", "cortex-builder")
    assert calls == [("/opt/cortex/toolchain/bin/codex", "--version")]


@pytest.mark.parametrize(
    ("records", "expected"),
    [
        (
            [{"type": "final", "role": "assistant", "content": "QUALIFICATION_OK"}],
            True,
        ),
        (
            [
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "QUALIFICATION_OK"},
                }
            ],
            True,
        ),
        (
            [
                {
                    "type": "assistant.message",
                    "data": {"content": "QUALIFICATION_OK"},
                }
            ],
            True,
        ),
        (
            [
                {
                    "type": "user.message",
                    "data": {"content": "Return exactly QUALIFICATION_OK"},
                }
            ],
            False,
        ),
        (
            [
                {
                    "type": "assistant.message",
                    "data": {"content": "I refuse. QUALIFICATION_OK"},
                }
            ],
            False,
        ),
        (
            [
                {
                    "type": "assistant.message",
                    "data": {"content": "QUALIFICATION_OK extra"},
                }
            ],
            False,
        ),
        (
            [
                {
                    "type": "assistant.message",
                    "data": {"content": "QUALIFICATION_OK"},
                },
                {
                    "type": "assistant.message",
                    "data": {"content": "QUALIFICATION_OK"},
                },
            ],
            False,
        ),
        ([{"type": "user", "content": "Return QUALIFICATION_OK"}], False),
        (
            [
                {
                    "type": "final",
                    "role": "assistant",
                    "content": "I refuse. QUALIFICATION_OK",
                }
            ],
            False,
        ),
        (
            [{"type": "final", "role": "assistant", "content": "QUALIFICATION_OK\n"}],
            False,
        ),
    ],
)
def test_final_assistant_response_must_be_exact(
    records: list[object], expected: bool
) -> None:
    driver = _load_driver()
    assert driver._has_exact_final_assistant_response(records) is expected


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
    workflow_evidence: dict[str, tuple[Path, str]] = {}
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
        workflow_evidence[phase] = (evidence_path, evidence_hash)
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
    brainstorm_path = (
        coordinator / "evidence" / "planning" / f"brainstorm-{run_id}.json"
    )
    brainstorm_hash = _write_json(
        brainstorm_path,
        {
            "schema_version": 1,
            "kind": "brainstorm-peer",
            "run_id": run_id,
            "work_id": work_id,
        },
    )
    artifacts.append(brainstorm_path)
    review_path, review_hash = workflow_evidence["review"]
    copilot_path = coordinator / "evidence" / "delivery-adapter" / "copilot.json"
    copilot_hash = _write_json(
        copilot_path,
        {
            "schema_version": 1,
            "kind": "copilot",
            "run_id": run_id,
            "work_id": work_id,
            "candidate": candidate,
            "status": "passed",
        },
    )
    artifacts.append(copilot_path)
    gate_refs = [
        {
            "kind": "brainstorm",
            "ref": str(brainstorm_path),
            "sha256": brainstorm_hash,
        },
        {
            "kind": "foreign-review",
            "ref": str(review_path),
            "sha256": review_hash,
        },
        {
            "kind": "copilot",
            "ref": str(copilot_path),
            "sha256": copilot_hash,
        },
    ]
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
        "gate_refs": gate_refs,
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
    registry = json.loads(fixture["registry"].read_text(encoding="utf-8"))
    gate_refs = {row["kind"]: row for row in registry["workflows"][0]["gate_refs"]}
    assert Path(gate_refs["brainstorm"]["ref"]).is_absolute()
    assert Path(gate_refs["brainstorm"]["ref"]).parent.name == "planning"
    assert gate_refs["foreign-review"]["ref"] == str(
        fixture["coordinator"] / "evidence" / "workflow" / "review-job.json"
    )
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


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "hash",
        "symlink",
        "symlink-ancestor",
        "absolute-escape",
        "lexical-escape",
        "duplicate",
    ],
)
def test_dispatch_closeout_resolves_every_delivery_gate_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    payload = json.loads(fixture["registry"].read_text())
    refs = payload["workflows"][0]["gate_refs"]
    first = Path(refs[0]["ref"])
    if mutation == "missing":
        first.unlink()
    elif mutation == "hash":
        refs[0]["sha256"] = "0" * 64
    elif mutation == "symlink":
        target = first.with_name("target.json")
        first.rename(target)
        first.symlink_to(target)
    elif mutation == "symlink-ancestor":
        target = first.parent.with_name("planning-target")
        first.parent.rename(target)
        first.parent.symlink_to(target, target_is_directory=True)
    elif mutation == "absolute-escape":
        outside = tmp_path / "outside-gate.json"
        refs[0]["sha256"] = _write_json(outside, {"status": "passed"})
        refs[0]["ref"] = str(outside)
    elif mutation == "lexical-escape":
        outside = fixture["coordinator"] / "outside-gate.json"
        refs[0]["sha256"] = _write_json(outside, {"status": "passed"})
        refs[0]["ref"] = str(
            fixture["coordinator"] / "evidence" / ".." / outside.name
        )
    else:
        refs[1]["ref"] = refs[0]["ref"]
        refs[1]["sha256"] = refs[0]["sha256"]
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
    with pytest.raises(driver.QualificationFailure, match="delivery gate"):
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
