from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pytest

from paulsha_cortex import cli as umbrella_cli
from paulsha_cortex import recovery_action_contracts as recovery_contracts
from paulsha_cortex.control import constants as control_constants
from paulsha_cortex.control import contract as control_contract
from paulsha_cortex.coordinator import cli as coordinator_cli
from paulsha_cortex.coordinator import work_actions
from paulsha_cortex.porcelain import recover as porcelain_recover


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "docs" / "recovery-action-contract-matrix.md"
RUN_ID = "workflow-" + "a" * 20
CANDIDATE = "b" * 40


def _subparser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    action = next(
        item
        for item in parser._actions
        if isinstance(item, argparse._SubParsersAction)
    )
    return action.choices[name]


def _choice_set(parser: argparse.ArgumentParser, command: str) -> set[str]:
    selected = _subparser(parser, command)
    action = next(
        item
        for item in selected._actions
        if item.dest == "action" and item.choices is not None
    )
    return set(action.choices)


def _work_fields(action: str) -> dict[str, object]:
    fields: dict[str, object] = {}
    if action in {"retry-build", "retry-verify", "retry-review", "recover-repair-commit"}:
        fields["expected_candidate"] = CANDIDATE
    if action in {"retry-card", "regenerate-gates", "recover-repair-commit"}:
        fields["expected_run_id"] = RUN_ID
    if action == "retry-card":
        fields["card"] = "verification"
    if action == "recover-planning":
        fields.update(
            expected_run_id=RUN_ID,
            failure_classification="environment",
            failure_reason="provider unavailable",
        )
    if action in {"abandon", "retire-delivered", "recover-superseded"}:
        fields.update(expected_run_id=RUN_ID, actor="operator", reason="recovery audit")
    if action == "reset-reclaim-budget":
        fields.update(actor="operator", reason="recovery audit")
    if action == "refreeze-base":
        fields.update(expected_run_id=RUN_ID, actor="operator", reason="recovery audit")
    return fields


def _control_request(action: str, args: dict[str, object]) -> dict[str, object]:
    return control_contract.validate_request(
        {
            "schema_version": control_constants.SCHEMA_VERSION,
            "req_id": "req-recovery-contract-test",
            "type": "work-action",
            "args": {"action": action, "repo": "acme/demo", "work_id": "demo", **args},
            "requested_by": "operator",
            "created_at": "2026-09-26T00:00:00Z",
        }
    )


def _matrix_rows(text: str) -> dict[str, list[str]]:
    section = text.split("## Work recovery matrix", 1)[1].split("\n## ", 1)[0]
    rows: dict[str, list[str]] = {}
    for line in section.splitlines():
        match = re.match(r"^\| `([a-z0-9-]+)` \|(.*)\|$", line)
        if match:
            rows[match.group(1)] = [cell.strip() for cell in match.group(2).split("|")]
    return rows


def _action_values(cell: str) -> set[str]:
    return set(re.findall(r"`([a-z0-9-]+)`", cell))


