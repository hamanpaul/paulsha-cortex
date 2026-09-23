---
status: accepted
work_item: planning-kind-bound-exact-match
---

# Planning 產出目的地精確綁定設計

## Decisions

### D1 精確 stem 文法取代 substring glob

`planning_kind_bound` 保留既有守衛與順序（kind、`work_id` slug 正則、`is_absolute`、`..`、`as_posix`、四段、目錄家族），把最後的 `fnmatch.fnmatch(path_value, pattern)` 換成純字串比對：spec／design 取 `name[:-len(f"-{kind}.md")]` 當 stem（`name` 不以 `-{kind}.md` 結尾即 `False`）；plan 取 `stem = name[:-len(".md")]`，候選集合為 `{stem}`，若 stem 以 `-plan` 結尾再加入 `stem[:-len("-plan")]`（兩者並存，不是用剝除後的值取代原值）。任一候選 ∈ `{work_id} ∪ 合法 anchor_slugs`，或 `re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-" + re.escape(work_id), 候選)` 成立，即為 `True`；spec／design 的候選只有 `{stem}`。無條件剝除會讓 `work_id="demo-plan"` 的 `plans/demo-plan.md` 從 main 現行的 `True` 變成 `False`（spec R1 明訂不得回歸），所以必須用候選集合。slug 正則抽成模組常數（例 `_PLANNING_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*")`），`work_id` 與 anchor 共用。

保留兩個固定 token 的修飾，理由如下：
- **日期前綴**：#812 建議修法與 #802 既有正例（`2026-08-27-<slug>-spec.md`）都保留它；只加在 `work_id` 前，因為 `_planning_destinations` 從不幫 anchor 加日期（日期若存在，已經是 change slug 本身的一部分，例如 `2026-07-25-fix-git-runner-cwd`）。
- **plan 的 `-plan` 後綴**：與 spec／design 的 kind 後綴同一套命名；`tests/test_workflow_production_wiring.py`（十餘處，含 manifest fixture 的 `outputs` 宣告）與 `tests/test_planning_publication_transaction_536.py` 以 `docs/superpowers/plans/<work_id>-plan.md` 當 brainstorm 目的地，而同檔另有測試以 `plans/<work_id>.md` 當 materialized／既有 plan。全面改 fixture 會擴大 diff，也可能改變這些 publication／reconciliation 測試原本要釘的語意。prototype 實測（main `7fa4716b` 的 scratch 副本）：不接受 `-plan` 會多出 8 個既有測試失敗；接受後跑全套（6304 passed、15 failed），15 個失敗中 3 個是 802 glob 測試的 parametrization（屬預期，依 spec R7 改斷言），其餘 12 個（`test_architecture_docs`、`test_architecture_phase_dispatch`、`test_install_service`）是因為 scratch 副本不是 git checkout，在未改動的 baseline 副本上同樣失敗，與本修正無關。

殘餘風險（accepted-risk）：只剩「他人 work_id 恰好等於 `YYYY-MM-DD-<本 work_id>`，或等於 `<本 base>-plan`」這種精確相等的撞名，任意前綴／後綴／`-v2` 這類開放式家族已經關閉。以 main `7fa4716b` 的 `.cortex/work-items.yaml`（128 個 work item）核對：沒有任何 work_id 以 `-plan` 結尾，也沒有任何日期前綴 stem 等於他人的 openspec anchor。

### D2 anchor 來源與 `_planning_destinations` 對齊

`_planning_destinations(pack)` 的 slug 取自 question pack 的 `source_refs`：先找 `openspec/changes/<slug>/…`，沒有才退回 `docs/superpowers/workstreams/<slug>/todo.md`。question pack 由 `assess_planning_completeness(artifacts)` 從 `run.planning_authority` 的 refs 建出；run 上沒有持久化 pack 本身。因此 `_planning_anchor_slugs(run)` 直接從同一份確定值推導：`run.openspec_refs`（claim 時的 `authority.mapped_openspec`，也是 `_artifact_rows` 產生 openspec refs 的唯一來源），加上 `run.planning_authority` 中本 work item 的 workstream todo ref 的 slug。取超集是安全的：每個值都是 monitor 確認、綁在本 work item 上的 authority，planner 自己寫不進 `docs/superpowers/workstreams/`（`docs_bound` 不含該目錄），所以 anchor 集合不會被 planner 產出反向擴大。用 `getattr(run, "openspec_refs", ()) or ()` 讀，相容測試用的 `SimpleNamespace` run。

