---
status: draft
work_item: design-adhoc-oneshot-dispatch
---

# adhoc-oneshot-dispatch Design

issue #279：想在不 `cortex install service` 的前提下，對任意 repo 派幾個
一次性小工（例如「幫我在另一個 repo 寫 10 個 case 檔」），且不進入
work-item lifecycle（不建 GitHub issue、不走 combo 全套 claim/ship）。本文
定案該入口（暫名 `cortex run once`）的架構決策，作為後續拆碼票的單一依據。

## 背景與現況查證（main @ a2e8d0c）

issue 原文列出四個結構性阻礙；2026-08-01 官方 comment 逐項核對後標記
「阻礙 2 前半已由 #288 解掉」。本文以 main @ a2e8d0c 重新核對全部四項，
結論如下（詳細檔案:行號見各 Decision 段）：

| # | 阻礙 | 官方 comment 判定 | 本次重新核查 |
|---|---|---|---|
| 1 | manager 綁單一 repo | 仍在 | **性質更根本**：不是「缺一個 CLI flag」，而是 `fanout`/`tick`/`work` 三個入口结构性依賴常駐 daemon 消費 control queue（見 D1） |
| 2 | job 狀態疑似共用宿主 runtime | 前半已解、後半仍在 | 一致；後半（job 狀態隔離）就是 D1 要解的問題 |
| 3 | deck combo 生命週期綁定 | 仍在 | **部分已解**：#324 落地 `small-fix` combo + instance-local override，但仍不足以滿足「單一 prompt-file 免 planning 文件」（見 D3） |
| 4 | builder identity 門檻 | 仍在 | 一致；但發現可重用既有 instance-local identity override 機制（見 D5） |

另外，`depends_on` 列的 #338（persona catalog gate 對外部 repo 派工必炸）
经查已被 #341（commit `0264f3f`）解掉，且早於本次查證基準
（見 D4）——這是官方 comment 未涵蓋的新發現。

## Decisions

### D1 runtime 隔離：繞過 control queue，直接組裝既有純函式，不擴充 instance 安裝機制

**現況**：`coordinator/cli.py` 的 `fanout`（144 行起）／`tick`（226 行起）／
`work`（191 行起）三個子指令全部經 `_submit_mutation_request()`
（`cli.py:578-609`）運作：

```python
status = read_status_fn()
if isinstance(status, dict) and status.get("degraded"):
    reason = status.get("degraded_reason") or "unknown"
    print(f"錯誤: manager daemon 未就緒（{reason}）；無法處理 {req_type}，請先啟動 daemon。")
    return 1
req_id = submit_request_fn(req_type, dict(args), DEFAULT_REQUESTED_BY)
done = poll_done_fn(req_id, timeout_seconds, DEFAULT_REQUEST_POLL_INTERVAL_SECONDS)
```

`submit_request_fn`/`poll_done_fn` 預設走 `paulsha_cortex.control.client`，
把 request 寫進 control queue 檔案，靠一個**已經在跑**的 manager daemon
輪詢消費、寫回 done 檔。這代表：即使把所有 `PSC_*` root 都疊加成 ephemeral
路徑，只要沒有行程在讀那個 ephemeral control queue，`submit_request_fn`
送出的 request 永遠不會被消費，`poll_done_fn` 必然 timeout——這才是「必須
先 `cortex install service --instance`」的真正根因，比官方 comment 描述的
「job 狀態落在共用 runtime」更底層：不只是「寫錯地方」，而是「沒有讀者」。

**決策**：`run once` 不透過 control queue，也不嘗試讓
`resolve_runtime_root()`／`_installed_environment()`
（`config/runtime.py:53-70`，讀 `~/.agents/core/runtime/<instance>-manager.env`
bootstrap 檔）認得一個「immediate、免安裝」的合成 instance。改為直接在
呼叫 `run once` 的同一行程內，組裝三個既有元件：

1. `JobRegistry(state_path=<ephemeral tmp path>)`
   （`coordinator/registry.py:217`）——建構子本就接受任意 `state_path`，
   与 `PSC_COORDINATOR_ROOT`／`resolve_runtime_root()` 完全無耦合，測試
   套件本來就是這樣用它的。
