---
status: draft
work_item: design-adhoc-oneshot-dispatch
---

# adhoc-oneshot-dispatch Specification

#279：想在不 `cortex install service` 的前提下，對任意 repo 派幾個一次性
小工，且不進入 work-item lifecycle。本 spec 定案 `cortex run once` 入口的
契約，作為後續拆分 code 票的 Requirements 依據。**本票不實作任何
Requirement，僅定案契約供未來實作票對照驗收。**

## 背景

2026-07-31 對外部 repo（serialwrap）ad-hoc 派 10 個小工的實測撞到四個
結構性阻礙，2026-08-01 官方 comment 標記「阻礙 2 前半已由 #288 解掉」。
本次以 main @ a2e8d0c 重新查證（皆已在 main 上核實，見對應 R 條的
檔案:行號）：

- `coordinator/cli.py` 的 `fanout`（144 行起）／`tick`（226 行起）／
  `work`（191 行起）皆經 `_submit_mutation_request()`（`cli.py:578-609`）
  送 `ControlRequest` 給常駐 manager daemon 消費，daemon 未就緒直接
  `return 1`——結构性依賴已安裝的 daemon（見 R1）。
- `coordinator/registry.py:217` 的 `JobRegistry.__init__` 接受任意
  `state_path`，`coordinator/manager.py:1951` 的 `run_tick()` 是不讀
  任何全域 root 的純函式——組裝這兩者可繞開 control queue（見 R1）。
- `config/paths.py:89-90` 的 `repo_root()`／`coordinator/autonomy.py:217-235`
  的 `_infer_repo_root()`（#288 已修正）已可正確解析跨 repo spec，但
  `Dispatcher.dispatch()` 一律新建 worktree／branch，無「呼叫方既有
  branch/worktree 內工作」路徑（見 R2）。
- `deck/data/combos/small-fix.yaml`（#324 已落地）是七 phase 各恰一卡的
  合法治理下限，`writing-plans-light`／`brainstorming` 卡仍要求
  planning 輸入（見 R3）。
- `coordinator/verification.py:780-838` 的 persona catalog 讀取已有
  packaged fallback（#341，commit `0264f3f`，已是 main 祖先），跨 repo
  persona gate 缺口已解（見 R4）。
- `coordinator/model_identities.py:240-268` 的 `load_model_identities()`
  已支援 packaged + instance-local 合併、shadow 衝突 fail-closed（見 R5）。

## Goals

- `cortex run once` 能對任意 repo 派一次性小工，不需要 `cortex install
  service`，job 狀態與宿主 `~/.agents` 完全隔離。
- 沿用既有 repo-root 解析、combo、persona catalog、identity registry
  機制，不重複造輪；明確劃出哪些既有不變式（worktree 建立、七 phase
  spine）v1 刻意不放寬。
- 未使用 `run once` 的既有入口（`fanout`／`tick`／`work`／已安裝
  instance）行為位元不變。

## Requirements

### R1 ad-hoc 派工 MUST 繞過 control queue，job 狀態 MUST 與宿主 runtime 物理隔離（對應 D1）

`cortex run once` SHALL 不透過 `coordinator/cli.py` 既有的
`_submit_mutation_request()`／control-queue 機制派工，SHALL 直接在呼叫
行程內組裝 `JobRegistry(state_path=<ephemeral 路徑>)`／`Dispatcher`／
呼叫既有純函式 `manager.run_tick()` 完成一輪 fanout→completion。ephemeral
`state_path` SHALL 落在系統 tmp 目錄，MUST NOT 落在任何
`PSC_AGENTS_ROOT`／已安裝 instance 的 root 之下。`config/runtime.py` 的
`PSC_INSTANCE`／`_installed_environment()` 機制 MUST NOT 因本功能而擴充
出「免安裝合成 instance」分支。