### D3 docs 家族只認 kind-bound，manifest glob 退回只管 openspec

combo manifest 對 `docs/superpowers/{specs,plans}` 宣告的 outputs 全是 `*<task-slug>*` 樣板（`fix-standard`／`small-fix`／`feature-oneshot` 的 planner outputs 經 `compile_combo` 實測），與 `planning_kind_bound` 舊樣板是同一個洞；而且 `manifest_bound` 不檢查 kind，small-fix 下 kind=spec 可以寫 `-design.md`。所以 `_publish_planning_artifacts` 的放行條件改為：

```python
if (
    relative.is_absolute()
    or ".." in relative.parts
    or not (docs_bound or openspec_bound)
    or (docs_bound and not kind_bound)
    or (openspec_bound and not manifest_bound)
    or relative.suffix != ".md"
):
    raise ValueError("planning artifact path outside governed roots")
```

openspec 分支與現行等價（現行 `kind_bound` 對 `openspec/…` 恆為 `False`，所以原本就只靠 `manifest_bound`）。planning publication 只發佈 spec／design／plan 三種 kind（`assess_planning_artifact` 對其他 kind 直接 raise），而 `_planning_destinations` 只產出這三種目的地，所以 `*-hw-evidence.md` 這類 niche 卡片 output 本來就不是 planning publication 的合法目的地。唯一可觀察的差異：docs 路徑帶未知 kind 時，錯誤訊息從 `unknown planning artifact kind` 提前變成 `outside governed roots`，兩者都是 `primary-artifact-write-rejected` 的 content 分類。spec R3 把它列為「其餘拒絕訊息不變」的唯一例外，R7(c) 以測試釘住。`allowed_refs` 參數與呼叫端推導不動（openspec 仍需要它）。

### D4 authority 重驗同步，in-flight run 相容

`_validated_brainstorm_planning_authority` 只對 `existing is None` 的新 ref 做路徑判定：docs 家族改用 `planning_kind_bound(..., anchor_slugs=_planning_anchor_slugs(run))`，其他 ref 仍用 `declared_patterns`。已持久化進 `run.planning_authority` 的 ref 走 `existing` 分支，不重做路徑判定，因此舊規則下已經 commit 的 run 在 resume reconciliation 時不受影響。唯一的例外是「檔案已發佈、registry 尚未 commit」的崩潰窗口：舊 glob 接受、新文法不接受的 ref 會在 reconciliation 被拒，落 `planning-authority-reconciliation-failed`。這是本修正要的 fail-closed，不另開相容通道。#418 materialized plan 副本（`missing` 差集，靠 byte-copy＋`plan_output_patterns` 判定）不經過本判準，行為不變。

### D5 守衛與 equivalent mutant

三條守衛的 killing input（prototype 以文字 mutation 逐條驗證）：
- `work_id` slug 正則：`("spec", "docs/superpowers/specs/Bad_Work-spec.md", "Bad_Work")`，拿掉正則時 stem 相等 → `True`。
- 四段：`docs/superpowers/specs/nested/<work_id>-spec.md`，拿掉時 `parts[:3]` 與 stem 都對 → `True`。
- `as_posix`：`docs/superpowers/./specs/<work_id>-spec.md` 與 `docs//superpowers/specs/<work_id>-spec.md`，`Path` 正規化後四段且 stem 對，拿掉時 → `True`。
- anchor slug 過濾：`anchor_slugs=("Bad_Anchor",)` 對 `Bad_Anchor-spec.md`，拿掉過濾時 → `True`。

