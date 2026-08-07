---
status: draft
work_item: design-adhoc-oneshot-dispatch
---

# design-adhoc-oneshot-dispatch Design

## Decisions

- **D1（runtime 隔離）**：`run once` 不擴充 `PSC_INSTANCE`/`_installed_environment()`
  （`config/runtime.py:53-70`）這條「已安裝 instance」機制。它改為直接在
  同一行程內組裝 `JobRegistry(state_path=<ephemeral tmp path>)`
  （`coordinator/registry.py:217`，建構子本就接受任意路徑，與
  `PSC_COORDINATOR_ROOT` 無耦合）＋`Dispatcher`
  （`coordinator/dispatcher.py:96-107`，三個依賴皆經建構子注入）＋既有純
  函式 `manager.run_tick()`（`coordinator/manager.py:1951`，簽名只吃
  `dispatcher`/`metas`/一串 optional 注入點，無任何全域 root 讀取），
  完全繞過 `coordinator/cli.py` 的 control-queue 入口
  （`_submit_mutation_request()`：daemon 未就緒直接印
  `錯誤: manager daemon 未就緒` 並回傳非零，`fanout`/`tick`/`work`
  三個 CLI 子指令全部經此函式送 `ControlRequest` 給常駐 daemon 消費——
  這是「必須先 install service」的真正根因，不是表層 env var 問題）。
  ephemeral state 存於系統 tmp 目錄（不落在任何 `PSC_AGENTS_ROOT`
  底下），跑完依 `--keep-state` flag 決定保留或清除；worktree／git
  artifacts 一律保留供人工檢視，不隨 state 一起清。
- **D2（repo-root／worktree 邊界）**：目標 repo 透過既有
  `PSC_REPO_ROOT`（`config/paths.py:89-90`，預設 `Path.cwd()`）指定，沿用
  `_infer_repo_root()`（`coordinator/autonomy.py:217-235`，#288 已修正）
  推導 spec 所屬 repo 的既有邏輯，不新增第二套 repo-root 解析。**但**
  `Dispatcher.dispatch()` 一律呼叫 `worktree_creator.create()` 新建
  worktree／`feature/<slice_id>` branch——issue 原文範例 CLI 帶的
  `--branch <existing-branch>`（在呼叫方既有 branch/worktree 內工作）
  明確列為 v1 **非目標**：`ScriptWorktreeCreator.create()` 對已存在的
  worktree 目錄 fail-closed 拒絕是多處既有機制（#276 的 `redispatch()`
  同 worktree 續派原語、`poll_done` baseline 判斷）依賴的不變式，貿然放寬
  成「可指向呼叫方任意既有路徑」風險高、需要獨立設計，不與本票的 v1
  範圍捆綁（見 `tasks.md` T3）。`run once` 產出的 worktree／branch 對
  呼叫方而言是一次性、隔離的副產物，本身即符合「不進 work-item
  lifecycle」的訴求精神——不建議為了字面滿足「in-place」而放棄這層隔離。
- **D3（combo 骨架）**：不新增 combo schema。重用 #324 落地的
  `small-fix` combo（`deck/data/combos/small-fix.yaml`：`workflow-claim`／
  `brainstorming`／`writing-plans-light`／`subagent-build`／
  `verification`／`code-review`／`policy-commit` 七卡對應七 phase，兩條
  gate_spine）與 `combo_search_dirs()`（`deck/schema.py:79-101`）的
  instance-local override 通路。**明確拒絕**進一步砍卡：`small-fix` 已是
  `validate_manager_spine()`（#324 非目標段落明講不放寬）七 phase 涵蓋、
  persona 綁定、ship-前-reviewer 這三條治理憲法下的合法下限——issue 原文
  期望的「單一 `--prompt-file`、不必事先寫 design/plan 文件」的落差，
  設計上以「`run once` 把 `--prompt-file` 內容接成
  `brainstorming`／`writing-plans-light` 兩張卡的輸入 brief」解決，而非
  跳過這兩張卡本身；`default_workflow_manifest()`
  （`coordinator/work_bridge.py:170`）既有的 `combo_name` 參數已可直接
  傳 `"small-fix"`，`run once` 走同一個入口，不繞道另建 manifest 組裝
  邏輯。
- **D4（跨 repo persona 缺口）**：#338 描述的
  `persona-catalog-unreadable` 症狀已由 #341（commit `0264f3f`，`main`
  祖先，早於本次查證基準 `a2e8d0c`）解掉——`verification.py:780-838`
  現在對 `dispatch_base` tree 探測不到 repo-local override
  （`git cat-file -e`）時 fallback 讀取
  `persona.loader.DEFAULT_PERSONAS_PATH` 套件內建 catalog，evidence 記
  `source: "packaged"` 可稽核。`0264f3f` 落地時間（`2026-08-07
  09:47:25 +0800`）僅晚於 #338 建立時間（`2026-08-07 09:39:59 +0800`）約
  8 分鐘，判定 #338 目前是「症狀已消失、issue 未關閉」的過期狀態。
  `run once` **不需要**任何繞過或停用 persona gate 的設計；唯一殘留動作
  是驗證＋關閉 #338 本身，該動作已在外層任務清單（非本票）追蹤。
- **D5（builder identity 臨時放行）**：**要做，且重用既有機制**——
  `model_identities.load_model_identities()`
  （`coordinator/model_identities.py:240-268`）已經是「packaged registry
  ＋instance-local `<PSC_PROJECT_CONFIG_ROOT>/model-identities.yaml`
  合併」模型，與 #324 的 combo override 是同一設計語彙：instance-local
  新增身分視為疊加，與 packaged 同鍵但內容不同即 raise
  （`shadows packaged default`，fail-closed，不靜默覆蓋）。`run once`
  的新職責僅止於：接受 `--identity-overlay <path>`（或等價 inline flag），
  把它複製進**該次呼叫專屬**的 ephemeral `PSC_PROJECT_CONFIG_ROOT`
  （隨 D1 的 ephemeral state root 一起建立、一起清除），dispatch 前照常
  呼叫 `load_model_identities()`——不新增第二套驗證路徑，不修改
  `model_identities.py`。overlay 檔案 MUST NOT 寫入宿主
  `~/.agents`／使用者全域 project config；未提供 `--identity-overlay`
  時行為與現行完全一致（沿用 packaged registry，`gemini-3.6-flash-high`
  等未註冊身分依現行 `_require_registered_identity()` fail-closed）。
  這比「in-process 動態放寬 capabilities 判斷」風險低得多，且不需要
  `model_identities.py` 任何程式碼改動。
- **D6（與 #324／#338 邊界）**：#324 完全可重用（D1 的 combo override
  基礎設施、D5 的 identity override 基礎設施皆脫胎於此），不重複造輪；
  #338 現況已過期（D4），本票不因它而改變 persona gate 設計；conflict_files
  中列出的 `deck/data/combos/feature-oneshot.yaml`／`model_identities.py`
  在本設計下皆**不需修改**——`run once` 只新增自己的 CLI 入口與 wiring，
  不動這兩個既有檔案，衝突面比原簡報預期的小。

詳細 D1–D6 全文論證、風險緩解與可證偽 Requirements 見
`docs/superpowers/specs/adhoc-oneshot-dispatch-design.md`
與 `docs/superpowers/specs/adhoc-oneshot-dispatch-spec.md`。
