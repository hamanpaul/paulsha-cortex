from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = REPO_ROOT / "skills" / "driving-cortex" / "SKILL.md"

HOME = Path.home()

ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9._-])/(?:home|Users)/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._~%+\\-]*)+"
)
DANGEROUS_FLAG = "--dangerously-bypass-approvals-and-sandbox"
TRIGGER_PHRASES = ("dogfood cortex", "派工 cortex", "cortex work")
SECTION_HEADINGS = (
    "## 心智模型",
    "## 開一個 dogfood 批次",
    "## 驅動桿",
    "## 執行器設定",
    "## 每批 merge 後部署",
    "## 生命週期特性",
    "## 已知坑",
)
FORBIDDEN_CONTEXT = (r"僅限", r"僅作", r"例外", r"除外", r"僅在", r"必要情境", r"緊急")
BUSINESS_IDENTIFIERS = ("hamanpaul", "arcadyan")


def _read_skill_text() -> str:
    assert SKILL_PATH.is_file(), f"missing skill file: {SKILL_PATH}"
    return SKILL_PATH.read_text(encoding="utf-8")


def _read_frontmatter(text: str) -> dict[str, object]:
    match = re.search(r"^---\n(?P<body>.*?)\n---\n", text, flags=re.DOTALL)
    assert match, "SKILL.md must include YAML frontmatter"
    data = yaml.safe_load(match.group("body")) or {}
    assert isinstance(data, dict), "SKILL.md frontmatter must be mapping"
    return data


def _assert_no_personal_or_vendor_tokens(text: str) -> None:
    normalized = text.lower()
    assert not ABSOLUTE_PATH_RE.search(text), "SKILL.md must not contain personal absolute paths"
    assert str(HOME) not in text, "SKILL.md must not contain current-user absolute path"
    assert str(REPO_ROOT) not in text, "SKILL.md should avoid hardcoded repo absolute paths"
    for token in (os.getenv("USER"), os.getenv("USERNAME")):
        if token:
            assert token not in text, f"SKILL.md must not embed username token: {token}"
    for marker in BUSINESS_IDENTIFIERS:
        assert marker not in normalized, f"SKILL.md must not embed vendor/organizational identifier: {marker}"


def _assert_unsafe_flag_context(text: str) -> None:
    if DANGEROUS_FLAG not in text:
        return
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if DANGEROUS_FLAG not in line:
            continue
        neighborhood = "\n".join(lines[max(0, idx - 1) : min(len(lines), idx + 2)])
        if not any(re.search(token, neighborhood) for token in FORBIDDEN_CONTEXT):
            assert False, (
                "Unsafe bypass flag is used as daily guide; keep it under explicit exception context"
            )


def test_skill_file_exists() -> None:
    assert SKILL_PATH.is_file(), f"missing skill file: {SKILL_PATH}"


def test_skill_has_description_frontmatter() -> None:
    text = _read_skill_text()
    meta = _read_frontmatter(text)
    description = meta.get("description")
    assert isinstance(description, str), "description missing from frontmatter"
    lower_description = description.lower()
    assert any(token in lower_description for token in TRIGGER_PHRASES), (
        "frontmatter description must include one trigger phrase"
    )


def test_skill_covers_seven_sections() -> None:
    text = _read_skill_text()
    for heading in SECTION_HEADINGS:
        assert heading in text, f"missing required heading: {heading}"


def test_skill_no_personal_paths() -> None:
    text = _read_skill_text()
    _assert_no_personal_or_vendor_tokens(text)


def test_skill_no_unsafe_bypass_as_daily_guide() -> None:
    text = _read_skill_text()
    _assert_unsafe_flag_context(text)


def test_skill_start_example_matches_porcelain_cli() -> None:
    text = _read_skill_text()
    example = "cortex run work start <work_id> --repo <owner/repo> --wait --json"
    assert example in text, "SKILL.md must use the current run work start command"
    assert "--workflow-action" not in text, "SKILL.md must not show the removed start option"

    from paulsha_cortex.porcelain.run import _build_parser

    args = _build_parser().parse_args(
        ["work", "start", "work-id", "--repo", "owner/repo", "--wait", "--json"]
    )
    assert (args.action, args.work_id, args.repo, args.wait, args.json) == (
        "start",
        "work-id",
        "owner/repo",
        True,
        True,
    )


def test_readme_documents_standard_skill_root_symlink() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "### 安裝 driving-cortex skill" in readme, "README.md must document skill discovery setup"
    section = readme.split("### 安裝 driving-cortex skill", 1)[1].split("\n### ", 1)[0]
    assert 'mkdir -p "$HOME/.agents/skills"' in section
    assert 'ln -s "$repo_root/skills/driving-cortex" "$HOME/.agents/skills/driving-cortex"' in section
