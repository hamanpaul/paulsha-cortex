from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from paulsha_cortex import command_policy


BASELINE_POLICY_PATH = Path("coordinator/data/command-policy.yaml")
PACKAGED_BASELINE_POLICY_PATH = Path("paulsha_cortex/coordinator/data/command-policy.yaml")
CORPUS_PATH = Path("tests/fixtures/command-policy/corpus.yaml")


def _load_policy() -> command_policy.CommandPolicy:
    return command_policy.load_policy_text(
        BASELINE_POLICY_PATH.read_text(encoding="utf-8"),
        source=str(BASELINE_POLICY_PATH),
    )


def _load_policy_payload() -> dict[str, object]:
    payload = yaml.safe_load(BASELINE_POLICY_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _load_corpus() -> dict[str, object]:
    payload = yaml.safe_load(CORPUS_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_command_policy_module_stays_yaml_free() -> None:
    source = Path(command_policy.__file__).read_text(encoding="utf-8")
    assert "import yaml" not in source
    assert "from yaml import" not in source


def test_packaged_command_policy_asset_matches_versioned_source() -> None:
    assert PACKAGED_BASELINE_POLICY_PATH.read_bytes() == BASELINE_POLICY_PATH.read_bytes()


def test_command_policy_corpus_covers_every_rule_bidirectionally() -> None:
    payload = _load_corpus()
    policy = _load_policy()

    coverage: dict[str, set[str]] = {}
    for case in payload["cases"]:
        rule_id = case["rule"]
        coverage.setdefault(rule_id, set()).add(case["expect"])

    for rule_id in policy.rules:
        assert coverage.get(rule_id) == {"allowed", "blocked"}, rule_id


def test_command_policy_corpus_matches_policy_decisions() -> None:
    payload = _load_corpus()
    policy = _load_policy()

    for case in payload["cases"]:
        decision = command_policy.evaluate_command(case["command"], policy=policy)
        expected_allowed = case["expect"] == "allowed"
        assert decision.allowed is expected_allowed, case["id"]
        if expected_allowed:
            assert decision.match is None, case["id"]
            continue
        assert decision.match is not None, case["id"]
        assert decision.match.rule_id == case["rule"], case["id"]


def test_command_policy_blocks_parser_regression_forms() -> None:
    policy = _load_policy()

    expectations = {
        "echo ok\nrm -rf /": "protected-root-destruction",
        "sudo -iu root rm -rf /": "protected-root-destruction",
        "sudo FOO=1 rm -rf /": "protected-root-destruction",
        "sudo -D / rm -rf /": "protected-root-destruction",
        'env -iS "rm -rf /"': "protected-root-destruction",
        'env --split-string "rm -rf /"': "protected-root-destruction",
        'python -c "import shutil; rr = shutil.rmtree; rr(\'/\')"' : "protected-root-destruction",
        "git push --delete origin main": "repository-destruction",
        "git push --delete main": "repository-destruction",
        "git push --mirror origin": "repository-destruction",
        "git push --all --force origin": "repository-destruction",
        "git -C repo push -f origin main": "repository-destruction",
        "sudo PATH=/usr/bin git push -f origin main": "repository-destruction",
        "bash -c 'echo x>/etc/sudoers.d/x'": "account-mutation",
        "bash -c 'printf x>/etc/sudoers.d/x'": "account-mutation",
        "> /etc/sudoers.d/x echo x": "account-mutation",
    }

    for command, rule_id in expectations.items():
        decision = command_policy.evaluate_command(command, policy=policy)
        assert not decision.allowed, command
        assert decision.match is not None, command
        assert decision.match.rule_id == rule_id, command


def test_command_policy_rule_commands_drive_matching() -> None:
    payload = _load_policy_payload()
    rules = payload["rules"]
    assert isinstance(rules, list)
    no_rm_payload = dict(payload)
    no_rm_payload["rules"] = [
        ({**rule, "commands": ["find", "python"]} if rule.get("id") == "protected-root-destruction" else dict(rule))
        for rule in rules
    ]
    no_rm_policy = command_policy.parse_policy(
        no_rm_payload,
        source="<no-rm-policy>",
    )

    decision = command_policy.evaluate_command("rm -rf /", policy=no_rm_policy)
    assert decision.allowed
    assert decision.match is None

    no_python_payload = dict(payload)
    no_python_payload["rules"] = [
        ({**rule, "commands": ["rm", "find"]} if rule.get("id") == "protected-root-destruction" else dict(rule))
        for rule in rules
    ]
    no_python_policy = command_policy.parse_policy(
        no_python_payload,
        source="<no-python-policy>",
    )
    python_decision = command_policy.evaluate_command(
        'python3 -c "import shutil; shutil.rmtree(\'/\')"',
        policy=no_python_policy,
    )
    assert python_decision.allowed
    assert python_decision.match is None

    no_writer_payload = dict(payload)
    no_writer_payload["rules"] = [
        (
            {**rule, "commands": ["passwd", "useradd", "adduser", "tee"]}
            if rule.get("id") == "account-mutation"
            else dict(rule)
        )
        for rule in rules
    ]
    no_writer_policy = command_policy.parse_policy(
        no_writer_payload,
        source="<no-writer-policy>",
    )
    redirect_decision = command_policy.evaluate_command(
        "bash -c 'echo x>/etc/sudoers.d/x'",
        policy=no_writer_policy,
    )
    assert redirect_decision.allowed
    assert redirect_decision.match is None


def test_command_policy_rejects_unimplemented_rule_commands() -> None:
    payload = _load_policy_payload()
    rules = payload["rules"]
    assert isinstance(rules, list)
    bad_payload = dict(payload)
    bad_payload["rules"] = [
        ({**rule, "commands": ["rm", "find", "python", "mv"]} if rule.get("id") == "protected-root-destruction" else dict(rule))
        for rule in rules
    ]

    with pytest.raises(command_policy.CommandPolicyError, match="未實作"):
        command_policy.parse_policy(
            bad_payload,
            source="<bad-policy>",
        )


def test_command_policy_rejects_unknown_wrappers() -> None:
    payload = _load_policy_payload()
    bad_payload = dict(payload)
    bad_payload["wrappers"] = ["sudo", "git"]

    with pytest.raises(command_policy.CommandPolicyError, match="wrappers 未實作"):
        command_policy.parse_policy(
            bad_payload,
            source="<bad-wrapper-policy>",
        )


def test_operator_overlay_adds_protections_and_revalidates_policy() -> None:
    baseline = _load_policy()
    effective = command_policy.apply_operator_overlay(
        baseline,
        {"add_protected_paths": ["/mnt/operator-protected"]},
    )

    assert "/mnt/operator-protected" in effective.protected_paths
    blocked = command_policy.evaluate_command(
        "rm -rf /mnt/operator-protected",
        policy=effective,
    )
    assert not blocked.allowed
    assert blocked.match is not None
    assert blocked.match.rule_id == "protected-root-destruction"
    assert command_policy.evaluate_command(
        "rm -rf /mnt/operator-protected",
        policy=baseline,
    ).allowed


@pytest.mark.parametrize(
    "overlay",
    [
        {"protected_paths": ["/"]},
        {"add_protected_paths": ["/", 1]},
        {"rules": []},
        {"unknown": ["/tmp"]},
    ],
)
def test_operator_overlay_rejects_unknown_or_non_additive_shape(overlay) -> None:
    with pytest.raises(command_policy.CommandPolicyError):
        command_policy.apply_operator_overlay(_load_policy(), overlay)