2. `Dispatcher(registry, pane_sender, worktree_creator, git_runner)`
   （`coordinator/dispatcher.py:96-107`）——三個依賴皆經建構子注入，
   `run once` 傳入真正會動作的 `TmuxPaneSender`/`SubprocessLauncher`
   組合（沿用 `coordinator/cli.py:_resolve_launcher()` 現行的
   executor→launcher 映射，不新增第二套）與
   `ScriptWorktreeCreator`（不需要新實作）。
3. `manager.run_tick(dispatcher, metas=..., ...)`
   （`coordinator/manager.py:1951`）——純函式：吃 `dispatcher` 與
   `metas`（spec 解析後的 dict list）與一串 optional 注入點
   （`handoff_dir`／`review_executor`／`identity_registry`／`reaper` 等），
   跑完整 fanout→complete_tick→（可選）janitor 一輪，回傳
   `{dispatch_skipped, dispatched, completed, errors, reaped, needs_human,
   ...}`。**沒有任何全域 root 讀取**——所有狀態落點都經參數傳入。

`run once` 的實作只需要一個新的**輪詢外殼**：反覆呼叫
`run_tick()`（沿用 `manager_daemon.py:862` 起 `build_periodic_tick_runner`
既有的 backoff 節奏思路，但不需要真的常駐——單一 slice 跑到終局
`passed`/`needs_human`/`failed` 或 `--timeout` 就返回），不需要
`build_request_executor`（`manager_daemon.py:445`）那層 control-queue
adapter，也不需要 `run_loop`（`manager_daemon.py:1027`）那層 daemon
主迴圈（含 signal handler、lock file、systemd 整合，這些都是「常駐服務」
才需要的東西，一次性呼叫不需要）。

ephemeral state（`JobRegistry` 的 `state_path`）落在系統 tmp 目錄（例如
`tempfile.mkdtemp(prefix="cortex-run-once-")`），**不落在任何
`PSC_AGENTS_ROOT` 之下**，與宿主 `~/.agents` 樹完全物理隔離——不是「疊加
一層 namespace」，而是「壓根不共用同一棵目錄樹」。跑完依 `--keep-state`
決定是否保留 state 目錄供除錯；worktree／git branch 一律保留供人工檢視
（不是 state，是 git 側可觀察的產物，清掉反而破壞「可稽核」的訴求）。

**為什麼不做**：擴充 `PSC_INSTANCE`／`_installed_environment()` 支援一種
「免安裝、自動產生的 ephemeral instance」——考慮過但拒絕，理由：

- `_installed_environment()` 的角色是讀「已經 `cortex install service`
  裝過的」bootstrap env，語意就是「這是一個持久、已註冊的 instance」；
  硬塞一個「免安裝」分支進去，會讓這個函式承擔兩種互斥語意（持久 vs
  一次性），未來任何讀這個函式的程式碼都要多想一種分支，維護成本擴散到
  一個本來單純的函式。
- 直接組裝三個元件（`JobRegistry`／`Dispatcher`／`run_tick`）完全不需要
  碰 `config/runtime.py`——`conflict_files` 原本列的這個檔案，在本設計下
  **不需要修改**，衝突面比簡報預期小。

### D2 repo-root／worktree 邊界：沿用既有解析機制，worktree／branch 建立維持不變，「in-place 派工」列為 v1 非目標

**現況**：目標 repo 由 `PSC_REPO_ROOT`（`config/paths.py:89-90`，
`_resolve_root("PSC_REPO_ROOT", Path.cwd())`，未設時退回 cwd）指定；
`_infer_repo_root(spec_path)`（`coordinator/autonomy.py:217-235`，#288
已修正）在 spec 不在 configured root 內時，沿路徑向上找 `.git` 推導
spec 自身所屬 repo（排除 `~/.agents` 本身）。`run once` 沿用這條路徑：
呼叫時把 `PSC_REPO_ROOT` 設成 `--repo-root` 指定的目標 repo，spec 檔寫在
目標 repo 內某個 scratch specs-dir，`_infer_repo_root` 自然解析正確。

**決策**：`Dispatcher.dispatch()` 的 worktree／branch 建立行為**不變**——
一律呼叫 `worktree_creator.create(branch, base_sha=...)` 新建
`feature/<slice_id>` worktree（`worktree-isolation` 卡註解原文：
`"worktree 由 coordinator 派工時自建（feature/<slice_id>）"`，
`deck/data/cards.yaml:62-75`）。issue 原文範例 CLI 帶的
`--branch <existing-branch>`（「可指定在呼叫方的現有 branch/worktree 內
工作」）**明確列為 v1 非目標**，理由：

