from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_policy_check_reacts_to_pr_metadata_changes() -> None:
    workflow = yaml.load(
        (REPO_ROOT / ".github/workflows/policy-check.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )

    event_types = set(workflow["on"]["pull_request"]["types"])
    assert event_types == {
        "opened",
        "synchronize",
        "reopened",
        "edited",
        "labeled",
        "unlabeled",
    }
