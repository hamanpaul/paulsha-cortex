from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ONBOARDING_DOCS = (
    "docs/onboarding/quickstart.md",
    "docs/onboarding/upgrade.md",
    "docs/onboarding/rollback.md",
    "docs/onboarding/troubleshooting.md",
    "docs/onboarding/concepts.md",
    "docs/onboarding/admin.md",
    "docs/onboarding/runbook.md",
)
README_GUIDE_HEADING = "## 新手上手"
README_GUIDE_LINKS = tuple(ONBOARDING_DOCS)
FORBIDDEN_MARKERS = (
    "/home/paul_chen",
    "paul_chen",
    "Arcadyan",
    "arcadyan",
)
PERSONAL_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9._-])/(?:home|Users)/[A-Za-z0-9._-]+")


def _read_required(relpath: str) -> str:
    path = REPO_ROOT / relpath
    assert path.is_file(), f"{relpath} is missing"
    return path.read_text(encoding="utf-8")


def _extract_markdown_section(text: str, heading: str) -> str:
    marker = f"\n{heading}\n"
    if text.startswith(f"{heading}\n"):
        start = len(heading) + 1
    else:
        start = text.find(marker)
        assert start != -1, f"README is missing the `{heading}` guide section"
        start += len(marker)
    remainder = text[start:]
    next_heading = re.search(r"^##\s+", remainder, flags=re.MULTILINE)
    return remainder[: next_heading.start()] if next_heading else remainder


def test_onboarding_docs_contract_is_documented() -> None:
    readme = _read_required("README.md")
    guide = _extract_markdown_section(readme, README_GUIDE_HEADING)
    for relpath in README_GUIDE_LINKS:
        assert relpath in guide, f"README guide must link `{relpath}`"

    quickstart = _read_required("docs/onboarding/quickstart.md")
    assert "pipx install" in quickstart
    assert "cortex bootstrap" in quickstart
    assert "workflow" in quickstart
    assert 'cortex ready --specs-dir "$HOME/.agents/specs"' in quickstart
    assert any(
        command in quickstart for command in ("cortex init-sample", "cortex deck compile", "cortex tick")
    ), "quickstart must show how to reach the first workflow"

    upgrade = _read_required("docs/onboarding/upgrade.md")
    rollback = _read_required("docs/onboarding/rollback.md")
    for relpath, text in (
        ("docs/onboarding/upgrade.md", upgrade),
        ("docs/onboarding/rollback.md", rollback),
    ):
        assert "pipx install --force" in text, f"{relpath} must cover the pipx reinstall path"

    troubleshooting = _read_required("docs/onboarding/troubleshooting.md")
    for keyword in ("manager degraded", "request timeout", "F8", "systemd", "executor", "F34", "venv"):
        assert keyword in troubleshooting, f"troubleshooting must cover `{keyword}`"

    concepts = _read_required("docs/onboarding/concepts.md")
    for keyword in ("spec", "job", "slice", "work"):
        assert keyword in concepts, f"concepts must define `{keyword}`"

    admin = _read_required("docs/onboarding/admin.md")
    for keyword in ("cortex service", "cortex inspect"):
        assert keyword in admin, f"admin must mention `{keyword}`"

    runbook = _read_required("docs/onboarding/runbook.md")
    for keyword in ("manager degraded", "request timeout", "systemd", "executor", "venv"):
        assert keyword in runbook, f"runbook must operationalize `{keyword}`"


def test_onboarding_docs_use_repo_safe_paths_only() -> None:
    readme = _read_required("README.md")
    guide = _extract_markdown_section(readme, README_GUIDE_HEADING)
    for relpath in (*ONBOARDING_DOCS,):
        text = _read_required(relpath)
        assert not PERSONAL_ABSOLUTE_PATH_RE.search(text), f"{relpath} must not contain personal absolute paths"
        for marker in FORBIDDEN_MARKERS:
            assert marker not in text, f"{relpath} must not contain `{marker}`"

    assert not PERSONAL_ABSOLUTE_PATH_RE.search(guide), "README onboarding guide must avoid personal absolute paths"
    for marker in FORBIDDEN_MARKERS:
        assert marker not in guide, f"README onboarding guide must not contain `{marker}`"