1. `ScriptWorktreeCreator.create()` 對已存在的 worktree 目錄
   fail-closed 拒絕（`"worktree target already exists"`）是既有機制依賴
   的不變式——#276（`builder-task-boundary-segmentation`）的 D1
   `redispatch()` 同 worktree 續派原語，正是因為要繞開這條限制才需要新
   增一個完全不同的方法，而不是放寬 `create()` 本身。放寬成「可指向呼叫方
   任意既有路徑」風險同一等級：`poll_done` 的 baseline 判斷、GC
   （`cortex work gc`）對 build worktree 的回收邏輯，都隱含假設「這個
   worktree 是 coordinator 自己建的、生命週期由 coordinator 管」。
2. `run once` 的訴求本質是「一次性、不進 work-item lifecycle」——建自己的
   隔離 worktree／branch 反而更貼合這個訴求（呼叫方的既有 branch/worktree
   不會被一次性小工的 commit 污染），字面滿足「in-place」不見得是更好的
   設計，只是 issue 原作者當下的直覺期望。

若未來真的需要「in-place 派工」，應獨立立案評估（見 `tasks.md` T3），不與
本票的 v1 範圍捆綁。

### D3 combo 骨架：重用 `small-fix`，不新增 combo schema，也不砍卡

**現況**：#324（`e7792f6` merge）落地 `deck/schema.py:79-101` 的
`combo_search_dirs()`／`resolve_combo_path()`／`iter_combo_files()`——
`$PSC_AGENTS_ROOT/config/combos/` instance-local 目錄優先於套件內建目錄，
同 id 覆蓋；並落地參考 combo `deck/data/combos/small-fix.yaml`：

```yaml
combo:
  id: small-fix
  task_type: small-fix
  cards:
    - ref: workflow-claim
    - ref: brainstorming
    - ref: writing-plans-light
    - ref: subagent-build
    - ref: verification
    - ref: code-review
    - ref: policy-commit
  gate_spine:
    - after: verification
      exists: ["reports/verify/*<task-slug>*.md"]
    - after: code-review
      exists: ["reports/review/*<task-slug>*.md"]
```

七卡對應七 phase（claim／discuss／plan／build／verify／review／ship）各
恰一張，兩條核心 gate_spine（無 `worktree-isolation`、無
`openspec-propose`／`openspec-archive`、無 `tdd-red`）。`writing-plans-light`
卡（`cards.yaml:52-58`）與 `writing-plans` 同 `skill_ref`，差異只在
`requires` 只吃 `docs/superpowers/specs/*<task-slug>*-design.md`，不依賴
`openspec/changes/<change>/proposal.md`，打斷了 #324 issue 原文描述的
requires DAG 斷鏈。`default_workflow_manifest()`
（`coordinator/work_bridge.py:170`）已接受 `combo_name` 參數，
`cortex work start --combo small-fix` 已可用。

**決策**：`run once` 直接重用 `small-fix`，**不新增 combo schema**，也
**不進一步砍卡**。理由：`validate_manager_spine()` 要求 combo 涵蓋全部
七個 phase、persona 綁死、ship 前必有 reviewer，這是 #324 issue 原文自己
标注「這層憲法不該改」的非目標（`不放寬 validate_manager_spine() 的七
phase 涵蓋、persona 綁定或 ship-前-reviewer 約束`）。`small-fix` 已經是
這條治理憲法下卡數最少的合法骨架——`run once` 若想進一步跳過
`brainstorming`／`writing-plans-light`，等於是在違反 #324 剛立下、issue
本身標注不可動的約束，不是本票該碰的範圍。

issue 原文期望的「單一 `--prompt-file`、不必事先寫 design/plan 文件」
因此**只能部分滿足**：`run once` 的設計回應是——把 `--prompt-file` 的
內容當作 `brainstorming`／`writing-plans-light` 兩張卡的輸入 brief（即
「這就是這次任務的 design 討論起點與 plan 內容」），讓這兩張卡的執行
（不論是否需要人工介入）消化掉這份 brief 產出 `docs/superpowers/plans/`
下的 plan 檔案，而不是試圈略過這兩個 phase 本身。具體的 prompt 模板／
skill 呼叫方式留給後續 code 票決定（見 `tasks.md` T1），本文件只定案
「不砍卡、用 brief 餵卡」這個架構立場。

### D4 跨 repo persona catalog 缺口：已由 #341 解掉，不需要繞過設計

