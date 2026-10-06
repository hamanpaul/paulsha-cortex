from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml


COMMAND_POLICY_MODULE = "paulsha_cortex.command_policy"
BASELINE_POLICY_PATH = Path("coordinator/data/command-policy.yaml")
CORPUS_PATH = Path("tests/fixtures/command-policy/corpus.yaml")
REQUIRED_BLOCKED_CASES = (
    r"\rm -rf /",
    "/bin/rm -rf /",
    "rm -r -f /*",
    "sudo -E rm -fr ~",
    "env -S 'rm -rf /'",
    'bash -c "rm -rf /"',
    "sh -c 'sudo passwd root'",
    "sudo useradd x",
    "sudo adduser x",
    "echo 'x ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/x",
    "find / -delete",
    'python3 -c "import shutil; shutil.rmtree(\'/\')"',
    "dd if=/dev/zero of=/dev/sda",
    "git push -f origin main",
    "git push origin :main",
    "gh repo delete o/r --yes",
)
REQUIRED_ALLOWED_CASES = (
    "rm -rf build/ node_modules",
    'echo "rm -rf /"',
    "grep passwd /etc/group",
    "sudo apt-get install -y jq",
    "dd if=a.img of=b.img",
    "python -m pytest",
)


def _string_scalars(node: object) -> set[str]:
    scalars: set[str] = set()

    def _collect(value: object) -> None:
        if isinstance(value, str):
            scalars.add(value)
            return
        if isinstance(value, dict):
            for key, nested in value.items():
                _collect(key)
                _collect(nested)
            return
        if isinstance(value, (list, tuple, set)):
            for nested in value:
                _collect(nested)

    _collect(node)
    return scalars


def test_command_policy_baseline_artifacts_cover_owner_examples() -> None:
    assert importlib.util.find_spec(COMMAND_POLICY_MODULE) is not None, (
        f"{COMMAND_POLICY_MODULE} must exist before command-policy enforcement "
        "can be wired into builder executors"
    )
    assert BASELINE_POLICY_PATH.is_file(), (
        "dangerous-command baseline policy must be versioned at "
        f"{BASELINE_POLICY_PATH}"
    )
    assert CORPUS_PATH.is_file(), (
        "command-policy corpus fixture must be versioned at "
        f"{CORPUS_PATH}"
    )

    payload = yaml.safe_load(CORPUS_PATH.read_text(encoding="utf-8"))
    scalar_strings = _string_scalars(payload)

    missing_blocked = sorted(
        command for command in REQUIRED_BLOCKED_CASES if command not in scalar_strings
    )
    missing_allowed = sorted(
        command for command in REQUIRED_ALLOWED_CASES if command not in scalar_strings
    )

    assert not missing_blocked, (
        "command-policy corpus is missing required blocked examples: "
        + ", ".join(missing_blocked)
    )
    assert not missing_allowed, (
        "command-policy corpus is missing required allowed examples: "
        + ", ".join(missing_allowed)
    )