def test_r01_registry_and_versioned_document_cover_each_other() -> None:
    assert recovery_contracts.RECOVERY_ACTION_CONTRACT_VERSION == "recovery-action-contract/v1"
    assert len(recovery_contracts.RECOVERY_ACTION_FAMILIES) == 13
    matrix = MATRIX_PATH.read_text(encoding="utf-8")
    rows = _matrix_rows(matrix)
    assert set(rows) == set(recovery_contracts.RECOVERY_ACTION_FAMILIES)

    for family_id, family in recovery_contracts.RECOVERY_ACTION_FAMILIES.items():
        assert _action_values(rows[family_id][0]) == set(family.coordinator_work)
        assert _action_values(rows[family_id][1]) == set(family.recover_work)
        assert _action_values(rows[family_id][2]) == set(family.coordinator_slice)
        assert _action_values(rows[family_id][3]) == set(family.recover_slice)

    matrix_work_actions = {
        action
        for family in recovery_contracts.RECOVERY_ACTION_FAMILIES.values()
        for action in family.coordinator_work
    }
    matrix_slice_actions = {
        action
        for family in recovery_contracts.RECOVERY_ACTION_FAMILIES.values()
        for action in family.coordinator_slice
    } | set(recovery_contracts.RECOVERY_SLICE_EXTENSIONS)
    assert matrix_work_actions == set(recovery_contracts.RECOVERY_WORK_ACTIONS)
    assert matrix_slice_actions == set(recovery_contracts.RECOVERY_SLICE_ACTIONS)
    assert set(recovery_contracts.RECOVER_WORK_ACTION_CHOICES) == {
        action
        for family in recovery_contracts.RECOVERY_ACTION_FAMILIES.values()
        for action in family.recover_work
    }
    assert set(recovery_contracts.RECOVER_SLICE_ACTION_CHOICES) == set(
        recovery_contracts.SLICE_ACTION_CHOICES
    )
    assert set(recovery_contracts.WORK_ACTION_CHOICES) == set(
        recovery_contracts.WORK_ACTION_CLASSIFICATION
    )
    assert set(recovery_contracts.WORK_ACTION_CHOICES) == (
        matrix_work_actions | set(recovery_contracts.NON_RECOVERY_WORK_ACTIONS)
    )
    assert set(recovery_contracts.SLICE_ACTION_CHOICES) == set(
        recovery_contracts.SLICE_ACTION_CLASSIFICATION
    )


@pytest.mark.parametrize(
    "family_id", sorted(recovery_contracts.RECOVERY_ACTION_FAMILIES)
)
def test_r01_each_recovery_family_has_a_versioned_matrix_row(family_id: str) -> None:
    matrix = MATRIX_PATH.read_text(encoding="utf-8")
    assert f"| `{family_id}` |" in matrix


def test_r01_formal_dispatchers_and_public_entrypoints_share_registered_choices() -> None:
    assert control_contract.WORK_ACTIONS == recovery_contracts.WORK_ACTIONS
    assert work_actions.WORK_ACTIONS == recovery_contracts.WORK_ACTIONS
    assert _choice_set(coordinator_cli._build_parser(), "work") == recovery_contracts.WORK_ACTIONS
    assert _choice_set(coordinator_cli._build_parser(), "slice-action") == recovery_contracts.SLICE_ACTIONS
    assert _choice_set(porcelain_recover._build_parser(), "work") == set(
        recovery_contracts.RECOVER_WORK_ACTION_CHOICES
    )
    assert _choice_set(porcelain_recover._build_parser(), "slice") == set(
        recovery_contracts.RECOVER_SLICE_ACTION_CHOICES
    )


@pytest.mark.parametrize("action", sorted(recovery_contracts.RECOVERY_WORK_ACTIONS))
def test_r02_control_contract_accepts_each_registered_work_recovery_action(
    action: str,
) -> None:
    normalized = _control_request(action, _work_fields(action))
    assert normalized["args"]["action"] == action


@pytest.mark.parametrize(
    "action", recovery_contracts.RECOVER_WORK_ACTION_CHOICES
)
def test_public_recover_work_alias_preserves_action_and_cas_fields(action: str) -> None:
    fields = _work_fields(action)
    argv = ["work", "demo", action, "--repo", "acme/demo", "--actor", "operator"]
    option_names = {
        "expected_run_id": "--expected-run-id",
        "expected_candidate": "--expected-candidate",
        "failure_classification": "--failure-classification",
        "failure_reason": "--failure-reason",
        "card": "--card",
        "reason": "--reason",
    }
    for field, value in fields.items():
        if field == "actor":
            continue
        argv.extend([option_names[field], str(value)])
    parsed = _subparser(porcelain_recover._build_parser(), "work").parse_args(argv[1:])
    mapped = porcelain_recover._work_args(parsed)
    assert mapped["action"] == action
    assert mapped["repo"] == "acme/demo"
    assert mapped["work_id"] == "demo"
    for field, value in fields.items():
        assert mapped[field] == value
    _control_request(action, {key: value for key, value in mapped.items() if key not in {"action", "repo", "work_id"}})