若不做：`run once` 若沿用 `fanout`/`tick` 現行入口，daemon 未安裝時
`_submit_mutation_request()` 必定回傳 `manager daemon 未就緒` 錯誤（現行
`cli.py:588-595` 行為），issue #279 訴求的「不需安裝 instance」完全無法
成立；若改成讓 `run once` 也寫進宿主共用 `PSC_COORDINATOR_ROOT`，則
issue 描述的「job 狀態記錄可能汙染現役 manager」風險原樣重現。

#### Scenario: 未安裝任何 instance 的環境執行 `run once`

- **WHEN** 呼叫端未曾執行過 `cortex install service`，`~/.agents/core/runtime/`
  下無任何 `-manager.env` bootstrap 檔
- **THEN** `run once` 仍可完成一輪派工到終局，不讀取、不要求該 bootstrap 檔存在

#### Scenario: 宿主已有現役 instance 常駐時執行 `run once`

- **WHEN** 宿主 `~/.agents/coordinator/jobs.json` 已有現役 manager daemon
  的真實 job 記錄
- **THEN** `run once` 產生的 job/slice 記錄寫入獨立的 ephemeral
  `state_path`，該檔案內容 MUST NOT 出現在宿主 `jobs.json` 中
- **THEN** 現役 daemon 的既有狀態不受任何欄位或筆數變化

### R2 目標 repo 解析 MUST 沿用既有機制；worktree／branch 建立行為 MUST 不變（對應 D2）

`run once` 的 `--repo-root` SHALL 透過既有 `PSC_REPO_ROOT` 解析鏈
（`config/paths.py:89-90`）與 `_infer_repo_root()`
（`coordinator/autonomy.py:217-235`）生效，MUST NOT 新增第二套 repo-root
解析邏輯。`Dispatcher.dispatch()` 建立 worktree／`feature/<slice_id>`
branch 的行為 MUST 維持不變；「在呼叫方既有 branch/worktree 內工作」
（即不新建 worktree、直接對呼叫方指定的既有路徑派工）在本 spec 範圍內
MUST NOT 實作，屬明確 v1 非目標。

