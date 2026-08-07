---
status: accepted
work_item: persona-enforce-required-check
---

# persona-scope enforcement（#135）

本文記錄 `persona-scope` 從 `shadow` 切到 `enforce` 之後的實際行為、違規訊息格式、
豁免用法，以及把 `persona-scope` 設為 main required status check 的設定步驟。

對應 spec / design / plan：

- `docs/superpowers/specs/persona-enforce-required-check-spec.md`（R1~R4）
- `docs/superpowers/specs/persona-enforce-required-check-design.md`（D1~D4）
- `docs/superpowers/plans/persona-enforce-required-check.md`

## Enforcement 模式

`paulsha_cortex/persona/personas.yaml` 頂層 `enforcement` 欄位決定
`python -m paulsha_cortex.persona.scope_ci`（`.github/workflows/persona-scope.yml`
的 job `persona-scope` 所執行者）的放行行為：

- `shadow`（原預設）：無論判定結果為何，恆 `exit 0`，僅印出 verdict JSON 供觀察。
- `enforce`（#135 起為 production 值）：偵測到 scope 違規時 `exit 1`（阻擋
  merge，前提是 `persona-scope` 已被設為 required status check，見下）；未違規
  或套用豁免時仍 `exit 0`。

`enforcement` 欄位讀取邏輯（`paulsha_cortex/persona/loader.py:load_enforcement`）
是 fail-safe 的：缺檔、壞 YAML、缺 key、非法值一律退回 `shadow`——最保守，永不
因設定損毀而誤翻 `enforce`。

`personas.yaml` 每個角色除 `write_paths`／`allowed_tools` 等 scope 欄位外，
可另宣告 `completion_obligations`（字串清單，預設空，非破壞性擴充）：這是
「派工前把完成義務講清楚」的事前宣告，會被 `render.render_contract_prompt`
注入 dispatch prompt（見 `paulsha_cortex/persona/render.py`），與
`PersonaGuardrail` 的 scope 判定各司其職——後者管「能不能做」，前者管「結束
前必須做完什麼」。目前僅 `builder` 宣告一條：完成前必須 `git add`＋
`git commit`，worktree 不乾淨不得回報完成；空清單角色（`manager`／
`planner`／`reviewer`）不受影響，render 出的 prompt 不新增此段。

## 切換前的零誤殺回放（R1 / D1）

`enforce` 上線前，先以近期已合併 PR 的實際檔案清單回放 persona scope 判定，
證明零誤殺：

```bash
python -m paulsha_cortex.persona.replay --limit 30 --ref main
```

回放邏輯（`paulsha_cortex/persona/replay.py`）：

1. 取 `main` 上最近 N 個 merge commit（`git log --merges --first-parent`）。
2. 對每個 merge，用 `parent1...parent2` 三點差異取得該次合併實際的檔案清單
   （等同該 PR 的 diff）。
3. 以 `builder` 角色（見下方「角色假設」）逐檔套用
   `PersonaGuardrail.evaluate_filesystem`——與 `paulsha_cortex/persona/scope_ci.py` enforce 模式下
   實際使用的判定邏輯完全相同，只是離線對歷史 diff 重放一次。
4. 印出結構化 JSON（`prs_scanned` / `files_scanned` / `false_positives` /
   逐 PR 明細），`false_positives == 0` 時 `exit 0`。

此回放納入 `tests/test_persona_scope_enforcement.py::HistoricalReplayTests`，
可重跑；日後修改 scope 定義（`personas.yaml` 的 `write_paths`）時，重跑同一
指令即可立即看出對歷史合併紀錄的影響面。

**角色假設**：回放固定假設歷史 PR 皆以 `builder` 角色合併，因為 coordinator
目前實際派工慣例即是如此（`paulsha_cortex/coordinator/manager.py` 對
`job.get("persona")` 缺省即 `"builder"`；CLI `--persona` 亦缺省 `builder`），
而 `builder` 的 `write_paths` 為 `["**"]`（不限範圍）。這反映「當前實際派工
慣例下 enforce 化不會誤傷既有合併路徑」，並非放寬或迴避檢查——判定邏輯本身
與 production 完全一致。若日後 `planner` / `reviewer` / `manager` 角色開始
直接產出獨立合併 PR，須擴充回放以涵蓋對應角色歸戶，而不是持續假設
`builder`。