`is_absolute()` 改成 `False`、拿掉 `..` 兩個 mutant 的輸出在所有輸入下都不變（POSIX 絕對路徑 `parts[0]` 為 `/`，過不了目錄家族；四段且家族正確時只有 `parts[3]` 可能是 `..`，過不了 `.md`），屬 equivalent mutant。#812 回報「四個 mutation 全 GREEN」裡的 `is_absolute` 一項因此無法以測試殺掉；本設計以行為測試釘住絕對路徑與 `..` 必拒，並在函式註解明寫它們是 defense-in-depth，不再宣稱「glob 由守衛兜底」。

### D6 #802 文件與 changelog 更正

#802 的 accepted spec 第一條 MUST 寫死 `*<work_id>*` 樣板，不改的話下一輪實作會被同一條 spec 逼回過寬形態（#812「這條是怎麼進 main 的」）。#802 已 CLOSED，openspec change 已 archive 為 `2026-08-27-planning-artifact-manifest-binding`（archive 內的 tasks.md 是歷史紀錄，不改）；`openspec/specs/planning-artifact-manifest-binding/spec.md` 只轉指 docs spec，不需改。changelog 更正只動 #802 條目的兩句，其餘文字不動；#812 本身的條目另寫在新 fragment。

### D7 風險／測試矩陣

| Surface／風險 | Harness | Oracle |
|---|---|---|
| 他人 `-v2` 目的地 | `planning_kind_bound("spec"/"design"/"plan", feat-work-gc-v2…, "feat-work-gc")` | 全部 `False` |
| manifest glob 入口 | `_publish_planning_artifacts`，三種 combo 的 `allowed_refs`（`compile_combo` 產出） | `outside governed roots`、目的地不存在 |
| kind 跨界 | small-fix／feature-oneshot，kind=spec 寫 `<work_id>-design.md` | 被拒 |
| docs 路徑未知 kind | small-fix，`kind="notes"` 寫 `specs/<work_id>-spec.md` | `outside governed roots`（不再是 `unknown planning artifact kind`） |
| work_id 以 `-plan` 結尾 | `planning_kind_bound("plan", …, "demo-plan")` | `plans/demo-plan.md`、`plans/demo-plan-plan.md` 為 `True`；`plans/demo.md` 為 `False` |
| authority 重驗入口 | `SimpleNamespace` run（沿 802 測試 `test_fix_standard_authority_accepts_published_canonical_planning_triplet` 樣板）＋ evidence 列 `feat-work-gc-v2-spec.md` | `outside planner outputs` |
| 合法 change slug ≠ work_id | `openspec_refs=("demo-change",)`、目的地 `demo-change-{spec,design}.md`、`plans/demo-change.md` | publish 接受、重驗回傳三筆 authority；不帶 anchor 時 publish 被拒 |
| workstream anchor | `planning_authority` 含 `docs/superpowers/workstreams/demo-ws/todo.md` | `_planning_anchor_slugs` 含 `demo-ws`；他人 work_id／非五段／非法 slug 被濾掉 |
| canonical／日期／`-plan` | 直呼＋publish | 接受；全形數字日期被拒 |
| 守衛 | D5 輸入 | 各自 `False`；絕對／`..` 必拒 |
| 既有回歸 | `tests/test_workflow_production_wiring.py`、`tests/test_planning_publication_transaction_536.py`、`tests/test_claim_inflight_supersede_524.py`、`tests/test_diagnostic_invariant_family_527.py`、`tests/test_planning_artifact_manifest_binding_802.py` | 全綠；只有 802 glob 測試的 prefix／suffix／middle 翻成 `False` |

### D8 Sizing

只有 1 個 production 模組（`manager.py`）→ `domain_breadth=0`。改動本身是路徑判準與參數串接，沒有新增 durable 欄位，也不改 publication transaction 或 CAS；但這個判準決定哪些 ref 會進入持久化的 `run.planning_authority`，也必須維持已持久化 authority 在 resume 時的相容（D4）→ `state_consistency=1`。三件齊全時機械三維固定 4，總分 5／Yellow。
