---
status: accepted
work_item: planning-kind-bound-exact-match
---

# Planning 產出目的地精確綁定（`planning_kind_bound` 去除 substring glob）規格

## Requirements

對應 [#812](https://github.com/hamanpaul/paulsha-cortex/issues/812)：#802（[spec](planning-artifact-manifest-binding-spec.md)）落地的 `planning_kind_bound` 以 `*<work_id>*` substring glob 比對，work item `feat-work-gc` 的 planner 可以寫進真實 work item `feat-work-gc-v2` 的 canonical 目的地，任意前綴／後綴也一併放行。同一個洞另有兩個入口：`_publish_planning_artifacts` 的 `manifest_bound`（combo manifest 的 `docs/superpowers/{specs,plans}/*<task-slug>*…` 樣板，`<task-slug>` 即 work_id）與 `_validated_brainstorm_planning_authority` 的 `declared_patterns`。三個入口必須一起關，只改 `planning_kind_bound` 不足以滿足 #812 驗收。

1. **R1 目的地文法改為精確 stem**：`planning_kind_bound(kind, path_value, work_id, *, anchor_slugs=())` 在既有守衛（`kind ∈ {spec, design, plan}`、`work_id` 符合 `[a-z0-9][a-z0-9-]*`、非絕對、不含 `..`、`Path(path_value).as_posix() == path_value`、恰四段、目錄家族正確、`.md`）全部通過後，只在 basename 符合下列之一時回 `True`：
   - kind 為 spec／design：`<base>-<kind>.md`（目錄 `docs/superpowers/specs`）
   - kind 為 plan：`<base>.md` 或 `<base>-plan.md`（目錄 `docs/superpowers/plans`）

   其中 `<base>` ∈ `{work_id}` ∪ `{anchor_slugs 中符合 slug 正則者}` ∪ `{YYYY-MM-DD-<work_id>}`；日期前綴只加在 `work_id` 前，且只接受 ASCII 數字（`[0-9]{4}-[0-9]{2}-[0-9]{2}-`，不得用會吃全形／其他 Unicode 數字的 `\d`）。plan 的兩種形式是集合成員判定，不是無條件剝除：令 `stem = name[:-len(".md")]`，`stem ∈ <base> 集合`，或 `stem` 以 `-plan` 結尾且 `stem[:-len("-plan")] ∈ <base> 集合`，任一成立即為 `True`。因此 `work_id="demo-plan"` 的 `plans/demo-plan.md`（`<base>.md`）與 `plans/demo-plan-plan.md`（`<base>-plan.md`）都接受，與 main 現行結果相同，不得回歸。其他前綴、後綴、中段、`-v2` 家族一律回 `False`；`anchor_slugs` 內不符 slug 正則的值直接忽略，不得造成放寬。函式不再使用 `fnmatch`。
2. **R2 anchor 取自 run 的確定值**：新增 `_planning_anchor_slugs(run) -> tuple[str, ...]`，回傳排序、去重後的 slug：`run.openspec_refs` 中符合 slug 正則者，加上 `run.planning_authority` 中 `work_id == run.work_id` 且 `ref` 恰為 `docs/superpowers/workstreams/<slug>/todo.md`（恰五段）的 `<slug>`（同樣過濾 slug 正則）。這兩個來源對應 `planning_runtime._planning_destinations` 的兩個錨點（openspec change 優先、workstream todo fallback），所以 change slug ≠ work_id 的合法目的地（例：`workflow-execution-identity-producer` 的 change 為 `cortex-refine-complete`；`fix-persona-catalog-portability-v2` 的 change 為 `2026-08-04-fix-persona-catalog-portability`）會被接受。
3. **R3 docs 家族只認 kind-bound**：`_publish_planning_artifacts` 新增 keyword 參數 `anchor_slugs: tuple[str, ...] = ()`。路徑落在 `docs/superpowers/specs`／`docs/superpowers/plans`（既有 `docs_bound`）時，只看 `planning_kind_bound(row["kind"], path, work_id, anchor_slugs=anchor_slugs)`，`manifest_bound` 不能再單獨放行；`openspec/changes/…` 分支維持 `openspec_bound and manifest_bound`，與現行等價（現行 `kind_bound` 對 openspec 路徑恆為 `False`）。其餘拒絕條件（絕對、`..`、非 `.md`、symlink、authority 缺席／drift、content assessment）的順序、行為與訊息都不變，路徑被拒一律仍是 `planning artifact path outside governed roots`。唯一刻意的訊息變化：docs 家族路徑帶 `spec`／`design`／`plan` 以外的 kind（例 `notes`）時，`planning_kind_bound` 回 `False`，在路徑判定就以 `outside governed roots` 被拒，不再走到 `assess_planning_artifact` 的 `unknown planning artifact kind: <kind>`（main 現行在 small-fix manifest 下 `kind="notes"` 寫 `specs/<work_id>-spec.md` 是後者）。兩者都是寫入被拒，`openspec/changes/…` 路徑帶未知 kind 的訊息不變。brainstorm 呼叫端（`apply_workflow_action` 內 `run_heterogeneous_brainstorm` 的 `artifact_writer`）傳入 `anchor_slugs=_planning_anchor_slugs(run)`。
4. **R4 authority 重驗走同一判準**：`_validated_brainstorm_planning_authority` 對不在 persisted authority 內的新 ref：docs 家族 ref（`Path(ref).parts[:3]` 為 `("docs","superpowers","specs")` 或 `("docs","superpowers","plans")`）只用 `planning_kind_bound(kind, ref, run.work_id, anchor_slugs=_planning_anchor_slugs(run))` 判定；其他 ref 維持 `declared_patterns` 的 fnmatch。已在 `run.planning_authority` 內的 ref（`existing is not None` 分支）與 #418 materialized plan 副本（`missing` 差集判定）行為不變。拒收訊息沿用 `workflow brainstorm artifact outside planner outputs: ref=…`。
5. **R5 守衛釘住**：新測試為 `planning_kind_bound` 的 `work_id` slug 正則、四段、`as_posix` 正規化三條守衛各給一個「只有該守衛會拒」的輸入（拿掉該守衛就變 `True`），`anchor_slugs` 的 slug 過濾也照此釘住。`is_absolute()` 與 `..` 兩條在 POSIX 下已被「四段＋目錄家族」守衛完全涵蓋（絕對路徑的 `parts[0]` 是 `/`；`parts[3] == ".."` 過不了 `.md` 後綴），屬 equivalent mutant，任何 black-box 測試都殺不掉，因此不要求殺掉；改以行為測試釘住「絕對路徑、`..` 必拒」，並把函式內註解改寫成如實描述（刪掉「basename glob 由守衛兜底」的說法，註明這兩條是 defense-in-depth）。
6. **R6 #802 文件與 changelog 更正**：`docs/superpowers/specs/planning-artifact-manifest-binding-spec.md` 第一條 MUST 與 `planning-artifact-manifest-binding-design.md` 的實作點改寫為 R1 文法，並明寫不得以 substring glob 比對（#812）。`CHANGELOG.md [Unreleased]` 與 `changelog.d/planning-artifact-manifest-binding.md` 的 #802 條目更正兩處：DiagnosticReason v2 的降級警語改為「任何 `schema_version: 2` 記錄（不論是否帶 `next_step_hint`）都會被舊版 `__post_init__` 拒收；Manager 一旦寫過 v2 `needs_human_reason` 就不可降級」；kind-bound 那句改為指向 #812 的精確 stem 綁定。
7. **R7 測試**：新增 `tests/test_planning_kind_bound_exact_match.py`，RED→GREEN：
   - (a) `work_id="feat-work-gc"` 對 `docs/superpowers/specs/feat-work-gc-v2-spec.md`、`…-v2-design.md`、`docs/superpowers/plans/feat-work-gc-v2.md`、`plans/feat-work-gc-v2-plan.md`、前綴 `evil-feat-work-gc-spec.md`、中段 `x-feat-work-gc-y-spec.md`、後綴 `feat-work-gc-extra-spec.md` 全部 `False`。
   - (b) 同一批路徑以 fix-standard、small-fix、feature-oneshot 三種 manifest 的 `allowed_refs` 呼叫 `_publish_planning_artifacts`，全部 raise `outside governed roots`，且目的地不落檔。
   - (c) kind 跨界（kind=spec 寫 `<work_id>-design.md`）在 small-fix、feature-oneshot manifest 下被拒；docs 路徑帶未知 kind（`kind="notes"` 寫 `docs/superpowers/specs/<work_id>-spec.md`）raise `outside governed roots`（R3 的唯一訊息變化）。
   - (d) 以下目的地被接受，並可經 `_validated_brainstorm_planning_authority` 重驗成功：canonical、日期前綴、`plans/<work_id>-plan.md`、anchor slug（`openspec_refs` 的 change ≠ work_id）；同一 anchor 目的地不帶 `anchor_slugs` 時被拒。另以 `work_id="demo-plan"` 直呼 `planning_kind_bound("plan", …)`：`plans/demo-plan.md` 與 `plans/demo-plan-plan.md` 都是 `True`，`plans/demo.md` 為 `False`。
   - (e) brainstorm evidence 的新 ref 為 `feat-work-gc-v2-spec.md`（檔案已在磁碟、hash 相符）時，small-fix manifest 的 run 重驗 raise `outside planner outputs`。
   - (f) R5 的守衛輸入，外加全形數字日期前綴被拒。
   - (g) `_planning_anchor_slugs`：openspec 來源、workstream todo 來源、非法 slug、他人 `work_id` 的 authority、非五段 workstream ref 的過濾。

   `tests/test_planning_artifact_manifest_binding_802.py::test_planning_kind_bound_accepts_glob_bound_destinations` 的 prefix／suffix／middle 三個 `is True` 斷言改成 `is False`（這個測試原本就是在釘住本漏洞），canonical／dated 斷言保留；其他既有測試不改斷言。

## Boundary

Production 只改 `paulsha_cortex/coordinator/manager.py`：`planning_kind_bound`、新增 `_planning_anchor_slugs`、`_publish_planning_artifacts`、`_validated_brainstorm_planning_authority`，以及 brainstorm `artifact_writer` 的呼叫端。以下不改：`planning_runtime._planning_destinations`；deck 卡片／combo 的 `*<task-slug>*` 樣板，以及 build 端 declared input glob 與 gate 存在性判準（#937 6.4）；`openspec_bound` 的 `parts[2] == work_id` 規則（#937）；`_materialize_plan_card_output`；#847 的 source ownership guard；`DiagnosticReason` schema 與 `from_dict`／`__post_init__`（只更正文字）。不新增 CLI、不改 persisted schema、不改 planning failure 分類。

## Evidence

- #812 實測表（main `0f57165c` 直呼）：`feat-work-gc`→`feat-work-gc-v2-spec.md`、`trust-root-executor-hardening`→`…-v2-spec.md`、`agy-builder-support`→`evil-agy-builder-support-backdoor-spec.md` 都是 `True`；`.cortex/work-items.yaml` 裡 `feat-work-gc` 與 `feat-work-gc-v2` 兩個 work item 並存。
- main `7fa4716b`：`paulsha_cortex/coordinator/manager.py:8933-8973` 的 `planning_kind_bound` 仍是 `pattern = f"docs/superpowers/specs/*{work_id}*-{kind}.md"`／`f"docs/superpowers/plans/*{work_id}*.md"` 加 `fnmatch.fnmatch`；`manager.py:9018-9027` 是 `not (manifest_bound or kind_bound)`；`manager.py:3436-3444` 的新 ref 以 `declared_patterns` 或 `planning_kind_bound` 放行；`manager.py:13188-13203` 呼叫端只傳 `work_id`。
- 本次 offline 重現（main `7fa4716b`，scratch 目錄直呼）：`_publish_planning_artifacts(work_id="feat-work-gc")` 寫 `docs/superpowers/plans/feat-work-gc-v2.md`，在 fix-standard、small-fix、feature-oneshot 三種 manifest 下都被接受；small-fix、feature-oneshot 下 kind=spec 寫 `feat-work-gc-v2-design.md` 也被接受，此時 `planning_kind_bound` 為 `False`，只靠 `manifest_bound` 放行。
- `tests/test_planning_artifact_manifest_binding_802.py:103-125` 斷言前綴／後綴／中段都是 `True`。
- `.cortex/work-items.yaml` 128 個 work item 中有 25 個的 openspec change slug ≠ work_id（多數為 `YYYY-MM-DD-<id>`，另有 `workflow-execution-identity-producer`→`cortex-refine-complete`）；`work_bridge.py:427` 以 `authority.mapped_openspec[0]` 為 change；`planning_runtime.py:1545-1582` 的 `_planning_destinations` 以 openspec／workstream 錨點 slug 產出 `specs/<slug>-spec.md`、`specs/<slug>-design.md`、`plans/<slug>.md`。
- `paulsha_cortex/coordinator/diagnostics.py:139-142`：`__post_init__` 第一條檢查就是 `schema_version != DIAGNOSTIC_REASON_SCHEMA_VERSION` 拋錯；`CHANGELOG.md:297-308` 與 `changelog.d/planning-artifact-manifest-binding.md` 寫「已寫入 hint 記錄後不可降級」，低估了影響。
