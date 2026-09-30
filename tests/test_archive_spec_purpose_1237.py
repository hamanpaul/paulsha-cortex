"""#1237：archive 為新 capability 留下的 TBD Purpose 由 Manager 以 proposal 的 Why 補上。"""
from __future__ import annotations

from pathlib import Path

from paulsha_cortex.coordinator import work_bridge

CHANGE = "deployment-canary-probe"
WHY = (
    "Deployment canary 需要一個小而可 merge 的行為變更，\n"
    "驗證 Cortex 從 intake 到 ship 的完整路徑。"
)


def _archived(root: Path, *, why: str | None = WHY, placeholder_change: str = CHANGE) -> Path:
    archive = root / "openspec" / "changes" / "archive" / f"2026-09-30-{CHANGE}"
    archive.mkdir(parents=True)
    proposal = "## Why\n\n" + why + "\n\n## What Changes\n\n- item\n" if why else "## What Changes\n\n- item\n"
    (archive / "proposal.md").write_text(proposal, encoding="utf-8")
    spec = root / "openspec" / "specs" / "canary-probe" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text(
        "# canary-probe Specification\n\n## Purpose\n"
        f"{work_bridge._archived_spec_purpose_placeholder(placeholder_change)}\n\n"
        "## Requirements\n\n### Requirement: Blank label fallback\n",
        encoding="utf-8",
    )
    return spec


def test_placeholder_is_replaced_with_the_proposal_why(tmp_path: Path) -> None:
    spec = _archived(tmp_path)

    filled = work_bridge._fill_archived_spec_purposes(tmp_path, change=CHANGE)

    assert filled == ("openspec/specs/canary-probe/spec.md",)
    text = spec.read_text(encoding="utf-8")
    assert "TBD - created by archiving change" not in text
    assert (
        "## Purpose\nDeployment canary 需要一個小而可 merge 的行為變更，"
        "驗證 Cortex 從 intake 到 ship 的完整路徑。\n\n## Requirements"
    ) in text


def test_spec_without_placeholder_is_left_alone(tmp_path: Path) -> None:
    spec = _archived(tmp_path)
    spec.write_text("# canary-probe Specification\n\n## Purpose\nReal purpose.\n", encoding="utf-8")

    assert work_bridge._fill_archived_spec_purposes(tmp_path, change=CHANGE) == ()
    assert spec.read_text(encoding="utf-8").endswith("Real purpose.\n")


def test_proposal_without_why_leaves_the_placeholder(tmp_path: Path) -> None:
    spec = _archived(tmp_path, why=None)
    before = spec.read_text(encoding="utf-8")

    assert work_bridge._fill_archived_spec_purposes(tmp_path, change=CHANGE) == ()
    assert spec.read_text(encoding="utf-8") == before


def test_placeholder_of_another_change_is_left_alone(tmp_path: Path) -> None:
    spec = _archived(tmp_path, placeholder_change="other-change")
    before = spec.read_text(encoding="utf-8")

    assert work_bridge._fill_archived_spec_purposes(tmp_path, change=CHANGE) == ()
    assert spec.read_text(encoding="utf-8") == before


def test_ascii_lines_are_joined_with_a_space(tmp_path: Path) -> None:
    spec = _archived(tmp_path, why="Canary needs a small change\nthat exercises the full path.")

    work_bridge._fill_archived_spec_purposes(tmp_path, change=CHANGE)

    assert "## Purpose\nCanary needs a small change that exercises the full path.\n" in spec.read_text(
        encoding="utf-8"
    )