**若回放出現誤殺**：只能修正 `personas.yaml` 的 scope 定義本身（或修正回放
的角色歸戶邏輯），絕不可放寬 `PersonaGuardrail` 的判定強度或擴大豁免範圍來
讓回放通過——那會讓「零誤殺」變成自我實現的空話（D2）。

## 違規訊息格式（R2 / D3）

`persona-scope.yml` 的 stdout 為單行 JSON verdict，違規時（`ok: false`）包含
可定位訊息：

```json
{
  "role": "planner",
  "changed_paths": ["paulsha_cortex/persona/scope_ci.py"],
  "violations": [
    {
      "path": "paulsha_cortex/persona/scope_ci.py",
      "rule_id": "filesystem-scope",
      "reason": "path paulsha_cortex/persona/scope_ci.py outside persona write scope"
    }
  ],
  "handoff_ok": true,
  "ok": false,
  "mode": "enforce",
  "manifest": "runtime/handoff/....json",
  "base": "origin/main",
  "head": "deadbeef",
  "exempted": false
}
```

三個定位要素：

- **persona**：`role` 欄位（哪個角色觸發）。
- **實際觸及路徑**：`violations[].path`（每一個越界檔案）。
- **違反的 scope 規則**：`violations[].rule_id` + `violations[].reason`
  （`filesystem-scope` = 越出 write_paths 或試圖寫出 worktree 邊界；
  `unknown-role` = 角色不在 catalog 中）。

## 豁免機制（R4 / D4）

套用 GitHub label `policy-exempt:persona-scope` 於 PR 上時：

- `persona-scope` job **不阻擋合併**（`exit 0`，即使存在違規）。
- verdict JSON 仍完整印出違規內容（`violations` 不清空、`ok` 仍如實反映
  `false`），並多一個 `"exempted": true` 欄位——**豁免不靜音**，供事後稽核
  豁免使用頻率、判斷 scope 契約是否需要調整。
- 依 repo 慣例，套用豁免的理由記錄在 PR 說明或 label 旁的 review comment 中
  （與其他 `policy-exempt:*` label 的用法一致），不由 `persona-scope` 本身
  驗證理由文字。

Label 傳遞機制：`.github/workflows/persona-scope.yml` 以
`PERSONA_SCOPE_PR_LABELS`（逗號分隔）環境變數，將
`github.event.pull_request.labels.*.name` 傳給 `paulsha_cortex/persona/scope_ci.py`；本機重放/除錯
時可自行設定同名環境變數模擬。

## 設為 main 的 required status check（R3，GitHub repo 設定）

這是 **repo 設定變更**，不是 code 變更；本 PR 不會用 `gh api` 直接改動，需
repo owner 手動或以下列步驟操作（可重複執行以稽核現況）：

1. GitHub 網頁：`Settings` → `Branches` → `main` 的 branch protection rule
   （若無則新增）。
2. 開啟 `Require status checks to pass before merging`。
3. 在 status check 清單勾選 `persona-scope`（即
   `.github/workflows/persona-scope.yml` 的 job 名稱；GitHub 需先有該
   workflow 至少跑過一次，check 名稱才會出現在選單中）。
4. 儲存規則。

或以 `gh` CLI 稽核／設定（由 repo owner 執行，非本 PR 範圍）：

```bash
# 稽核現況
gh api repos/hamanpaul/paulsha-cortex/branches/main/protection \
  --jq '.required_status_checks.contexts'

# 設定（需 admin 權限；範例，實際執行前請與現有 contexts 合併，勿覆蓋）
gh api -X PATCH repos/hamanpaul/paulsha-cortex/branches/main/protection/required_status_checks \
  -f strict=true \
  -f 'contexts[]=persona-scope'
```

## 相關檔案

- `paulsha_cortex/persona/personas.yaml`：`enforcement` 設定與各角色 write scope。
- `paulsha_cortex/persona/scope_ci.py`：CI entry point，讀 `enforcement` 決定放行行為。
- `paulsha_cortex/persona/gate.py`：verdict 判定邏輯（`evaluate_diff` / `build_verdict`）。
- `paulsha_cortex/persona/guardrail.py`：`PersonaGuardrail`，實際 path/tool scope 判定。
- `paulsha_cortex/persona/replay.py`：歷史回放工具（R1）。
- `.github/workflows/persona-scope.yml`：required status check 的來源 workflow。
- `tests/test_persona_scope_enforcement.py`：本次切換的測試涵蓋。
