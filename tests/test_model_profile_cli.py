"""#452 A/D：`cortex model profile` 評測巷道測試（hermetic：patchmud 用 fake
執行檔 fixture 吐 canned report.yaml，不真 spawn patchmud）。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import model_profile as mp
from paulsha_cortex.coordinator.model_identities import _load_model_identity_file
from paulsha_cortex.porcelain import model_profile as porcelain_model

REGISTRY_V3 = """\
schema_version: 3
identities:
  - executor: agy
    model_id: gemini-3.1-pro-high
    independence_domain: google
    capabilities: [planning, review]
    live_probe: agy-plan-sandbox
  - executor: copilot
    model_id: gpt-5.4
    independence_domain: openai
    capabilities: [build]
  - executor: claude
    model_id: sonnet
    independence_domain: anthropic
    capabilities: [planning, build, review]
"""

_FAKE_PATCHMUD = """\
#!/usr/bin/env python3
import json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def opt(args, name):
    return args[args.index(name) + 1]


def main():
    cmd = sys.argv[1]
    args = sys.argv[2:]
    (HERE / "calls.log").open("a", encoding="utf-8").write(json.dumps(sys.argv[1:]) + "\\n")
    if cmd == "run":
        runs_root = Path(opt(args, "--runs-root"))
        run_dir = runs_root / opt(args, "--run-id")
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.yaml").write_text("model: " + opt(args, "--model") + "\\n", encoding="utf-8")
        print("end_reason=hidden-pass clear=True")
        return 0
    if cmd == "report":
        out = Path(opt(args, "--out"))
        out.mkdir(parents=True, exist_ok=True)
        template = (HERE / "report-template.yaml").read_text(encoding="utf-8")
        (out / "report.yaml").write_text(template, encoding="utf-8")
        print("report 輸出：" + str(out / "report.yaml"))
        return 0
    print("未知子命令", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
"""


def _report_yaml(
    *, clears: int, runs: int = 8, model: str = "anthropic:claude-sonnet-5"
) -> str:
    # 聚合鍵 model 預設用 normalize 後的完整 spec（patchmud PR #15 起 run.yaml
    # 即如此記錄）——與 CLI 別名 `sonnet` 不同，鎖住 #466 A-1「鍵值從 report
    # 本身取」的修法。
    return (
        "schema_version: 1\n"
        f"runs_included: {runs}\n"
        "runs_skipped: []\n"
        "leaderboards:\n"
        "  clear_rate:\n"
        "    status: ok\n"
        "    rows:\n"
        f"      - model: {model}\n"
        "        loadout: P0T0R0\n"
        f"        runs: {runs}\n"
        f"        clears: {clears}\n"
    )


def _pin(seed: str) -> str:
    """仿 patchmud pin 格式的 deterministic 64-hex。"""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


ENCOUNTERS = (
    "input-validation-v1",
    "input-validation-v2",
    "legacy-regression-v1",
    "legacy-regression-v2",
    "parser-edge-v1",
    "parser-edge-v2",
    "state-recovery-v1",
    "state-recovery-v2",
)


@pytest.fixture()
def fake_patchmud(tmp_path: Path) -> SimpleNamespace:
    root = tmp_path / "patchmud-root"
    deck = root / "decks" / "pilot-v1"
    for name in ENCOUNTERS:
        encounter = deck / name
        encounter.mkdir(parents=True)
        (encounter / "card.yaml").write_text(f"id: {name}\n", encoding="utf-8")
        # deck 指紋取自各 encounter 的 provenance pin（#466 A-3）。仿真 deck 的
        # provenance 形狀：pin 之外還有多行自由文字欄位（zh-TW 折行）——
        # subset YAML parser 讀不了整份文件，pin 必須以行掃描取（回歸鎖）。
        (encounter / "provenance.yaml").write_text(
            "schema_version: 1\n"
            f"issue_id: {name}\n"
            "variant_notes: 多行自由文字欄位\n"
            "  ——第二行縮排折行，subset parser 不支援的 plain scalar\n"
            f"content_sha256: {_pin(name)}\n",
            encoding="utf-8",
        )
    (root / "VERSION").write_text("0.0.1\n", encoding="utf-8")
    tools = tmp_path / "tools"
    tools.mkdir()
    bin_path = tools / "patchmud"
    bin_path.write_text(_FAKE_PATCHMUD, encoding="utf-8")
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IXUSR)
    (tools / "report-template.yaml").write_text(_report_yaml(clears=6), encoding="utf-8")
    registry = tmp_path / "model-identities.yaml"
    registry.write_text(REGISTRY_V3, encoding="utf-8")
    return SimpleNamespace(root=root, bin=bin_path, registry=registry, tools=tools)


def _options(fake: SimpleNamespace, **overrides) -> mp.ProfileOptions:
    base = dict(
        patchmud_bin=str(fake.bin),
        patchmud_root=fake.root,
        registry_file=fake.registry,
    )
    base.update(overrides)
    return mp.ProfileOptions(**base)


def _cells_by_key(result: dict) -> dict:
    return {
        (cell["executor"], cell["model_id"], cell["persona"]): cell
        for cell in result["cells"]
    }


def test_missing_patchmud_is_explicit_skip_exit_zero(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(mp.shutil, "which", lambda name: None)
    registry = tmp_path / "model-identities.yaml"
    registry.write_text(REGISTRY_V3, encoding="utf-8")
    code = porcelain_model.main(["profile", "--registry-file", str(registry)])
    assert code == 0
    out = capsys.readouterr().out
    assert "skip" in out and "patchmud" in out
    # registry 原樣未動。
    assert registry.read_text(encoding="utf-8") == REGISTRY_V3


def test_profile_preview_measures_sonnet_and_skips_unavailable_adapters(
    fake_patchmud: SimpleNamespace,
) -> None:
    result = mp.run_model_profile(_options(fake_patchmud), sleep=lambda _s: None)
    cells = _cells_by_key(result)
    # 誠實約束：patchmud 僅 anthropic adapter——copilot builder 一律 skip。
    assert cells[("copilot", "gpt-5.4", "builder")]["status"] == "skipped"
    assert cells[("copilot", "gpt-5.4", "builder")]["reason"] == "adapter-unavailable"
    # pilot-v1 只量 builder：planner／reviewer 維持 default、不假跑。
    assert cells[("claude", "sonnet", "planner")]["reason"] == "persona-dimension-unmeasured"
    assert cells[("agy", "gemini-3.1-pro-high", "reviewer")]["reason"] == (
        "persona-dimension-unmeasured"
    )
    # claude/sonnet builder：6/8 → [green, yellow] 實測提案＋diff 預覽。
    sonnet = cells[("claude", "sonnet", "builder")]
    assert sonnet["status"] == "proposed"
    assert sonnet["envelope"]["accepts_bands"] == ["green", "yellow"]
    assert "accepts_bands: [green, yellow]" in sonnet["diff"]
    assert "profile_provenance" in sonnet["diff"]
    # 未帶 --apply：不寫檔（R3 人工複核閘）。
    assert not result["applied"]
    assert fake_patchmud.registry.read_text(encoding="utf-8") == REGISTRY_V3
    # 8 關全跑不抽樣（#455 §4.3）。
    calls = [
        json.loads(line)
        for line in (fake_patchmud.tools / "calls.log").read_text(encoding="utf-8").splitlines()
    ]
    run_calls = [call for call in calls if call[0] == "run"]
    assert len(run_calls) == len(ENCOUNTERS)
    assert all(call[call.index("--model") + 1] == "sonnet" for call in run_calls)


def test_profile_apply_writes_registry_and_fingerprint_skip_then_force(
    fake_patchmud: SimpleNamespace,
) -> None:
    applied = mp.run_model_profile(_options(fake_patchmud, apply=True), sleep=lambda _s: None)
    assert applied["applied"] is True
    cells = _cells_by_key(applied)
    assert cells[("claude", "sonnet", "builder")]["status"] == "applied"
    # 寫回的檔案必須能被 loader round-trip（fail-closed 驗證過才落盤）。
    registry = _load_model_identity_file(fake_patchmud.registry)
    sonnet = registry.require("claude", "sonnet")
    assert sonnet.accepts_bands == ("green", "yellow")
    provenance = sonnet.profile_provenance
    assert provenance["fingerprint"]["deck_id"] == "pilot-v1"
    assert provenance["fingerprint"]["patchmud_version"] == "0.0.1"
    assert provenance["source"]["accepts_bands"] == "measured"
    assert provenance["source"]["invariant_ceiling"] == "default"
    # 其他三欄誠實維持 default：檔案內不得出現預設值欄位（#453 R4）。
    text = fake_patchmud.registry.read_text(encoding="utf-8")
    assert "consistency_scope:" not in text.split("profile_provenance")[0]

    # 指紋未變 → already-profiled skip（idempotent，#452 驗收 3）。
    second = mp.run_model_profile(_options(fake_patchmud), sleep=lambda _s: None)
    assert _cells_by_key(second)[("claude", "sonnet", "builder")]["status"] == "already-profiled"

    # deck 內容 pin 變更（encounter provenance 重 pin）→ 指紋不同 → 重評。
    (
        fake_patchmud.root / "decks" / "pilot-v1" / ENCOUNTERS[0] / "provenance.yaml"
    ).write_text(f"content_sha256: {_pin('mutated')}\n", encoding="utf-8")
    changed = mp.run_model_profile(_options(fake_patchmud), sleep=lambda _s: None)
    assert _cells_by_key(changed)[("claude", "sonnet", "builder")]["status"] == "proposed"

    # --force 重評（deck 還原也一樣重跑）。
    forced = mp.run_model_profile(_options(fake_patchmud, force=True), sleep=lambda _s: None)
    assert _cells_by_key(forced)[("claude", "sonnet", "builder")]["status"] == "proposed"


def test_below_green_floor_is_never_written(fake_patchmud: SimpleNamespace) -> None:
    (fake_patchmud.tools / "report-template.yaml").write_text(
        _report_yaml(clears=1), encoding="utf-8"
    )
    result = mp.run_model_profile(_options(fake_patchmud, apply=True), sleep=lambda _s: None)
    cell = _cells_by_key(result)[("claude", "sonnet", "builder")]
    # 1/8 < 1/4：空 accepts_bands 違反 #209 R2 非空契約，不得落 registry。
    assert cell["status"] == "not-writable"
    assert "below-green-floor" in cell["reason"]
    assert result["applied"] is False
    assert fake_patchmud.registry.read_text(encoding="utf-8") == REGISTRY_V3


def test_rate_limit_backoff_retries_encounters(fake_patchmud: SimpleNamespace) -> None:
    attempts: dict[str, int] = {}
    sleeps: list[float] = []

    def runner(argv, **_kwargs):
        if argv[1] == "run":
            run_id = argv[argv.index("--run-id") + 1]
            attempts[run_id] = attempts.get(run_id, 0) + 1
            if attempts[run_id] == 1:
                return SimpleNamespace(
                    returncode=2, stdout="", stderr="run 失敗：HTTP 429 rate_limit_error"
                )
            runs_root = Path(argv[argv.index("--runs-root") + 1])
            (runs_root / run_id).mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(returncode=0, stdout="clear", stderr="")
        if argv[1] == "report":
            out = Path(argv[argv.index("--out") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "report.yaml").write_text(_report_yaml(clears=8), encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    result = mp.run_model_profile(
        _options(fake_patchmud), runner=runner, sleep=sleeps.append
    )
    cell = _cells_by_key(result)[("claude", "sonnet", "builder")]
    assert cell["status"] == "proposed"
    # 每關第一次 429、退避後重試成功；退避序列為指數（2 秒起）。
    assert all(count == 2 for count in attempts.values())
    assert sleeps and all(delay == 2.0 for delay in sleeps)
    assert "encounter_failures" not in cell


def test_incomplete_deck_sample_falls_back_to_default(fake_patchmud: SimpleNamespace) -> None:
    # report 只聚到 5/8 run → incomplete-deck-sample，誠實維持 default、不落檔。
    (fake_patchmud.tools / "report-template.yaml").write_text(
        _report_yaml(clears=4, runs=5), encoding="utf-8"
    )
    result = mp.run_model_profile(_options(fake_patchmud, apply=True), sleep=lambda _s: None)
    cell = _cells_by_key(result)[("claude", "sonnet", "builder")]
    assert cell["status"] == "not-writable"
    assert cell["reason"] == "incomplete-deck-sample"
    assert fake_patchmud.registry.read_text(encoding="utf-8") == REGISTRY_V3


def test_report_group_key_taken_from_report_not_alias(
    fake_patchmud: SimpleNamespace,
) -> None:
    """#466 A-1：run.yaml 記 normalize 後的完整 spec，鍵值必須從 report 取。

    fixture 模板的聚合鍵是 `anthropic:claude-sonnet-5`（≠ CLI 別名 `sonnet`）；
    別名查表的舊實作在此必落 identity-not-in-report。"""
    result = mp.run_model_profile(_options(fake_patchmud), sleep=lambda _s: None)
    cell = _cells_by_key(result)[("claude", "sonnet", "builder")]
    assert cell["status"] == "proposed"
    assert cell["observation"]["model"] == "anthropic:claude-sonnet-5"


def test_report_with_multiple_groups_is_explicit_failure(
    fake_patchmud: SimpleNamespace,
) -> None:
    """profile 的 runs_root 為單一身分專用：report 多於一組聚合鍵＝污染，
    fail-closed 明確報錯，不得猜一組來映射。"""
    template = _report_yaml(clears=6) + (
        "      - model: agy:gemini-3.1-pro\n"
        "        loadout: P0T0R0\n"
        "        runs: 8\n"
        "        clears: 8\n"
    )
    (fake_patchmud.tools / "report-template.yaml").write_text(template, encoding="utf-8")
    result = mp.run_model_profile(_options(fake_patchmud, apply=True), sleep=lambda _s: None)
    cell = _cells_by_key(result)[("claude", "sonnet", "builder")]
    assert cell["status"] == "failed"
    assert cell["reason"] == "report-group-ambiguous"
    assert fake_patchmud.registry.read_text(encoding="utf-8") == REGISTRY_V3


def test_report_row_without_model_is_explicit_failure(
    fake_patchmud: SimpleNamespace,
) -> None:
    """malformed row（缺 model）不得被 str(None) 誤當成合法聚合鍵（review）。"""
    template = (
        "schema_version: 1\n"
        "runs_included: 8\n"
        "runs_skipped: []\n"
        "leaderboards:\n"
        "  clear_rate:\n"
        "    status: ok\n"
        "    rows:\n"
        "      - loadout: P0T0R0\n"
        "        runs: 8\n"
        "        clears: 6\n"
    )
    (fake_patchmud.tools / "report-template.yaml").write_text(template, encoding="utf-8")
    result = mp.run_model_profile(_options(fake_patchmud, apply=True), sleep=lambda _s: None)
    cell = _cells_by_key(result)[("claude", "sonnet", "builder")]
    assert cell["status"] == "failed"
    assert cell["reason"] == "report-group-ambiguous"
    assert "缺非空 model" in cell["detail"]
    assert fake_patchmud.registry.read_text(encoding="utf-8") == REGISTRY_V3


def test_deck_fingerprint_stable_across_timings_overwrite(
    fake_patchmud: SimpleNamespace,
) -> None:
    """#466 A-3：`patchmud validate-deck` 覆寫 reference_timings（或殘留快取）
    不得使 deck 指紋漂移；指紋只跟 encounter provenance pin 走。"""
    deck_dir = fake_patchmud.root / "decks" / "pilot-v1"
    before = mp.deck_content_sha256(deck_dir)

    hidden = deck_dir / ENCOUNTERS[0] / "hidden"
    hidden.mkdir()
    (hidden / "reference_timings.yaml").write_text(
        "schema_version: 1\ntimings_ms: {}\n", encoding="utf-8"
    )
    assert mp.deck_content_sha256(deck_dir) == before

    (deck_dir / ENCOUNTERS[0] / "provenance.yaml").write_text(
        f"content_sha256: {_pin('repinned')}\n", encoding="utf-8"
    )
    assert mp.deck_content_sha256(deck_dir) != before


def test_profile_runs_archived_durably_and_traceable(
    fake_patchmud: SimpleNamespace,
) -> None:
    """#466 A-4：run 封存落 patchmud runs/（耐久），registry provenance 的
    observation.runs_root 可回溯到仍存在的封存目錄。"""
    applied = mp.run_model_profile(_options(fake_patchmud, apply=True), sleep=lambda _s: None)
    cell = _cells_by_key(applied)[("claude", "sonnet", "builder")]
    runs_root = Path(cell["runs_root"])
    assert runs_root.is_dir()
    assert runs_root.parent == fake_patchmud.root / "runs"
    assert (runs_root / f"profile-claude-sonnet-{ENCOUNTERS[0]}").is_dir()

    registry = _load_model_identity_file(fake_patchmud.registry)
    provenance = registry.require("claude", "sonnet").profile_provenance
    assert provenance["observation"]["runs_root"] == str(runs_root)


def test_porcelain_model_profile_text_output(fake_patchmud: SimpleNamespace, capsys) -> None:
    code = porcelain_model.main(
        [
            "profile",
            "--patchmud-bin",
            str(fake_patchmud.bin),
            "--patchmud-root",
            str(fake_patchmud.root),
            "--registry-file",
            str(fake_patchmud.registry),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "adapter-unavailable" in out
    assert "未帶 --apply" in out
    assert "--- " in out and "+++ " in out  # unified diff 預覽


def test_identity_filter_accepts_both_spellings_and_narrows_cells(
    fake_patchmud: SimpleNamespace,
) -> None:
    # `/` 與 `:` 兩種拼法都接受（與 build_capability_lookup 的解析一致）。
    for spelling in ("claude/sonnet", "claude:sonnet"):
        result = mp.run_model_profile(
            _options(fake_patchmud, identity_filter=(spelling,)), sleep=lambda _s: None
        )
        labels = {(cell["executor"], cell["model_id"]) for cell in result["cells"]}
        assert labels == {("claude", "sonnet")}


def test_identity_filter_unknown_identity_is_explicit_error(
    fake_patchmud: SimpleNamespace, capsys
) -> None:
    # 對抗審查修正：--identity 打錯字不得靜默產出零 cells＋exit 0（操作者會誤
    # 信「全部已評測完」）；查無對應身分 → 明確錯誤、porcelain exit 2。
    with pytest.raises(ValueError, match="查無對應身分"):
        mp.run_model_profile(
            _options(fake_patchmud, identity_filter=("claude/sonnet-typo",)),
            sleep=lambda _s: None,
        )
    code = porcelain_model.main(
        [
            "profile",
            "--patchmud-bin",
            str(fake_patchmud.bin),
            "--patchmud-root",
            str(fake_patchmud.root),
            "--registry-file",
            str(fake_patchmud.registry),
            "--identity",
            "claude:sonnet-typo",
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "查無對應身分" in err and "claude:sonnet-typo" in err


def test_render_registry_file_roundtrips_through_subset_loader(tmp_path: Path) -> None:
    rows = [
        {
            "executor": "claude",
            "model_id": "sonnet",
            "independence_domain": "anthropic",
            "capabilities": ["planning", "build", "review"],
            "accepts_bands": ["green", "yellow"],
            "profile_provenance": {
                "fingerprint": {
                    "executor": "claude",
                    "model_id": "sonnet",
                    "persona": "builder",
                    "deck_id": "pilot-v1",
                    "deck_content_sha256": "0" * 64,
                    "patchmud_version": "0.0.1",
                },
                "source": {
                    "accepts_bands": "measured",
                    "invariant_ceiling": "default",
                    "consistency_scope": "default",
                    "acceptance_modes": "default",
                },
                "reasons": {"accepts_bands": "measured:clear-rate-ladder-v1"},
                "observation": {"runs": 8, "clears": 6, "red_pinned": False},
                "profiled_at": "2026-08-12T00:00:00Z",
            },
        }
    ]
    target = tmp_path / "registry.yaml"
    mp.write_registry_file(target, rows)
    registry = _load_model_identity_file(target)
    identity = registry.require("claude", "sonnet")
    assert identity.accepts_bands == ("green", "yellow")
    assert identity.profile_provenance["observation"]["red_pinned"] is False
    assert identity.profile_provenance["fingerprint"]["deck_content_sha256"] == "0" * 64


def test_inspect_models_displays_envelope_and_source(monkeypatch, tmp_path: Path, capsys) -> None:
    from paulsha_cortex.porcelain import inspect as porcelain_inspect

    registry_file = tmp_path / "model-identities.yaml"
    registry_file.write_text(REGISTRY_V3, encoding="utf-8")
    registry = _load_model_identity_file(registry_file)
    monkeypatch.setattr(
        "paulsha_cortex.coordinator.model_identities.load_model_identities",
        lambda *args, **kwargs: registry,
    )
    assert porcelain_inspect.main(["models", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "models"
    rows = payload["models"]
    builder_rows = [row for row in rows if row["persona"] == "builder"]
    assert {(row["executor"], row["model_id"]) for row in builder_rows} == {
        ("copilot", "gpt-5.4"),
        ("claude", "sonnet"),
    }
    for row in rows:
        assert row["source"]["accepts_bands"] == "default"
        assert row["envelope"]["invariant_ceiling"] is None

    assert porcelain_inspect.main(["models"]) == 0
    text = capsys.readouterr().out
    assert "accepts_bands" in text and "default" in text