**現況**：`coordinator/verification.py:780-838` 的 `catalog_probe`：

```python
catalog_probe = _run_git(
    ["-C", str(resolved_repo_root), "cat-file", "-e", f"{dispatch_base}:{PERSONA_CATALOG_PATH}"],
    git_runner,
)
if catalog_probe["status"] == "ok":
    # repo-local override：pin dispatch_base commit 讀取，壞損 fail-closed
    ...
else:
    # 未宣告 override：canonical 來源改為 cortex 套件內建 catalog
    from ..persona.loader import DEFAULT_PERSONAS_PATH
    packaged_path = DEFAULT_PERSONAS_PATH
    packaged_text = packaged_path.read_text(encoding="utf-8")
    ...
    details["persona_catalog"] = {..., "source": "packaged"}
```

這是 #341（commit `0264f3f`，`fix(verification): persona catalog 改以
套件內建為 canonical 來源`）落地的行為，修正 #295（primary）／#291
（duplicate）：verification 原本無條件 `git show
{dispatch_base}:paulsha_cortex/persona/personas.yaml` 到**目標 repo**，
該檔只存在於 paulsha-cortex 自身，任何非 cortex repo 必然
`persona-catalog-unreadable`。修正後：目標 repo 沒有宣告 repo-local
override 時，改讀套件內建 catalog，不再要求目標 repo git tree 長出這個
檔案。

`git merge-base --is-ancestor 0264f3f a2e8d0c` 核驗為真——`0264f3f` 是本次
查證基準 main 的祖先，已經落地。`gh issue view 338 --json createdAt`
回報建立時間 `2026-08-07T01:39:59Z`（= `09:39:59 +0800`），
`git log -1 --format=%ci 0264f3f` 回報 `2026-08-07 09:47:25 +0800`——
`0264f3f` 落地僅晚於 #338 建立約 8 分鐘。#338 描述的症狀（`git show`
到 `hamanpaul/embedebuguide` 時 `fatal: path
'paulsha_cortex/persona/personas.yaml' does not exist`）與 #295/#291
的根因完全相同，這條 fallback 分支落地後理應同步解掉。

**決策**：`run once` **不需要**任何繞過或停用 persona gate 的設計——`本
depends_on` 列出的 #338 阻礙已經消失。唯一動作是**驗證＋關閉 #338**，
建議操作者用一個已知外部 repo（無 repo-local override）跑一次 tick／
`run once` 的最小 dogfood 驗證 evidence 出現 `source: "packaged"`
後關閉該 issue；此動作已在外層任務清單的發版驗證步驟追蹤，不在本設計
文件票的交付範圍內。

### D5 builder identity 臨時放行：重用既有 instance-local identity override 機制

**現況**：`model_identities.load_model_identities()`
（`coordinator/model_identities.py:240-268`）：

```python
def load_model_identities(config_root=None, *, use_packaged_default=True):
    root = Path(config_root) if config_root is not None else paths.project_config_root()
    custom_path = root / "model-identities.yaml"
    if not use_packaged_default:
        return _load_model_identity_file(custom_path)
    packaged = _load_model_identity_file(_packaged_registry_path())
    if not custom_path.is_file():
        return packaged
    custom = _load_model_identity_file(custom_path)
    packaged_by_key = {(item.executor, item.model_id): item for item in packaged.identities}
    additions = []
    for identity in custom.identities:
        key = (identity.executor, identity.model_id)
        packaged_identity = packaged_by_key.get(key)
        if packaged_identity is not None and packaged_identity == identity:
            continue
        if packaged_identity is not None:
            raise ValueError(f"model-identities custom identity shadows packaged default: {key[0]}/{key[1]}")
        additions.append(identity)
    return IdentityRegistry(schema_version=MODEL_IDENTITY_SCHEMA_VERSION,
                             identities=tuple(additions) + packaged.identities)
