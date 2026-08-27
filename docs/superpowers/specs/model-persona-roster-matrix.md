---
status: accepted
work_item: model-persona-roster-matrix
---

# model-persona-roster-matrix Specification

#456（`#452` 子項）：定案候選 (executor, model_id, persona) 身分矩陣——launcher 硬約束已先於
benchmark 排除一批組合，評測預算（`#455` 的 N）只花在剩下的格子。**本票是定案文件，不改
registry 檔案、不改任何 `.py`**；實際登錄由 `#452` B（schema v2→v3）／`#453`（保守預設封套）
落地時依本文件 R3 執行。

## 背景

- persona 三段固定：`planner`／`builder`／`reviewer`（`paulsha_cortex/coordinator/workflow.py:23`
  `MODEL_CHAIN_PERSONAS`，`#205` 已凍結，claim 時整條鏈一次解析）。
- executor 五家族：`copilot`／`claude`／`codex`／`agy`／`cg`
  （`paulsha_cortex/coordinator/launcher.py:783-789` `_ARGV_BUILDERS`，唯一真相來源）。
- packaged registry（`paulsha_cortex/coordinator/data/model-identities.yaml`）現況**只有一個 agy 身分**：
  `agy` / `gemini-3.1-pro-high` / `google` / `capabilities: [planning, review]` /
  `live_probe: agy-plan-sandbox`。packaged roster 中沒有任何 agy 身分帶 `build` capability
  （build 僅 copilot／claude／codex 具備）。
- 至今**沒有任何真模型跑完整 patchmud deck 的實測樣本**（`#455` 現況），因此本矩陣沒有
  「已實測」格；全部非排除格皆為「待 benchmark」。
- 引用之行號以 main @ `ea76673`（v0.1.6）為準，行號可能隨後續 commit 漂移；引用時以
  符號名（函式／常數）為主錨點。

## Requirements／決策

### R1 packaged baseline 硬約束排除清單（4 格，先於 benchmark 定案，逐格附程式碼依據）

| # | 排除格 | 依據（建構期或 argv 層即不可達） |
|---|---|---|
| 1 | `copilot` × planner | `SubprocessLauncher.__init__`（`launcher.py:815-816`）：`(read_only or review_only) and executor == "copilot"` → raise「copilot executor has no enforced read-only planning mode」；`build_copilot_argv`（`launcher.py:496-497`）同樣拒絕。無法提供唯讀保證的 executor 不得任 planner。 |
| 2 | `copilot` × reviewer | 同上——該檢查對 `review_only` 一併拒絕，`as_review_only()` 對 copilot 必在建構期 raise。注意：ship 階段的「copilot delivery review」gate（`workflow.py:566-568` `delivery_reviews = {"copilot", "maintainer-review"}`、`delivery.py` 的 copilot loop）是 **PR 層的 current-HEAD delivery review**，不是 reviewer persona 的 launcher 巷道，兩者不可混淆；本格排除的是後者。 |
| 3 | `agy` × builder（packaged fallback） | packaged roster 未宣告 `build` capability，因此 packaged fallback 不會把 agy 選為 builder。#799 另提供 host overlay 明示 `build` 時的 `accept-edits` launcher；該 overlay opt-in 不改 packaged baseline，也不把未驗證的 builder capability 寫入 roster。 |
| 4 | `cg` × builder | cg 是 zero-tool（`build_cg_argv` docstring，`launcher.py:703-754`：wrapper 自帶 `--available-tools=__none__`＋`--disable-builtin-mcps`＋throwaway HOME，不能跑 tool／寫檔／commit）。建構期三重 fail-closed：`allow_unsafe` raise（`launcher.py:735-736`、`813-814`）、`commit_required` raise（`launcher.py:737-738`）、builder 語境（`read_only`／`review_only` 皆 False）raise（`launcher.py:739-740`、`820-821`）。`#442` 已確認「補 cg builder」這條路走不通。 |

### R2 (executor, persona) 候選矩陣定案（5×3 = 15 格）

| executor | planner | builder | reviewer |
|---|---|---|---|
| `copilot` | **硬約束排除**（R1-1） | 待 benchmark | **硬約束排除**（R1-2） |
| `claude` | 待 benchmark | 待 benchmark | 待 benchmark |
| `codex` | 待 benchmark | 待 benchmark | 待 benchmark |
| `agy` | 待 benchmark | **packaged fallback 不可達；host overlay 明示 `build` 後由 #799 launcher 可達** | 待 benchmark |
| `cg` | 待 benchmark¹ | **硬約束排除**（R1-4） | 待 benchmark |

