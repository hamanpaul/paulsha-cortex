from __future__ import annotations

import pytest

from paulsha_cortex.deck.schema import Card
from paulsha_cortex.deck.verify import DeckVerifyError, verify_card


def _card(produces):
    return Card(
        id="c",
        kind="skill",
        type="headless",
        card_class="core",
        skill_ref="x",
        produces=tuple(produces),
    )


def test_verify_pass_when_all_globs_match(tmp_path):
    (tmp_path / "reports" / "review").mkdir(parents=True)
    (tmp_path / "reports" / "review" / "2026-demo-x.md").write_text("r", encoding="utf-8")
    result = verify_card(_card(["reports/review/*demo*.md"]), "demo", root=tmp_path)
    assert result.ok and result.missing == ()


def test_verify_fail_lists_missing(tmp_path):
    result = verify_card(_card(["reports/review/*demo*.md"]), "demo", root=tmp_path)
    assert not result.ok
    assert result.missing == ("reports/review/*demo*.md",)


def test_verify_empty_produces_trivially_pass(tmp_path):
    assert verify_card(_card([]), "demo", root=tmp_path).ok


def test_verify_partial_match_lists_only_missing(tmp_path):
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "a-demo.md").write_text("r", encoding="utf-8")
    result = verify_card(
        _card(["reports/*demo*.md", "reports/verify/*demo*.md"]), "demo", root=tmp_path)
    assert not result.ok
    assert result.matched == ("reports/*demo*.md",)
    assert result.missing == ("reports/verify/*demo*.md",)


def test_verify_recursive_glob_and_missing_root(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "x-demo.md").write_text("r", encoding="utf-8")
    assert verify_card(_card(["**/*demo*.md"]), "demo", root=tmp_path).ok
    # root 不存在 → 乾淨 FAIL（列 missing），不崩潰
    result = verify_card(_card(["x/*.md"]), "demo", root=tmp_path / "nope")
    assert not result.ok


def test_verify_dotdot_escape_rejected(tmp_path):
    # 對抗審查回歸：.. 逃逸 root 不得變成假陽性 pass
    with pytest.raises(DeckVerifyError, match="路徑段"):
        verify_card(_card(["../pyproject.toml"]), "demo", root=tmp_path)


def test_verify_absolute_pattern_rejected(tmp_path):
    # 對抗審查回歸：絕對路徑要有明確錯誤，不是 NotImplementedError traceback
    for bad in ("/etc/hosts", "~/x.md"):
        with pytest.raises(DeckVerifyError, match="絕對路徑"):
            verify_card(_card([bad]), "demo", root=tmp_path)


def test_verify_unresolved_change_placeholder_rejected(tmp_path):
    # 對抗審查回歸：<change> 未提供 → 參數錯誤，不得靜默列為 missing
    with pytest.raises(DeckVerifyError, match="佔位符未解析"):
        verify_card(_card(["openspec/changes/<change>/proposal.md"]), "demo", root=tmp_path)
    # 有給 change 則正常驗收
    (tmp_path / "openspec" / "changes" / "demo-change").mkdir(parents=True)
    (tmp_path / "openspec" / "changes" / "demo-change" / "proposal.md").write_text(
        "p", encoding="utf-8")
    result = verify_card(
        _card(["openspec/changes/<change>/proposal.md"]), "demo",
        root=tmp_path, change="demo-change")
    assert result.ok


def test_verify_rejects_unsafe_task_slug_and_change(tmp_path):
    with pytest.raises(DeckVerifyError, match="task_slug 名稱不合法"):
        verify_card(_card(["reports/review/*<task-slug>*.md"]), "demo*", root=tmp_path)
    with pytest.raises(DeckVerifyError, match="change 名稱不合法"):
        verify_card(
            _card(["openspec/changes/<change>/proposal.md"]),
            "demo",
            root=tmp_path,
            change="*",
        )