@pytest.mark.parametrize("action", recovery_contracts.RECOVER_WORK_ACTION_CHOICES)
def test_recover_work_formal_cli_submits_the_same_registered_action(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    submitted: list[tuple[str, dict[str, object], str]] = []
    monkeypatch.setattr(
        porcelain_recover.control_client,
        "submit_request",
        lambda kind, args, actor: submitted.append((kind, args, actor)) or "req-843",
    )
    monkeypatch.setattr(porcelain_recover, "track_submitted_request", lambda *_a, **_k: 0)
    argv = ["work", "demo", action, "--repo", "acme/demo", "--actor", "operator"]
    option_names = {
        "expected_run_id": "--expected-run-id",
        "expected_candidate": "--expected-candidate",
        "failure_classification": "--failure-classification",
        "failure_reason": "--failure-reason",
        "card": "--card",
        "reason": "--reason",
    }
    for field, value in _work_fields(action).items():
        if field == "actor":
            continue
        argv.extend([option_names[field], str(value)])

    assert porcelain_recover.main(argv) == 0
    assert len(submitted) == 1
    kind, args, requested_by = submitted[0]
    assert kind == "work-action"
    assert requested_by == porcelain_recover.REQUESTED_BY
    assert args["action"] == action
    assert args["repo"] == "acme/demo"
    assert args["work_id"] == "demo"
    assert args["actor"] == "operator"
    for field, value in _work_fields(action).items():
        assert args[field] == value


@pytest.mark.parametrize("action", recovery_contracts.RECOVER_SLICE_ACTION_CHOICES)
def test_public_recover_slice_alias_preserves_namespace_action_and_cas(
    action: str,
) -> None:
    argv = ["slice", "slice-demo", action, "--actor", "operator"]
    if action == "supersede":
        argv.extend(["--reason", "replace stale slice", "--expected-binding-revision", "2"])
    parsed = _subparser(porcelain_recover._build_parser(), "slice").parse_args(argv[1:])
    mapped = porcelain_recover._slice_args(parsed)
    assert mapped["slice_id"] == "slice-demo"
    assert mapped["action"] == action
    assert mapped["actor"] == "operator"
    if action == "supersede":
        assert mapped["expected_binding_revision"] == 2
        assert mapped["reason"] == "replace stale slice"
        payload = {
            "schema_version": control_constants.SCHEMA_VERSION,
            "req_id": "req-slice-recovery-contract-test",
            "type": "slice-action",
            "args": mapped,
            "requested_by": "operator",
            "created_at": "2026-09-26T00:00:00Z",
        }
        assert control_contract.validate_request(payload)["args"] == mapped


@pytest.mark.parametrize("action", recovery_contracts.RECOVER_SLICE_ACTION_CHOICES)
def test_recover_slice_formal_cli_submits_the_same_registered_namespace_action(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    submitted: list[tuple[str, dict[str, object], str]] = []
    monkeypatch.setattr(
        porcelain_recover.control_client,
        "submit_request",
        lambda kind, args, actor: submitted.append((kind, args, actor)) or "req-843",
    )
    monkeypatch.setattr(porcelain_recover, "track_submitted_request", lambda *_a, **_k: 0)
    argv = ["slice", "slice-demo", action, "--actor", "operator"]
    expected: dict[str, object] = {
        "slice_id": "slice-demo",
        "action": action,
        "actor": "operator",
    }
    if action == "supersede":
        argv.extend(["--reason", "replace stale slice", "--expected-binding-revision", "2"])
        expected.update(reason="replace stale slice", expected_binding_revision=2)

    assert porcelain_recover.main(argv) == 0
    assert len(submitted) == 1
    kind, args, requested_by = submitted[0]
    assert kind == "slice-action"
    assert requested_by == porcelain_recover.REQUESTED_BY
    assert args == expected


@pytest.mark.parametrize("action", sorted(recovery_contracts.RECOVERY_WORK_ACTIONS))
def test_coordinator_work_cli_submits_the_registered_action_unchanged(
    action: str,
) -> None:
    submitted: list[tuple[str, dict[str, object], str]] = []
    fields = _work_fields(action)
    argv = ["work", action, "demo", "--repo", "acme/demo", "--actor", "operator"]
    option_names = {
        "expected_run_id": "--expected-run-id",
        "expected_candidate": "--expected-candidate",
        "failure_classification": "--failure-classification",
        "failure_reason": "--failure-reason",
        "card": "--card",
        "reason": "--reason",
    }
    for field, value in fields.items():
        if field == "actor":
            continue
        argv.extend([option_names[field], str(value)])

    result = coordinator_cli.main(
        argv,
        control_read_status=lambda: {"degraded": False},
        control_submit_request=lambda kind, args, actor: submitted.append(
            (kind, args, actor)
        )
        or "req-recovery-contract-test",
        control_poll_done=lambda *_args, **_kwargs: {"status": "ok", "result": {}},
    )
    assert result == 0
    assert len(submitted) == 1
    request_type, args, actor = submitted[0]
    assert request_type == "work-action"
    assert actor == coordinator_cli.DEFAULT_REQUESTED_BY
    assert args["action"] == action
    assert args["repo"] == "acme/demo"
    assert args["work_id"] == "demo"
    assert args["actor"] == "operator"
    for field, value in fields.items():
        assert args[field] == value
    _control_request(action, {key: value for key, value in args.items() if key not in {"action", "repo", "work_id"}})


def test_r06_legacy_reviewer_actions_remain_distinct_from_slice_actions() -> None:
    families = recovery_contracts.RECOVERY_ACTION_FAMILIES
    legacy = families["legacy-reviewer-retry"]
    assert legacy.coordinator_work == ("retry-verify", "retry-review")
    assert legacy.coordinator_slice == ("retry-verify", "retry-review")
    assert "retry-verify" not in recovery_contracts.RECOVER_WORK_ACTION_CHOICES
    assert "retry-review" not in recovery_contracts.RECOVER_WORK_ACTION_CHOICES
    assert set(legacy.coordinator_slice) <= set(recovery_contracts.RECOVER_SLICE_ACTION_CHOICES)


def test_r16_candidate_cas_help_names_all_recovery_consumers() -> None:
    help_text = _subparser(coordinator_cli._build_parser(), "work").format_help()
    for action in recovery_contracts.RECOVERY_EXPECTED_CANDIDATE_ACTIONS:
        assert action in help_text
    assert "exact Candidate SHA CAS" in help_text


def test_r16_umbrella_and_recover_alias_help_match_candidate_cas_scope() -> None:
    umbrella_help = umbrella_cli._WORK_HELP
    recover_work_help = _subparser(
        porcelain_recover._build_parser(), "work"
    ).format_help()
    recover_work_help = " ".join(recover_work_help.split())
    for action in recovery_contracts.RECOVERY_EXPECTED_CANDIDATE_ACTIONS:
        assert action in umbrella_help
    assert "--expected-candidate" in umbrella_help
    assert "--expected-candidate" in recover_work_help
    assert "retry-build／recover-repair-commit" in recover_work_help
    assert "exact Candidate SHA CAS" in recover_work_help


@pytest.mark.parametrize(
    "action", sorted(recovery_contracts.RECOVERY_EXPECTED_CANDIDATE_ACTIONS)
)
def test_explicit_candidate_cas_rejects_a_conflicting_payload(
    tmp_path: Path, action: str
) -> None:
    payload = tmp_path / "work-payload.json"
    payload.write_text(
        json.dumps({"expected_candidate": "c" * 40}), encoding="utf-8"
    )
    submitted: list[object] = []
    result = coordinator_cli.main(
        [
            "work", action, "demo", "--repo", "acme/demo",
            "--expected-candidate", CANDIDATE, "--payload", str(payload),
        ],
        control_read_status=lambda: {"degraded": False},
        control_submit_request=lambda *args: submitted.append(args) or "req-843",
        control_poll_done=lambda *_args, **_kwargs: {"status": "ok", "result": {}},
    )
    assert result == 2
    assert submitted == []