已實測（patchmud）：**0 格**（`#455` 現況：無任何真模型完整 deck 樣本）。生產 dogfood 實跑紀錄
（copilot/gpt-5.4 builder、codex/gpt-5.3-codex-spark builder、claude/sonnet ForeignReview、
agy planner——見 R3 出處欄）屬營運經驗，**不折抵** benchmark：`#452` 的封套欄位只認
patchmud 實測或明示預設兩種 provenance。

¹ `cg` × planner 的可達性限制：`PLANNER_PRIORITY`（`model_identities.py:25-29`）只列
`agy`／`claude`／`codex` 三對 (executor, domain)，secondary planner 自動 fallback
（`select_secondary_planner`，`model_identities.py:394-421`）**永遠不會選到 cg**；cg planner
僅經 `#205` run-scoped override 巷道可達。本票定案：**不**在本波擴充 `PLANNER_PRIORITY`
（屬 `#452` C 解析實作票的範圍），但該格仍列待 benchmark——override 巷道是真實巷道，且
benchmark 結果是日後決定是否擴充優先序的依據。

> **`#534` 後續更新（2026-08）**：`select_secondary_planner` 已改走三層解析鏈
> （`model_resolution.rank_candidates`），不再迭代 `PLANNER_PRIORITY`——該常數僅
> 保留為 executor↔domain 對應的歷史記錄，不參與任何選擇。因此本註記描述的
> 「cg planner 永遠不可達」限制**已解除**：cg 只要列在 host overlay，或評估
> 合格並人工複核進 `model-eval-roster.yaml`，即可被自動 fallback 選到。

補充註記（非排除，實作票需知）：reviewer persona 的結構化終局契約目前只有 claude 有
`--json-schema` 綁定（`launcher.py:993-994` 只對 claude 傳 `review_terminal_kind`，
`_claude_review_json_schema`）；codex／agy／cg reviewer 依賴 prompt 側終局契約＋harvest
驗證。benchmark reviewer 維度時此差異屬觀測條件，須記入 run 條件。

### R3 定案登錄 roster：每 executor 的 model_id 清單（僅收 repo 內有據可查的字串）

`#452` B 落地時，packaged registry SHALL 登錄以下 5 個身分（複合鍵 `(executor, model_id)`，
與 `IdentityRegistry` 去重鍵一致）。`capabilities` 填「R2 矩陣中該 executor 的非排除格」——
這是**候選宣告**（benchmark 前），封套值由 `#453` 預設／`#455` 實測補：

| executor | model_id | independence_domain | capabilities | model_id 出處（repo 內） |
|---|---|---|---|---|
| `agy` | `gemini-3.1-pro-high` | `google` | `[planning, review]` ＋ `live_probe: agy-plan-sandbox` | `model_identities.py:20` `AGY_MODEL_ID`；packaged registry 既有身分（#799 的 `build` 僅能由 host overlay 明示，不改 packaged roster） |
| `copilot` | `gpt-5.4` | `openai` | `[build]` | `docs/superpowers/plans/2026-07-21-v0.1.0-release-plan.md:12`（builder：copilot CLI / gpt-5.4）；`docs/superpowers/workstreams/add-cortex-version-flag/todo.md:11` 等多處派工紀錄 |
| `claude` | `sonnet` | `anthropic` | `[planning, build, review]` | `docs/superpowers/plans/2026-07-21-v0.1.0-release-plan.md:12`（ForeignReview：claude / sonnet）；claude CLI `--model` 接受 `sonnet` 別名（`build_claude_argv` `--model` 原樣透傳，`launcher.py:600-601`）。完整版本 pin **待確認**（見 R4） |
| `codex` | `gpt-5.3-codex-spark` | `openai` | `[planning, build, review]` | `docs/superpowers/workstreams/fix-mutation-request-timeout/todo.md:11`、`terminal-result-contract/todo.md:11` 等多處派工紀錄 |
| `cg` | `glm-5.2` | `zhipu` | `[planning, review]` | `launcher.py:698` `_CG_DEFAULT_MODEL = "glm-5.2"`；operator env file `COPILOT_MODEL=glm-5.2`（`launcher.py:694-697` 註解） |

落地約束：

- **cg 的 model 顯式化**：`launcher.py:694-697` 明載 cg 實際身分由 operator env file 決定、
  `_CG_DEFAULT_MODEL` 只是 argv 未帶 `model` 時的落地預設。registry 宣告與實際身分不得脫鉤：
  依 registry 身分派工 cg 時 MUST 顯式帶 `--model glm-5.2`（`build_cg_argv` 已支援 `model`
  參數），不得依賴 env 預設隱含。
- **agy 的 CLI token 解析**：agy `--model` 必須用 `agy models` 認得的字面值，registry 比對走
  `_resolve_agy_cli_token`（`model_identities.py:314-328`）的正規化容錯；新增 agy 身分時
  model_id MUST 以 `agy models` 實際輸出為準（R4 的 gemini-3.6-flash-high 卡在這一步）。