```

`config_root` 預設 `paths.project_config_root()`
（`resolve_project_config_root()` → `PSC_PROJECT_CONFIG_ROOT` 解析鏈，
`config/runtime.py:137-147`），讀該目錄下 `model-identities.yaml` 作為
**新增身分的疊加來源**，與 packaged 同鍵但內容不同 → `raise`（fail-closed，
不靜默覆蓋 packaged）。這與 #324 的 combo instance-local override
（`combo_search_dirs()`）是同一設計語彙：packaged 是 canonical baseline，
instance-local 是可疊加的資源目錄，衝突 fail-closed。

packaged registry（`coordinator/data/model-identities.yaml`）現況只有一個
身分（`agy`/`gemini-3.1-pro-high`/`capabilities: [planning]`，見
`docs/superpowers/specs/design-model-capability-envelope-design.md`
「現況更正」段的核實），`gemini-3.6-flash-high` 不在其中——這正是 issue
原文描述的「臨時要當 builder 需改 global 身份設定」阻礙的直接證據。

**決策**：**要做，且完全重用既有機制**——`run once` 新增
`--identity-overlay <path>`（YAML 檔，schema 與
`model-identities.yaml` 相同）：dispatch 前，把該檔複製進 D1 建立的
ephemeral `PSC_PROJECT_CONFIG_ROOT`（隨 ephemeral state 一起建、一起依
`--keep-state` 決定去留），照常呼叫 `load_model_identities()`——**不修改
`model_identities.py` 一行程式碼**，不新增第二套驗證路徑，shadow 衝突
偵測、schema 驗證全部沿用既有邏輯。

明確邊界：

- overlay 只影響**這一次 `run once` 呼叫**——寫入的是 ephemeral 目錄，
  MUST NOT 觸碰宿主 `~/.agents` 或使用者全域 `PSC_PROJECT_CONFIG_ROOT`。
- 未提供 `--identity-overlay` 時行為與現行完全一致：packaged registry
  單一身分，未註冊的 `(executor, model_id)` 依現行
  `_require_registered_identity()`（`manager.py:265-271`）fail-closed，
  `run once` 不做任何隱性放行。
- overlay 檔案本身仍受 `IdentityRegistry.from_rows()` 既有的 schema 驗證
  （型別、重複值、`agy` canonical model_id 特判等），格式錯誤一樣
  fail-closed 拒載，不是「無條件信任呼叫方輸入」。

這比 issue 原文暗示的「per-invocation 動態放寬 capabilities 判斷」風險
低得多（不改判斷邏輯，只是多一個可疊加的資料來源），且與 D3 的 combo
override 手法完全對稱，維護心智負擔低。

### D6 與 #324／#338 的邊界收斂

`conflict_files`（簡報列出）原本預期 `autonomy.py`／`manager.py`／
`config/runtime.py`／`deck/data/combos/feature-oneshot.yaml`／
`model_identities.py` 五個檔案會被多票同時觸碰。本設計下：

- `config/runtime.py`：**不需修改**（D1 選擇不擴充 instance 安裝機制）。
- `deck/data/combos/feature-oneshot.yaml`：**不需修改**（D3 重用
  `small-fix`，不動 `feature-oneshot`）。
- `model_identities.py`：**不需修改**（D5 重用既有合併邏輯）。
- `autonomy.py`／`manager.py`：後續 code 票會**新增**函式／CLI 入口（
  `run once` 子指令、輪詢外殼），但不修改這兩個檔案既有函式的既有行為——
  `_infer_repo_root()`／`run_tick()` 簽名與語意皆不變，純粹是新呼叫端。

`#324` 完全可重用（D3／D5 兩個決策都是站在它落地的基礎設施上）；`#338`
現況已過期（D4），本設計不因它調整 persona gate 的任何行為。

## 風險與緩解

- **風險：ephemeral state 若沒清乾淨會在 tmp 目錄堆積殘留**。緩解：
  預設跑完即清（除非 `--keep-state`），且落點是系統 tmp 而非任何
  `PSC_*` root，即使清理失敗也不會汙染 `~/.agents`。
- **風險：identity overlay 若誤植到宿主 project config root，會讓臨時
  放行變成永久放行**。緩解：後續 code 票的驗收條件（`tasks.md` T2）
  MUST 包含「overlay 只寫入 ephemeral 目錄，宿主 `~/.agents` 不受影響」
  的迴歸測試，比照 `changelog.d/fix-test-production-state-leak.md`
  （#303）已有的隔離測試模式。
- **風險：`small-fix` combo 仍要求 `brainstorming`／`writing-plans-light`
  兩張互動性質的 planning 卡，`run once` 若不能把這兩張卡跑得夠快，
  「一次性小工」的體感會變成「還是要走一輪完整 planning」**。這是 D3
  刻意接受的取捨（治理憲法優先於字面體感），緩解方式是 D3 段落描述的
  「prompt-file 當 brief 餵卡」設計，把兩張卡的執行成本壓到最低，而非
  跳過它們。
