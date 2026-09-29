from __future__ import annotations

import argparse

import pytest

from paulsha_cortex import recovery_action_contracts as recovery_contracts
from paulsha_cortex.control import constants
from paulsha_cortex.control import contract
from paulsha_cortex.coordinator import cli as coordinator_cli
from paulsha_cortex.porcelain import recover


RUN_ID = "workflow-" + "a" * 20
ERA = "claim:v1:" + "c" * 64
CANDIDATE = "b" * 40
CHAIN = {
    "planner_executor": "claude",
    "planner_model": "planner-model",
    "builder_executor": "agy",
    "builder_model": "builder-model",
    "reviewer_executor": "codex",
    "reviewer_model": "reviewer-model",
}


def _args(**overrides):
    return {
        "action": "rechain",
        "repo": "acme/demo",
        "work_id": "demo",
        "actor": "operator",
        "reason": "explicit chain readjudication",
        "expected_run_id": RUN_ID,
        "expected_candidate": CANDIDATE,
        "expected_era": ERA,
        **CHAIN,
        **overrides,
    }


def _request(**overrides):
    return contract.validate_request(
        {
            "schema_version": constants.SCHEMA_VERSION,
            "req_id": "req-rechain-test",
            "type": "work-action",
            "args": _args(**overrides),
            "requested_by": "operator",
            "created_at": "2026-09-29T00:00:00Z",
        }
    )


def test_rechain_is_registered_in_both_work_action_namespaces():
    assert "rechain" in recovery_contracts.WORK_ACTIONS
    assert "rechain" in recovery_contracts.RECOVER_WORK_ACTION_CHOICES
    assert "rechain" in recovery_contracts.RECOVERY_ACTION_FAMILIES["rechain"].coordinator_work


def test_rechain_contract_requires_exact_run_candidate_era_and_full_chain():
    accepted = _request()
    assert accepted["args"]["action"] == "rechain"

    for field, bad in (
        ("expected_run_id", "workflow-short"),
        ("expected_candidate", "short"),
        ("expected_era", "wrong-era"),
        ("reviewer_model", ""),
        ("builder_executor", None),
    ):
        with pytest.raises(ValueError):
            _request(**{field: bad})


@pytest.mark.parametrize(
    "field",
    [
        "actor", "reason", "expected_run_id", "expected_candidate", "expected_era",
        "planner_executor", "planner_model", "builder_executor", "builder_model",
        "reviewer_executor", "reviewer_model",
    ],
)
def test_rechain_contract_rejects_each_missing_authority_cas_or_persona_pin(field):
    with pytest.raises(ValueError):
        _request(**{field: None})


def test_rechain_cli_exposes_exact_cas_and_per_persona_chain():
    parser = coordinator_cli._build_parser()
    parsed = parser.parse_args(
        [
            "work", "rechain", "demo", "--repo", "acme/demo",
            "--actor", "operator", "--reason", "explicit chain readjudication",
            "--expected-run-id", RUN_ID, "--expected-candidate", CANDIDATE,
            "--expected-era", ERA,
            "--planner-executor", "claude", "--planner-model", "planner-model",
            "--builder-executor", "agy", "--builder-model", "builder-model",
            "--reviewer-executor", "codex", "--reviewer-model", "reviewer-model",
        ]
    )
    assert parsed.action == "rechain"
    recover_parsed = recover._build_parser().parse_args(
        [
            "work", "demo", "rechain", "--repo", "acme/demo", "--actor", "operator",
            "--reason", "explicit chain readjudication", "--expected-run-id", RUN_ID,
            "--expected-candidate", CANDIDATE, "--expected-era", ERA,
            "--planner-executor", "claude", "--planner-model", "planner-model",
            "--builder-executor", "agy", "--builder-model", "builder-model",
            "--reviewer-executor", "codex", "--reviewer-model", "reviewer-model",
        ]
    )
    assert recover_parsed.action == "rechain"