- **agy planning 綁定**：任何 `agy` 身分若帶 `planning` capability，MUST 為
  `independence_domain: google` ＋ `live_probe: agy-plan-sandbox`
  （`model_identities.py:147-151` fail-closed）。
- **agy builder opt-in（#799）**：`build_agy_argv` 的 `accept-edits` 形狀只適用於
  provisioned worktree；registry 的 `build` capability MUST 來自明示的 host overlay。
  這不會提升 packaged fallback 的 capability，也不代表已完成 benchmark。

### R4 待確認 model_id（不登錄、不計入 N；查不到依據的一律不發明）

| 候選 | 出處 | 卡點 |
|---|---|---|
| `agy` / `gemini-3.6-flash-high`（review 候選） | `docs/superpowers/workstreams/cost-governance-cluster/todo.md:135`（三身分表，與 packaged registry 矛盾、`#209` spec R6 已記錄未收斂）；`driving-cortex-skill/todo.md:12`（ForeignReview 計畫） | 需 `agy models` 實測確認 CLI token 在列才可登錄。**即使確認，也只登 `review` capability、不得登 `planning`**：`probe_agy_capability`（`model_identities.py:352,360`）寫死只驗 `AGY_MODEL_ID`，第二個 agy planning 身分會通過 registry 驗證卻拿不到真 live probe 覆蓋（假覆蓋）。確認後 reviewer 格 +1 → N+1。 |
| `codex` / `gpt-5.4-codex` | 僅 `docs/superpowers/plans/feat-slice-executor-model.md:13` 的測試 fixture 例示 | 非實跑紀錄，不足為據；確認實際可用後依 R2 codex 列（3 個非排除格）→ N+3。 |
| `claude` / `sonnet` 的完整版本 pin | `cost-governance-cluster/todo.md:135` 的 `claude-sonnet-4-6` 掛在 executor `agy` 之下（且該表與 packaged registry 矛盾、agy builder 已被 R1-3 排除），不可移植為 `claude` executor 的依據 | 先以別名 `sonnet` 登錄（CLI 接受），版本 pin 由 benchmark run 的 provenance 記錄實際解析到的模型版本，之後再決定是否改登 pinned id。 |

### R5 `independence_domain` 填法與 builder/reviewer 分離相容性

**填法定案：domain 依模型血統（vendor of the model），不依傳輸巷道。** 與
`PLANNER_PRIORITY` 既有映射（`agy`→`google`、`claude`→`anthropic`、`codex`→`openai`）一致：
`#205` 的 ship 前檢查（`workflow.py:575-584`：`builder_domains` 與 `reviewer_domains` 交集
非空即 raise）目的為 builder／reviewer 模型獨立性，決定相關性的是模型血統。因此：

- `copilot/gpt-5.4` → `openai`（executor 是 GitHub Copilot CLI，模型是 GPT 血統）。
- `cg/glm-5.2` → `zhipu`（傳輸走 copilot API／llm-share，模型是智譜 GLM 血統；新 domain 字串）。

相容性檢核（依 R3 roster）：

- packaged builders：`copilot`(openai)、`claude`(anthropic)、`codex`(openai)。
  `agy` 僅在 host overlay 明示 `build` 時加入 builder lane，不屬 packaged roster。
- reviewers：`claude`(anthropic)、`codex`(openai)、`agy`(google)、`cg`(zhipu)。
- 每個 builder 都存在至少一個異 domain reviewer → roster 與 ship 前檢查相容。
- **不可配對（同 domain，ship 必拒）**：`copilot` builder × `codex` reviewer、
  `codex` builder × `codex` reviewer、`claude` builder × `claude` reviewer。
  `#452` C 的解析器 MUST 在凍結模型鏈時避開這三種配對，而非留給 ship 前才炸。
- planner 側另有 `select_secondary_planner` 的異 domain 要求
  （`model_identities.py:409-410`：secondary 必須與 primary 異 domain）。

### R6 「registry 登錄」與「本機可用」分離表達（呼應 `#442` spawn probe 風險）

**機制決策：登錄不隱含可用；可用性一律由 runtime 訊號表達，不新建機制、沿用三個既有 seam。**

- **靜態層（registry）**＝身分存在宣告：versioned、machine-agnostic、packaged。某台機器沒裝
  對應 CLI，MUST NOT 成為從 packaged registry 移除身分的理由。