若不做（若反而放寬 `worktree_creator.create()` 允許指向任意既有路徑）：
會破壞 `redispatch()`（#276 D1）、`poll_done` baseline 判斷、`cortex work
gc` 隱含「worktree 由 coordinator 建立與回收」的既有不變式，屬未經獨立
設計評估的高風險變更。

#### Scenario: `--repo-root` 指向非 configured root 的外部 repo

- **WHEN** `run once --repo-root <外部 repo 路徑>` 且該路徑與 manager
  既有 configured root 不同
- **THEN** spec 解析出的 repo root 為該外部 repo 自身（沿 `.git` 向上找），
  不誤判為 manager 既有 configured root

#### Scenario: `run once` 呼叫不帶 in-place 選項

- **WHEN** `run once` 完成一次派工
- **THEN** 目標 repo 內產生一個新建的 `feature/<slice_id>` worktree／branch，
  呼叫方原本所在的 branch／worktree 內容不受任何 commit 影響

### R3 combo 骨架 MUST 重用 `small-fix`，MUST NOT 新增更輕量的 combo 或砍卡（對應 D3）

`run once` SHALL 透過 `default_workflow_manifest(combo_name="small-fix")`
（`coordinator/work_bridge.py:170`）既有入口消費 combo，MUST NOT 新增
違反 `validate_manager_spine()` 七 phase 涵蓋、persona 綁定、
ship-前-reviewer 三條既有治理約束的新 combo。`--prompt-file` 提供的內容
SHALL 作為 `brainstorming`／`writing-plans-light` 兩張卡的輸入 brief，
MUST NOT 被設計為跳過這兩張卡本身。

若不做（若新增砍掉 planning 卡的更輕量 combo）：直接違反 #324 issue 原文
自行標注、`validate_manager_spine()` 強制的不可放寬約束，且與 #324 落地
的治理模型產生兩套互相矛盾的「最小 combo」定義。

#### Scenario: `run once` 消費 `small-fix` combo

- **WHEN** `run once` 對某一次性任務建 WorkflowRun
- **THEN** manifest 的 `combo` 欄位為 `small-fix`
- **THEN** `validate_manager_spine()` 對該 manifest 通過（七 phase 涵蓋）

#### Scenario: `--prompt-file` 內容餵入 planning 卡

- **WHEN** `run once --prompt-file task.md` 執行到 `writing-plans-light` 卡
- **THEN** 該卡的輸入 brief 含 `task.md` 內容
- **THEN** 產出的 plan 檔案存在於 `docs/superpowers/plans/`，`gate_spine`
  對應檢查通過

### R4 跨 repo persona catalog 缺口 MUST NOT 需要額外繞過設計（對應 D4）

`run once` 對外部 repo 的派工 MUST 沿用現行 `verification.py:780-838`
的 packaged fallback（#341 已落地），MUST NOT 為 `run once` 新增停用或
繞過 persona-scope 檢查的旁路。

若不做（若誤判 #338 仍是阻礙而新增繞過邏輯）：會在已經 fail-closed 且
可稽核（`source` 欄位）的既有機制之外，另開一條未受同等治理的旁路，
擴大攻擊面且與現行機制邏輯重複。

#### Scenario: `run once` 對無 repo-local override 的外部 repo 派工

- **WHEN** 目標 repo 的 `dispatch_base` tree 內不存在
  `paulsha_cortex/persona/personas.yaml`
- **THEN** verification 的 `persona_catalog.source` 為 `"packaged"`
- **THEN** 該 slice 不因 `persona-catalog-unreadable` 被擋

### R5 builder identity 臨時放行 MUST 透過 ephemeral instance-local overlay，MUST NOT 修改 registry 驗證邏輯（對應 D5）

`run once` SHALL 支援 optional `--identity-overlay <path>`：提供時，
SHALL 把該檔案複製進 R1 所建 ephemeral `PSC_PROJECT_CONFIG_ROOT`，
dispatch 前呼叫既有 `load_model_identities()`（
`coordinator/model_identities.py:240-268`）取得合併後 registry。overlay
檔案 MUST 經過現行 `IdentityRegistry.from_rows()` schema 驗證，格式錯誤
或與 packaged 同鍵不同值（shadow）MUST fail-closed 拒絕整次 `run once`
呼叫，MUST NOT 靜默略過該筆身分或退回無 overlay 狀態。overlay 檔案
MUST NOT 寫入宿主 `~/.agents` 或使用者全域 `PSC_PROJECT_CONFIG_ROOT`。
未提供 `--identity-overlay` 時，`run once` 的 identity 驗證行為 MUST 與
現行 `fanout`／`tick` 完全一致（僅 packaged registry，未註冊身分
fail-closed）。

若不做（若改為在派工路徑內動態放寬 `capable()`／capability 判斷）：
會新增一條繞過既有 fail-closed schema 驗證的邏輯路徑，且與 #209
（`design-model-capability-envelope`）正在定案的 `capable()` 六項判準
產生設計面衝突——本 R5 選擇「多一個可疊加資料來源」而非「改判斷邏輯」，
與 #209 的 envelope 判準完全正交、互不影響。

#### Scenario: 帶 `--identity-overlay` 臨時放行未註冊身分

- **WHEN** `run once --identity-overlay overlay.yaml` 且該檔宣告
  packaged registry 沒有的 `(executor, model_id)` 對
- **THEN** 該次呼叫的派工使用該身分，registry 驗證通過
- **THEN** 呼叫結束後，宿主 `~/.agents` 下任何 `model-identities.yaml`
  內容不含該筆身分

#### Scenario: overlay 與 packaged 同鍵不同值

- **WHEN** overlay 檔宣告的 `(executor, model_id)` 與 packaged registry
  相同，但 `capabilities` 或其他欄位不同
- **THEN** `run once` fail-closed 拒絕整次呼叫，錯誤訊息指出衝突鍵
- **THEN** 不啟動任何 model session、不建任何 job

#### Scenario: 未提供 overlay 時行為不變

- **WHEN** `run once` 呼叫未帶 `--identity-overlay`
- **THEN** identity 驗證行為與現行 `fanout`／`tick` 對未註冊身分的
  fail-closed 行為完全一致
