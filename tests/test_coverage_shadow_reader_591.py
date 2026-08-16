"""#591：R1 shadow telemetry 的 aggregation reader ＋ retention 的測試。

reader 是 R1 Go/No-Go（「兩週 telemetry 中所有 disagreement 可解釋」）的直接輸入，
因此這裡釘住三件事：統計正確性（含分組與樣本明細）、TTL 清掃、壞檔容錯絕不炸掉
整份報告。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from paulsha_cortex.coordinator import coverage
from paulsha_cortex.coordinator.coverage import (
    DEFAULT_SHADOW_TTL_SECONDS,
    SHADOW_REPORT_SCHEMA,
    UNKNOWN_DISAGREEMENT_KIND,
    build_shadow_report,
    main,
    run_coverage_shadow,
)
from paulsha_cortex.coordinator.work_bridge import default_workflow_manifest
from paulsha_cortex.coordinator.workflow import WorkflowManifest

NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)


def _stamp(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def _write(
    root,
    name: str,
    *,
    agreement: bool,
    days_ago: float = 1,
    kind: str | None = "topology-fail-coverage-pass",
    combo: str = "feature-oneshot",
    task_slug: str = "demo-work",
    callsite: str = "manager.start",
    missing: tuple[str, ...] = (),
    work_id: str = "w-1",
) -> None:
    """寫一筆合成 telemetry（欄位集與 `ShadowComparison.to_dict()` 同型）。"""
    disagreement = None
    if not agreement:
        disagreement = {
            "topology_reason": "workflow steps 未依 phase 順序排列",
            "coverage_reason": None,
            "missing_responsibilities": list(missing),
        }
        if kind is not None:
            disagreement["kind"] = kind
    payload = {
        "schema_version": coverage.SHADOW_TELEMETRY_SCHEMA,
        "recorded_at": _stamp(days_ago),
        "callsite": callsite,
        "manifest": {
            "combo": combo,
            "task_slug": task_slug,
            "version": 1,
            "steps": 7,
        },
        "context": {"work_id": work_id, "repo": "owner/repo"},
        "topology": {"passed": agreement, "reason": None if agreement else "順序錯"},
        "coverage": {
            "passed": True,
            "reason": None,
            "required": ["intake", "specification"],
            "covered": ["intake", "specification"],
            "missing": list(missing),
            "satisfied_by": {"intake": ["claim-card"], "specification": ["define-card"]},
        },
        "agreement": agreement,
        "disagreement": disagreement,
    }
    (root / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# 統計正確性
# --------------------------------------------------------------------------


def test_report_counts_agreement_and_disagreement(tmp_path) -> None:
    _write(tmp_path, "a1.json", agreement=True)
    _write(tmp_path, "a2.json", agreement=True)
    _write(tmp_path, "a3.json", agreement=True)
    _write(tmp_path, "d1.json", agreement=False)
    _write(tmp_path, "d2.json", agreement=False)

    report = build_shadow_report(root=tmp_path, now=NOW)

    assert report.total == 5
    assert report.agreements == 3
    assert report.disagreements == 2
    assert report.agreement_rate == pytest.approx(0.6)
    assert report.corrupt == ()
    assert report.swept == ()


def test_disagreements_group_by_kind_with_distributions(tmp_path) -> None:
    _write(tmp_path, "d1.json", agreement=False, combo="feature-oneshot", task_slug="w-a")
    _write(tmp_path, "d2.json", agreement=False, combo="feature-oneshot", task_slug="w-b")
    _write(
        tmp_path,
        "d3.json",
        agreement=False,
        combo="hotfix",
        task_slug="w-a",
        callsite="work_bridge.default",
        missing=("review",),
    )

    report = build_shadow_report(root=tmp_path, now=NOW)

    assert len(report.groups) == 1
    group = report.groups[0]
    assert group.kind == "topology-fail-coverage-pass"
    assert group.count == 3
    # 分佈依 count 由大到小、同 count 依名稱字典序。
    assert group.combos == {"feature-oneshot": 2, "hotfix": 1}
    assert group.task_slugs == {"w-a": 2, "w-b": 1}
    assert group.callsites == {"manager.start": 2, "work_bridge.default": 1}
    assert group.missing_responsibilities == {"review": 1}


def test_multiple_kinds_are_ordered_by_count(tmp_path) -> None:
    _write(tmp_path, "d1.json", agreement=False, kind="topology-fail-coverage-pass")
    _write(tmp_path, "d2.json", agreement=False, kind="topology-fail-coverage-pass")
    _write(tmp_path, "d3.json", agreement=False, kind="topology-pass-coverage-fail")

    report = build_shadow_report(root=tmp_path, now=NOW)

    assert [group.kind for group in report.groups] == [
        "topology-fail-coverage-pass",
        "topology-pass-coverage-fail",
    ]
    assert [group.count for group in report.groups] == [2, 1]


def test_disagreement_without_kind_falls_into_unknown_group(tmp_path) -> None:
    _write(tmp_path, "d1.json", agreement=False, kind=None)

    report = build_shadow_report(root=tmp_path, now=NOW)

    assert [group.kind for group in report.groups] == [UNKNOWN_DISAGREEMENT_KIND]


def test_samples_carry_enough_detail_to_explain_a_case(tmp_path) -> None:
    _write(tmp_path, "d1.json", agreement=False, work_id="w-42")

    group = build_shadow_report(root=tmp_path, now=NOW).groups[0]
    sample = group.samples[0]

    assert sample["file"] == "d1.json"
    assert sample["callsite"] == "manager.start"
    assert sample["combo"] == "feature-oneshot"
    assert sample["task_slug"] == "demo-work"
    assert sample["context"]["work_id"] == "w-42"
    assert sample["topology_reason"] == "workflow steps 未依 phase 順序排列"
    assert sample["satisfied_by"]["intake"] == ["claim-card"]


def test_sample_limit_caps_details_without_capping_counts(tmp_path) -> None:
    for index in range(4):
        _write(tmp_path, f"d{index}.json", agreement=False)

    limited = build_shadow_report(root=tmp_path, sample_limit=1, now=NOW).groups[0]
    unlimited = build_shadow_report(root=tmp_path, sample_limit=0, now=NOW).groups[0]

    assert limited.count == unlimited.count == 4
    assert len(limited.samples) == 1
    assert len(unlimited.samples) == 4


def test_window_spans_earliest_and_latest_records(tmp_path) -> None:
    _write(tmp_path, "old.json", agreement=True, days_ago=9)
    _write(tmp_path, "mid.json", agreement=True, days_ago=4)
    _write(tmp_path, "new.json", agreement=True, days_ago=0.5)

    report = build_shadow_report(root=tmp_path, now=NOW)

    assert report.earliest == _stamp(9)
    assert report.latest == _stamp(0.5)


def test_empty_and_missing_root_report_zero_without_faking_a_rate(tmp_path) -> None:
    missing = build_shadow_report(root=tmp_path / "never-written", now=NOW)
    assert missing.root_exists is False
    assert missing.total == 0
    assert missing.agreement_rate is None

    present = build_shadow_report(root=tmp_path, now=NOW)
    assert present.root_exists is True
    assert present.total == 0


def test_dotfiles_and_temp_files_are_ignored(tmp_path) -> None:
    _write(tmp_path, "good.json", agreement=True)
    (tmp_path / ".coverage-half.tmp").write_text("{partial", encoding="utf-8")
    (tmp_path / ".hidden.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not telemetry", encoding="utf-8")

    report = build_shadow_report(root=tmp_path, now=NOW)

    assert report.total == 1
    assert report.corrupt == ()


# --------------------------------------------------------------------------
# 壞檔容錯
# --------------------------------------------------------------------------


def test_corrupt_records_are_skipped_and_counted(tmp_path) -> None:
    _write(tmp_path, "good.json", agreement=True)
    (tmp_path / "truncated.json").write_text('{"schema_version": "1"', encoding="utf-8")
    (tmp_path / "not-an-object.json").write_text("[1, 2, 3]", encoding="utf-8")
    (tmp_path / "empty.json").write_text("", encoding="utf-8")

    report = build_shadow_report(root=tmp_path, now=NOW)

    assert report.total == 1
    assert set(report.corrupt) == {"truncated.json", "not-an-object.json", "empty.json"}
    # 壞檔不進統計母體，也不改變 agreement 比例。
    assert report.agreement_rate == pytest.approx(1.0)


def test_record_with_broken_inner_shapes_still_reports(tmp_path) -> None:
    (tmp_path / "weird.json").write_text(
        json.dumps(
            {
                "recorded_at": 12345,
                "callsite": None,
                "manifest": "not-a-mapping",
                "context": ["not", "a", "mapping"],
                "coverage": None,
                "agreement": False,
                "disagreement": {"kind": "topology-fail-coverage-pass"},
            }
        ),
        encoding="utf-8",
    )

    report = build_shadow_report(root=tmp_path, now=NOW)

    assert report.total == 1
    group = report.groups[0]
    assert group.combos == {"-": 1}
    assert group.callsites == {"-": 1}
    assert group.samples[0]["context"] == {}
    # 型別再怎麼歪，render 都不能炸。
    assert "topology-fail-coverage-pass" in report.render_text()


# --------------------------------------------------------------------------
# TTL 清掃（retention）
# --------------------------------------------------------------------------


def test_expired_records_are_swept_and_excluded_from_stats(tmp_path) -> None:
    _write(tmp_path, "fresh.json", agreement=True, days_ago=2)
    _write(tmp_path, "stale.json", agreement=False, days_ago=45)

    report = build_shadow_report(root=tmp_path, now=NOW)

    assert report.swept == ("stale.json",)
    assert not (tmp_path / "stale.json").exists()
    assert (tmp_path / "fresh.json").exists()
    assert report.total == 1
    assert report.disagreements == 0
    assert report.ttl_seconds == DEFAULT_SHADOW_TTL_SECONDS


def test_ttl_boundary_keeps_records_inside_the_window(tmp_path) -> None:
    _write(tmp_path, "just-inside.json", agreement=True, days_ago=29.9)
    _write(tmp_path, "just-outside.json", agreement=True, days_ago=30.1)

    report = build_shadow_report(root=tmp_path, now=NOW)

    assert report.swept == ("just-outside.json",)
    assert report.total == 1


def test_custom_ttl_is_honoured(tmp_path) -> None:
    _write(tmp_path, "a.json", agreement=True, days_ago=8)

    report = build_shadow_report(root=tmp_path, ttl_seconds=7 * 86_400, now=NOW)

    assert report.swept == ("a.json",)
    assert report.total == 0


def test_no_sweep_keeps_expired_records(tmp_path) -> None:
    _write(tmp_path, "stale.json", agreement=True, days_ago=90)

    report = build_shadow_report(root=tmp_path, ttl_seconds=None, now=NOW)

    assert report.swept == ()
    assert report.ttl_seconds is None
    assert (tmp_path / "stale.json").exists()
    # 停用清掃時，過期記錄仍計入統計母體。
    assert report.total == 1


def test_unparsable_timestamp_falls_back_to_mtime_for_sweeping(tmp_path) -> None:
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    old = (NOW - timedelta(days=99)).timestamp()
    os.utime(corrupt, (old, old))

    report = build_shadow_report(root=tmp_path, now=NOW)

    # 壞檔沒有可解析的 recorded_at，仍必須隨時間退場而不是永久堆積。
    assert report.swept == ("corrupt.json",)
    assert report.corrupt == ()
    assert not corrupt.exists()


def test_sweep_failure_is_counted_not_raised(tmp_path) -> None:
    if os.geteuid() == 0:  # pragma: no cover - CI 以 root 跑時 chmod 擋不住 unlink
        pytest.skip("root 可無視目錄權限刪檔")
    root = tmp_path / "readonly"
    root.mkdir()
    _write(root, "stale.json", agreement=True, days_ago=90)
    root.chmod(0o500)
    try:
        report = build_shadow_report(root=root, now=NOW)
    finally:
        root.chmod(0o700)

    assert report.sweep_failed == ("stale.json",)
    assert report.swept == ()
    assert (root / "stale.json").exists()


# --------------------------------------------------------------------------
# 與真實 sink 端到端
# --------------------------------------------------------------------------


def _topology_broken_manifest() -> WorkflowManifest:
    valid = default_workflow_manifest("demo-work", change=None, combo_name="feature-oneshot")
    return WorkflowManifest(
        combo=valid.combo, task_slug=valid.task_slug, steps=tuple(reversed(valid.steps))
    )


def test_reader_consumes_records_written_by_the_real_sink(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PSC_RESPONSIBILITY_COVERAGE", raising=False)
    run_coverage_shadow(
        default_workflow_manifest("demo-work", change=None, combo_name="feature-oneshot"),
        callsite="manager.start",
        context={"work_id": "w-1"},
        root=tmp_path,
    )
    run_coverage_shadow(
        _topology_broken_manifest(),
        callsite="manager.start",
        context={"work_id": "w-2"},
        root=tmp_path,
    )

    report = build_shadow_report(root=tmp_path, now=NOW)

    assert report.total == 2
    assert report.agreements == 1
    assert [group.kind for group in report.groups] == ["topology-fail-coverage-pass"]
    assert report.groups[0].samples[0]["context"] == {"work_id": "w-2"}


def test_default_root_follows_coordinator_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PSC_COORDINATOR_ROOT", str(tmp_path))
    monkeypatch.delenv("PSC_RESPONSIBILITY_COVERAGE", raising=False)
    run_coverage_shadow(
        default_workflow_manifest("demo-work", change=None, combo_name="feature-oneshot"),
        callsite="test",
    )

    report = build_shadow_report(now=NOW)

    assert report.root == tmp_path / "coverage-shadow"
    assert report.total == 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_json_output_shape(tmp_path, capsys) -> None:
    _write(tmp_path, "a.json", agreement=True)
    _write(tmp_path, "d.json", agreement=False)
    _write(tmp_path, "stale.json", agreement=True, days_ago=99)
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")

    assert main(["--report", "--root", str(tmp_path), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == SHADOW_REPORT_SCHEMA
    assert payload["root"] == str(tmp_path)
    assert payload["records"] == {
        "total": 2,
        "agreement": 1,
        "disagreement": 1,
        "agreement_rate": 0.5,
    }
    assert payload["corrupt"]["count"] == 1
    assert payload["retention"]["ttl_seconds"] == DEFAULT_SHADOW_TTL_SECONDS
    assert payload["retention"]["swept"]["files"] == ["stale.json"]
    assert payload["disagreements"][0]["kind"] == "topology-fail-coverage-pass"
    assert payload["disagreements"][0]["samples"][0]["file"] == "d.json"


def test_cli_text_output_lists_groups_and_samples(tmp_path, capsys) -> None:
    _write(tmp_path, "a.json", agreement=True)
    _write(tmp_path, "d.json", agreement=False, missing=("review",))

    assert main(["--report", "--root", str(tmp_path)]) == 0

    out = capsys.readouterr().out
    assert "coverage shadow telemetry 報告" in out
    assert "agreement 1 / disagreement 1" in out
    assert "[topology-fail-coverage-pass] 1 筆" in out
    assert "combo=feature-oneshot" in out
    assert "satisfied_by:" in out


def test_cli_text_output_says_so_when_all_agree(tmp_path, capsys) -> None:
    _write(tmp_path, "a.json", agreement=True)

    assert main(["--report", "--root", str(tmp_path)]) == 0

    assert "disagreement 分組: （無）" in capsys.readouterr().out


def test_cli_no_sweep_flag_disables_retention(tmp_path, capsys) -> None:
    _write(tmp_path, "stale.json", agreement=True, days_ago=99)

    assert main(["--report", "--root", str(tmp_path), "--no-sweep", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["retention"]["ttl_seconds"] is None
    assert (tmp_path / "stale.json").exists()


def test_cli_ttl_days_flag(tmp_path, capsys) -> None:
    _write(tmp_path, "a.json", agreement=True, days_ago=3)

    assert main(["--report", "--root", str(tmp_path), "--ttl-days", "2", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["retention"]["swept"]["files"] == ["a.json"]
    assert payload["retention"]["ttl_seconds"] == 2 * 86_400


def test_cli_requires_an_explicit_action(tmp_path, capsys) -> None:
    assert main(["--root", str(tmp_path)]) == 2
    assert "需指定 --report" in capsys.readouterr().err


def test_cli_rejects_negative_ttl(tmp_path, capsys) -> None:
    assert main(["--report", "--root", str(tmp_path), "--ttl-days", "-1"]) == 2
    assert "不可為負" in capsys.readouterr().err


def test_cli_survives_a_directory_full_of_garbage(tmp_path, capsys) -> None:
    for index in range(3):
        (tmp_path / f"junk{index}.json").write_text("nonsense", encoding="utf-8")

    assert main(["--report", "--root", str(tmp_path), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["records"]["total"] == 0
    assert payload["corrupt"]["count"] == 3
