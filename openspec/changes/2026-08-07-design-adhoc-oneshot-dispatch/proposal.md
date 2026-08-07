---
status: draft
work_item: design-adhoc-oneshot-dispatch
---

## Goals

定案「跨 repo ad-hoc 一次性派工」（issue #279）的架構決策——`cortex run once`
這個新入口的 job 狀態要落在哪、要不要新開 combo schema、跨 repo persona
catalog 缺口是否仍需處理、builder identity 臨時放行的 fail-closed 邊界——
作為後續 code 票的單一設計依據。**本票只交付設計文件，不動
`paulsha_cortex/` 任何程式檔。**

## Why

2026-07-31 實際嘗試對外部 repo（serialwrap）以 ad-hoc 方式派 10 個小工時
撞到四個結構性阻礙（manager 綁單一 repo、job 狀態疑似共用宿主 runtime、
deck combo 生命週期綁定、builder identity 門檻），2026-08-01 官方 comment
逐項核對後標記「阻礙 2 的 repo-root 解析前半已由 #288 解掉，其餘三項半
仍在」。本次以 main @ a2e8d0c 重新查證，結論與官方 comment 大致一致，但
發現兩處官方 comment 未涵蓋、影響設計走向的新事實：

1. **阻礙 3（combo 生命週期）已有部分底座**：#324（combo 可擴充與可選，
   已於 e7792f6 merge）落地了 instance-local combo override
   （`paulsha_cortex/deck/schema.py:79-101` 的 `combo_search_dirs()`）與參考
   輕量 combo `small-fix`（`paulsha_cortex/deck/data/combos/small-fix.yaml`，
   7 卡 2 gate_spine，無 `worktree-isolation` 卡）。`run once` 不必重新設計
   combo 骨架，但**不能**進一步砍到「零 planning 卡」——`small-fix` 已是
   `validate_manager_spine()` 七 phase 涵蓋這條治理憲法下的合法下限（見
   `design.md` D3），issue 原文期望的「單一 prompt-file、無需事先寫
   design/plan 文件」只能部分滿足。
2. **阻礙 4（persona catalog gate 對外部 repo 必炸）已經解掉**：本票
   `depends_on` 列的 #338 描述的症狀（`persona-catalog-unreadable`），其
   根因與 #295/#291 完全相同，且已由 #341（commit `0264f3f`，早於本次查證
   基準 main @ a2e8d0c）修正——`verification.py:780-838` 現在對
   `dispatch_base` tree 探測不到 repo-local override 時會 fallback 讀取
   `paulsha_cortex.persona.loader.DEFAULT_PERSONAS_PATH` 套件內建 catalog。
   `0264f3f` 是 `a2e8d0c` 的祖先（`git merge-base --is-ancestor` 已核驗），
   而 #338 建立於 `2026-08-07T01:39:59Z`、`0264f3f` 落地於同日
   `09:47:25 +0800`（約晚 8 分鐘）——**#338 這張 GitHub issue 本身仍是
   OPEN，但它描述的 bug 在其建立當下幾乎同時就已被另一張票的 fix 解掉**，
   目前處於「症狀已消失、票未關閉」的過期狀態。`run once` 的設計不需要為
   跨 repo persona gate 另闢繞過路徑；本票在 `design.md` D4 記錄此發現，
   關閉 #338 本身留給外層任務清單既有的驗證/發版步驟處理，不在本票範圍。

真正未解的核心阻礙，經本次查證確認**性質比官方 comment 描述的更根本**：
`cortex fanout`/`tick`/`work` 在 CLI 層一律經 control queue 送
`ControlRequest` 給 manager daemon 消費（`coordinator/cli.py` 的
`_submit_mutation_request()`，找不到就緒的 daemon 直接印
`錯誤: manager daemon 未就緒`）——這些入口**结构性依賴一個已安裝、常駐
的 daemon**，不是靠疊加 `PSC_*` env var 就能繞過的表層問題。`run once`
要做到「不需安裝 instance」，必須繞過 control queue，直接在同一行程內
組裝 `JobRegistry`（`coordinator/registry.py:217` 建構子接受任意
`state_path`）／`Dispatcher`（`coordinator/dispatcher.py:96-107`）並呼叫
既有的純函式 `manager.run_tick()`（`coordinator/manager.py:1951`）——這條
組裝路徑本來就與 `PSC_INSTANCE`/`_installed_environment()`
（`config/runtime.py:53-70`）完全解耦，`run once` 不需要、也不應該去擴充
instance 安裝機制本身。

## What Changes（設計層級，非程式碼變更）

- 定案 D1：`run once` 的 job 狀態隔離模型——繞過 control queue，直接在同一
  行程內組裝 `JobRegistry(state_path=<ephemeral tmp path>)` 並呼叫
  `manager.run_tick()`，不嘗試擴充 `PSC_INSTANCE`/`_installed_environment()`
  這條「已安裝 instance」機制；`PSC_AGENTS_ROOT` 之下其餘 root
  （`PSC_COORDINATOR_ROOT` 等，`config/runtime.py:12-18`）維持現行
  `RUNTIME_ROOT_DEFAULTS` 語意不動。
- 定案 D2：repo-root／worktree 邊界——沿用既有
  `PSC_REPO_ROOT`（`config/paths.py:89-90`）＋`_infer_repo_root()`
  （`coordinator/autonomy.py:217-235`）機制指定目標 repo；`Dispatcher.dispatch()`
  仍一律新建 worktree／`feature/<slice_id>` branch，不重用呼叫方既有
  branch／worktree——issue 原文期望的「可指定在呼叫方現有 branch/worktree
  內工作」明確列為 v1 非目標（見 `design.md` D2 風險說明）。
- 定案 D3：combo 消費——重用 #324 的 `small-fix` combo 與 instance-local
  override 機制，不新增 combo schema；`run once` 的職責是把
  `--prompt-file` 內容接進 `brainstorming`／`writing-plans-light` 兩張
  planning 卡的輸入，而非跳過整個 plan phase。
- 定案 D4：跨 repo persona catalog 缺口——已由 #341 解掉（見上），
  `run once` 不需要繞過或停用 persona gate。
- 定案 D5：builder identity 臨時放行——重用
  `model_identities.load_model_identities()`（`coordinator/model_identities.py:240-268`）
  既有的「packaged + instance-local `model-identities.yaml` 合併、
  shadow 衝突 fail-closed」機制，`run once` 只需把 overlay 檔案寫進其
  ephemeral `PSC_PROJECT_CONFIG_ROOT`，不新增驗證邏輯。
- 定案 D6：與 #324／#338 的關係收斂——#324 完全可重用，不重複造輪；#338
  現況已過期（bug 已修、票未關）。
- 不實作 D1–D5 任何一項；code 落地拆為三張候選後續票（見 `tasks.md`）。

## Capabilities

### Modified Capabilities

- `trusted-dispatch-completion`：新增「ad-hoc oneshot dispatch 入口」的
  contract delta，詳見 `specs/trusted-dispatch-completion/spec.md` 的
  ADDED Requirements，與 `docs/superpowers/specs/
  adhoc-oneshot-dispatch-spec.md` 的完整 Requirements、`docs/superpowers/
  specs/adhoc-oneshot-dispatch-design.md` 的 Decisions。