- **動態層（本機可用）**由既有 seam 表達，缺一不可、不得以 registry 有列來短路：
  1. `live_probe`／`CapabilityProbe`（`model_identities.py:274-384`）——
     `select_secondary_planner` 已示範正確模式：`planning` capability 在列只是入圍，
     `probe.ready` 且 probe 回報身分與 registry 宣告一致（`model_identities.py:411-419`）
     才可選。新 persona 的選擇器 MUST 沿用此模式。
  2. dispatch runtime preflight（`SubprocessLauncher.executor_environment`，
     `launcher.py:925` 起；`runtime_preflight.ExecutorEnvironment`）——與正式 job 同一份
     env 解析 interpreter／PATH，CLI 不在場在 dispatch 前暴露。
  3. `check_executor_auth`（`executor_auth.py:110`）＋cards 的 `provider:executor` 閘門
     （`manager.py:6183` 的 hold 註解，`#442` 剩餘實質項）——閘門啟用後，CLI／auth 不在場的
     機器上該候選在 admission 被擋下排隊或改派，而不是 spawn probe 事後失敗。
- 推論：`#442` 啟用 `provider:executor` 時**不需要**任何 registry 條目的增刪配合；本矩陣
  新增 4 個身分也**不以** `#442` 閘門啟用為前置。兩票解耦。
- `#452` B 的 `profile_provenance`（`patchmud`／`default`）屬靜態層（封套值的來源），與
  本機可用性正交，MUST NOT 被當成可用性訊號消費。

### R7 「待 benchmark」格數 N 定案（`#455` 消費）

以 R3 roster（每 executor 恰 1 個 model_id）在 (executor, model_id, persona) 粒度計數：

| persona | 待 benchmark 格 | 數 |
|---|---|---|
| planner | `claude/sonnet`、`codex/gpt-5.3-codex-spark`、`agy/gemini-3.1-pro-high`、`cg/glm-5.2` | 4 |
| builder | `copilot/gpt-5.4`、`claude/sonnet`、`codex/gpt-5.3-codex-spark` | 3 |
| reviewer | `claude/sonnet`、`codex/gpt-5.3-codex-spark`、`agy/gemini-3.1-pro-high`、`cg/glm-5.2` | 4 |

**N = 11**（packaged baseline：15 格 − 硬約束排除 4 格；已實測 0 格）。#799 的
host-overlay agy builder opt-in 不計入這份 packaged benchmark 基線。

分期註記：`pilot-v1` 現況只量得到 builder 維度（`#452` 邊界明載，題庫票
`hamanpaul/paulsha-patchmud#13` 未落地前 planner／reviewer 停在預設窗口），故**現階段可
實測 3 格**（builder 列），其餘 8 格待題庫落地。`#455` 的成本外推 SHOULD 以
「N=11 全量上界、3 格近期實測」兩個數字併陳。R4 待確認身分每確認一個，N 依其非排除格數
增加（gemini-3.6-flash-high：+1；gpt-5.4-codex：+3），兩票數字須同步更新。

### R8 fail-closed 驗證證據

R3 roster（5 身分、含 agy 追加 `review` capability）已於本票查證時以
`IdentityRegistry.from_rows` 實際載入驗證通過（無重複身分、agy planning 綁定滿足、欄位
白名單無違例）；並反向驗證兩條 fail-closed 路徑仍會攔截：agy planning 配非 google domain
→ raise、重複 `(executor, model_id)` → raise。`#452` B 落地 PR 的測試 MUST 覆蓋同等斷言
（roster 正向載入＋兩條負向），不得只信本票的一次性驗證。

## 非目標

- 不改 `model-identities.yaml`、不改任何 `.py`（含不補 R1-3 註記的 agy-builder 建構期 guard）。
- 不擴充 `PLANNER_PRIORITY`（cg planner 自動 fallback 可達性留給 `#452` C）。
- 不啟用 cards 的 `provider:executor` 閘門（`#442` 剩餘項，與本票解耦，見 R6）。
- 不定封套四欄位的值（`#453` 預設／`#454` 映射／`#455` 實測）。
- 不處置 `cost-governance-cluster/todo.md:135` 與 packaged registry 的矛盾（`#209` spec R6
  已記錄，owner 對齊超出本票）；本票僅在 R4 拒絕把該表當登錄依據。

## 驗收面

- R1 每格排除理由可追到具體程式碼約束（symbol＋行號雙錨點），拿掉任一依據該格即回到
  待 benchmark——用以確認排除是被證據支撐而非慣性沿用。
- R3 每個 model_id 字串在 repo 內 grep 可命中所列出處；R4 之外不存在任何發明的 model_id。
- R7 的 N=11 與 R2 矩陣格數自洽（15 − 4 − 0 = 11），且 `#455` 引用的 N 與本文件一致。
- `#452` B 落地時依 R8 補齊 roster 的正負向 fail-closed 測試。
