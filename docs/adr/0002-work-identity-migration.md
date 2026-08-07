# ADR-0002: `cortex work migrate` —— work identity 遷移原子動詞

- Status: Proposed（本 ADR 只定設計；不含實作，見「Rollout and rollback」）
- Date: 2026-08-07
- Decision owners: paulsha-cortex maintainers
- Related issue: `hamanpaul/paulsha-cortex#331`

## Context

### 現況：識別遷移沒有原子動詞

`cortex work` 的 action 白名單（`paulsha_cortex/coordinator/cli.py:186-190`，
`action` 參數本身宣告於 185 行）與
`execute_work_action` 的鏡像白名單（`paulsha_cortex/coordinator/work_actions.py:3600-3604`）
目前只有 `link, unlink, start, resume, retry-build, retry-verify, retry-review,
recover-planning, recover-pre-candidate, recover-repair-commit, abandon, auto,
ship, review-attest`——沒有任何「把一個 work_id 的來源整批遷到另一個 work_id」
的複合動詞。`_mutate_override`（`work_actions.py:357-397`）一次呼叫只能對單一
`(work_id, source)` pair 做 `link` 或 `unlink`，而且每次呼叫都各自
`_write_override`（`work_actions.py:287-314`）一次——即使兩個呼叫改的是同一個
`.cortex/work-items.yaml` 檔案裡的不同 row，也不會被合併成一次 atomic replace。

### 實測案例：W1 -v2 重識別的 5-PR 舞蹈

Issue 提出後，#326–#330（`feature/w1-v2-exclusions` →
`-exclusions-fix` → `-unlink-window` → `-tombstones` → `-full-window`）是本專案
自己在 2026-08-05～08-06 真的跑過一次識別遷移（`feat-work-gc` →
`feat-work-gc-v2`、`design-task-type-taxonomy` → `design-task-type-taxonomy-v2`）
的完整記錄。逐一核對 `.cortex/work-items.yaml` 的實際 diff：

| PR | commit | 對 `.cortex/work-items.yaml` 做的事 |
|---|---|---|
| #326 | `a8e1c6b`（merge `9a2ee0a`） | 舊 work_id 補 `excludes`（openspec + github_issue），斷開 inferred 重新合併 |
| #327 | `0c1f96e`（merge `9eee8e9`） | 舊 work_id 的 `excludes` 收窄——**移除 github_issue exclude**，只留 openspec |
| #328 | `02c77c8`（merge `9ef0589`） | 新 work_id（`-v2`）的 `links` 移除 `github_issue`，暫時放棄新識別對 issue 的確認 |
| #329 | `06f1784`（merge `96d95cf`） | 舊 work_id 新增 `links: [path -> 新建 todo.md 墓碑]`，讓舊識別重新有非空 confirmed source |
| #330 | `0eda4ff`（merge `4ee9a55`） | 舊 work_id 的 `excludes` **全部清空**；新 work_id（`-v2`）的 `openspec` link 也**暫時移除** |

5 個 PR 橫跨約 **8 小時 49 分鐘**（`9a2ee0a` 23:38:11 → `4ee9a55` 08:27:37，見
`git log --format=%ai 9a2ee0a 4ee9a55`），其中 #327→#328（`9eee8e9` 23:48:31 →
`9ef0589` 07:43:47）間隔近 **7 小時 55 分鐘**（單一最大間隔）。這不是巧合：`.cortex/work-items.yaml` 的合法寫入（`_write_override`
的 fsync + atomic replace）只保證**檔案**瞬間一致，但 `claim.py` 的碰撞檢查
（見下）讀的是 Monitor 週期性重建的 `work-items.snapshot.json`——`Monitor`
的 rescan 週期（`paulsha_cortex/monitor/config.py:53`
`rescan_interval_seconds: int = 300`、`config.py:55`
`github_refresh_interval_seconds: int = 300`）意味著每一步 override 變更都要
等下一次 Monitor 重掃才會反映到 `load_work_authority` 讀到的
`WorkAuthority`。操作者必須手動確認上一步已經「生效」（沒有 fail-closed 錯誤）
才敢送下一步，這正是 5 個 PR 之間出現數小時等待窗口、且需要逐步「先撤、再補、
再撤」拉鋸的根因——**不是流程沒設計好，而是這個系統本身把「mutation
authority」（override 檔）與「confirmed authority」（Monitor 快照）解耦成兩個
非同步時鐘**。

最終狀態（`main@9bda3c0` 現況，`.cortex/work-items.yaml:418-441`）：兩個舊
work_id（`feat-work-gc`、`design-task-type-taxonomy`）目前只剩一個 `path` link
指向 #329 建立的墓碑 `todo.md`；兩個新 work_id（`-v2`）則完全沒有
`github_issue`／`openspec` link，只剩 `path` link。也就是說：**墓碑至今仍留在
repo 上**（`docs/superpowers/workstreams/feat-work-gc/todo.md`、
`docs/superpowers/workstreams/design-task-type-taxonomy/todo.md` 兩檔仍存在，
`git log 4ee9a55..HEAD -- <這兩個路徑>` 無任何後續 commit）——#329 commit
message 承諾的「abandon 完成後由終態 PR 移除」尚未發生。這本身就是本 ADR 要
消滅的那類「手動記得清理」的殘留狀態。

### 碰撞檢查是刻意的單一 choke point，不應繞過

`claim.py:689-734`（`_load_work_authorities_with_diagnostics`）在整個 snapshot
的層級做兩個不變量檢查：

1. `(repo, work_id)` 身分不得重複（717-718）。
2. 同一 `(repo, issue)` 不得被兩個不同 `work_id` 同時 confirm（730-734），命中
   即 `raise ValueError("confirmed work authority missing or ambiguous")`。

第二條的註解明講：這是 #217／design #208 D「source-owner transfer」既有機制
的完整性守門——**任何 claim/ship/abandon 呼叫都經過這裡讀 authority**，是刻意
選擇的單一 choke point，「這正是這個 issue 必須永遠不讓 claim 觀察到的
中介狀態」。`work_bridge.py:288-373`（`_other_owner_ongoing_runs` +
`start_canonical_workflow` 的 368-373 行）在 claim-time 再加一層守門：新
work_id 若在啟動時發現舊 work_id 仍有 `ongoing` run 聲稱重疊 issue，直接
`raise RuntimeError("source-owner transfer incomplete: ...")`——不會自動接管。

`tests/test_claim_source_owner_atomic.py` 的 docstring 進一步證實：這個
不變量目前只有「snapshot 層級一次性檢查」的測試覆蓋，**沒有任何 fixture
涵蓋「合法遷移中雙 owner」的暫態**——貿然放寬會讓「兩個 work_id 同時聲稱同一
來源」這種資料損毀級 bug 變得可能。本 ADR 的第一個決策就是：**不放寬、不繞過
這個檢查**，改為讓 `.cortex/work-items.yaml` 的寫入本身收斂成一次 atomic
transaction，讓外部觀察者（含 Monitor 下一次 rescan）永遠不會看到「來源同時
屬於兩個 work_id」的中介狀態。

### `abandon` 的全等 CAS 是為什麼要墓碑

`_abandon_action`（`work_actions.py:2216-2225`）在非 `superseded` 分支要求：

```python
expected_issues = tuple(f"{authority.repo}#{n}" for n in authority.mapped_issues)
if run.issue_refs != expected_issues or run.openspec_refs != authority.mapped_openspec:
    raise RuntimeError("abandon WorkflowRun refs differ from current WorkAuthority")
```

`authority` 是 `execute_work_action`（`work_actions.py:3609-3616`）在 dispatch
**之前**、用當下 snapshot 對 `work_id` 呼叫 `load_work_authority` 拿到的物件。
問題在於：一旦 override 把來源從舊 work_id 挪走，`authority.mapped_issues`／
`mapped_openspec` 會變空，`expected_issues` 隨之變成 `()`，但 `run.issue_refs`
仍是 run 建立當下記錄的舊值——兩者恆不相等，`abandon` 永遠 CAS 失敗。W1 案例
的墓碑（`todo.md`，`work_actions.py` 沒有變動，是 repo 內容層的變通）只是給
`load_work_authority(old_work_id)` 一個非空 `mapped_todo_paths`／
`confirmed_todo` 讓它至少能被解析出來，並沒有解決「`mapped_issues` 已經被
`unlink` 清空」這個核心問題——**這正是 #330「abandon 尋址窗口放寬至全額認領」
必須把 issue／openspec link 暫時「還」給舊 work_id 的原因**：只有暫時全額
認領回舊 work_id，`expected_issues == run.issue_refs` 才能重新成立，abandon
才能通過 CAS，然後才能再撤掉。

本 ADR 的第二個決策（見下）直接消滅這個依賴：`migrate` 動詞在**修改 override
之前**就把舊 authority 完整讀進記憶體，之後的 abandon CAS 全部對這個「凍結副本」
比對，不再重新讀取 override 之後的（已清空的）authority。這樣一來，**墓碑
機制本身在新設計下不再必要**——見下方「Decision §3」。

### `depends_on: [325]` 查證結果

簡報標了 `depends_on: [325]` 但註明「若查證後 #325 與本票無直接技術耦合，
下一位實作者可調整」。已用 `gh issue view 325` 核對：#325 的範圍是「job record
補 token usage 欄位」（`registry.py` 的 job schema、`log_path` 對應各 executor
的用量 log 解析），與 work identity/override/authority 完全是不同子系統，
程式碼零重疊。**結論：此依賴應移除**，本 ADR 與 #331 的設計不因 #325 是否
落地而受阻；`depends_on` 的標注來源應是批次排程面的流程慣例（W4 設計票統一
排在 #325 之後），而非技術耦合，後續票務排程可自行決定順序。

## Decision

### 1. 新動詞介面

在 `p_work`（`cli.py:183-209`）的 action 白名單加入 `"migrate"`：

```python
p_work.add_argument(
    "action",
    choices=[
        "link", "unlink", "start", "resume", "retry-build", "retry-verify",
        "retry-review", "recover-planning", "recover-pre-candidate",
        "recover-repair-commit", "abandon", "auto", "ship", "review-attest",
        "migrate",
    ],
)
```

`work_id` 既有的單一 positional 沿用為 `old_work_id`（`migrate` 語意下）。
**不新增第二個 positional**——現有 14 個 action 共用同一個
`p_work.add_argument("work_id")`，若替 `migrate` 加第二個 positional 會破壞
其餘 action 的 argparse 形狀一致性，或需要另開一個 `migrate` 專屬 subparser
（增加整體 CLI 結構複雜度）。改為新增兩個選項：

```python
p_work.add_argument("--new-work-id", help="migrate 專用：目標 work_id")
p_work.add_argument(
    "--source", action="append", metavar="<kind>:<ref>",
    help="migrate 專用：要遷移的來源，可重複；kind ∈ github_issue|github_pr|openspec|path",
)
```

`--actor`／`--reason` 已存在（`abandon` 用過），`migrate` 直接複用同一組驗證
（bounded、printable、單行）。`--expected-run-id` 也複用既有 `abandon` 參數
——當舊 work_id 有 `ongoing` run 時必填（CAS），沒有 ongoing run 時必須不填
（避免 flag 語意混淆）。

CLI 呼叫範例：

```bash
cortex work migrate feat-work-gc --new-work-id feat-work-gc-v2 --repo owner/repo \
  --source openspec:2026-08-04-feat-work-gc \
  --source github_issue:owner/repo#178 \
  --expected-run-id workflow-0123456789abcdef0123 \
  --actor operator --reason 'v0.1.0 世代熔斷後續作重識別'
```

`--source kind:ref` 用單一 repeatable flag 而非簡報原提案的
`[--issue N ...] [--openspec <ref> ...]`，理由：W1 實測案例遷移的來源不只
`github_issue`／`openspec` 兩種——#329 墓碑本身就是 `path` kind 的 link。
`_canonical_source`（`work_actions.py:259-284`）已經是四種 kind
（`github_issue`／`github_pr`／`openspec`／`path`）共用同一個驗證函式，
`--source` 用 `kind:ref` 字串（`split(":", 1)`）餵給它，可以不重複四份
專屬 flag 就涵蓋全部 kind；`--issue N` 這種便利 sugar 可以留給後續實作者
視 CLI 易用性另外加（desugar 成 `--source github_issue:<repo>#N`），不影響
本設計的正確性。

`execute_work_action`（`work_actions.py:3600-3604`）白名單同步加入
`"migrate"`；dispatch 順序需要調整——現行流程對所有非
`link`／`unlink` action 一律先 `load_work_authority(work_id)`
（`work_actions.py:3609-3616`）才進入各自 handler。`migrate` 需要在這一步
之後，用同一個已載入的 `authority`（對應 `old_work_id`）繼續，**不得**為了
取得 `new_work_id` 的 authority 而再呼叫一次 `load_work_authority`
（理由見 §2 的凍結副本設計）。

### 2. 原子性設計：單一 override transaction + 凍結 CAS，不放寬碰撞檢查

**維持 `claim.py:717-734` 的碰撞檢查與 `work_bridge.py:368-373` 的
source-owner-transfer 守門完全不變**——不引入「遷移中合法雙 owner」的暫態。
理由已在 Context 說明：沒有測試 fixture 覆蓋、且是系統唯一 choke point，
貿然放寬的風險遠高於維持現狀的操作不便。

取而代之，`migrate` 用兩個機制把「現況 5 步」收斂成「1 個 CLI 呼叫（可能需要
operator 重送一次以跨過 Monitor rescan 邊界，但**不需要人工判斷中間狀態、
不需要手改 yaml、不需要墓碑）」：

**(a) 單一 atomic override transaction。** 新增
`_mutate_override_batch(*, repo, transactions)`，其中
`transactions: list[tuple[work_id, "link" | "exclude", source]]`。對同一個
`.cortex/work-items.yaml`，一次呼叫在記憶體中對**多個 work_id 的 row**同時
套用變更（舊 work_id 每個來源都從 `links` 移到 `excludes`；新 work_id 每個
來源都加進 `links`，並從 `excludes` 移除——舊 work_id 端不留 `excludes`，因為
它們已經不需要再排除，遷移後舊 work_id 直接沒有這些 link；新 work_id 若原本
就沒有這些來源的 `excludes` 殘留也一併清掉），驗證整個 payload
（複用 `_validate_override_payload`）後**只呼叫一次 `_write_override`**
——只有一次 `os.replace`，外部觀察者（含 Monitor 下一次 rescan）只會看到
「遷移前」或「遷移後」兩種狀態，不會看到 W1 案例裡那種
「舊排除了、新還沒補上」的中間態。這比現行 `_mutate_override` 每次只動
一個 `(work_id, source)` pair、需要多次呼叫才能完成一組遷移的設計更貼近
「atomic」的字面意義。

**(b) 舊 authority 與舊 run refs 在 override 寫入前凍結，不重新讀取。**
`execute_work_action` 在 dispatch 給 `_migrate_action` 之前已經
`load_work_authority(old_work_id)` 過一次（見上）；`_migrate_action` 把這個
`authority` 物件、以及（若有 `expected_run_id`）對應 run 的
`run.issue_refs`／`run.openspec_refs` **原樣**存進本地變數，**在整個函式
生命週期內都不重新讀取 override 之後的 authority**。因此：

- `_mutate_override_batch` 寫入之後，`_abandon_action` 原本要求的
  `run.issue_refs == expected_issues`（`expected_issues` 來自
  `authority.mapped_issues`）改成拿凍結副本的 `authority.mapped_issues`
  （寫入前的值，此時仍等於 `run.issue_refs`）去比對——**這條 CAS 檢查完全
  不需要改寫語意，只需要餵它凍結前的資料**，不必新增「migrate-aware 版全等
  檢查」放寬語意。這比簡報原本設想的「abandon 需要 migrate-aware 變體允許
  refs 從舊 authority 過渡到新 authority一次到位」更保守、更貼近既有程式碼
  ——不改變 `_abandon_action` 的判斷邏輯本身，只改變呼叫它時餵的 `authority`
  是凍結的還是重讀的。
- 舊 work_id 的墓碑機制**因此不再需要**：現行墓碑存在的唯一理由是讓
  `load_work_authority(old_work_id)` 在 override 已經清空來源後還能
  resolve 出一個非空 authority；既然 `migrate` 從頭到尾都用寫入前凍結的
  authority，就不會有「override 清空後還要重新 resolve 舊 work_id」這一步。

**(c) 新 work_id 的確認要跨過 Monitor 的非同步邊界，透過
`snapshot_hash` 判斷，不猜測、不 busy-loop。** `WorkAuthority.snapshot_hash`
（`claim.py:163`）就是整份快照的內容雜湊（`_load_snapshot` 用
`verification.canonical_json_hash(payload)` 算出、`claim.py:223`）。`migrate`
在 override 寫入**前**記下舊 authority 的 `snapshot_hash` 當
`pre_migration_snapshot_hash`；寫入 override 之後，若舊 work_id 有 `ongoing`
run 要 abandon，`migrate` 嘗試 `load_work_authority(new_work_id)`：

  - 若拋出 `ValueError`（尚未 confirm）**且**目前可讀到的
    snapshot（`_load_snapshot()` 的 digest，不需要新增 API，函式已是
    module-level）雜湊仍等於 `pre_migration_snapshot_hash`（代表 Monitor
    根本還沒重掃過），回傳一個新的 typed pending 結果
    `{"action": "awaiting-snapshot-refresh", ...}`（不是例外、不是
    `needs_human`——單純還沒到，operator 重送同一個 CLI 呼叫即可，沒有副
    作用因為 override 已經是目標終態、不會重複寫）。
  - 若 digest 已經前進但 `new_work_id` 仍然不能被 confirm（例如 GitHub
    closing-reference 與 override 打架、或 override 寫錯），這是一個**真正
    的問題**，直接 fail-closed 成 `needs_human:
    new-identity-not-confirmed-after-refresh`，附上實際觀察到的
    digest／`skipped` 診斷（複用 `_load_work_authorities_with_diagnostics`
    回傳的 `AuthorityValidationError`），不無限重試。
  - 若 `new_work_id` 已經 confirm，才進入 abandon 步驟（用 §2(b) 的凍結 CAS）。

這個判準比「等一個 timeout」更精確：它直接問「Monitor 是否已經看過我剛寫的
內容」，而不是猜多久夠久（W1 案例的真實間隔從 10 分鐘到 8 小時不等，寫死
timeout 無論多長都可能不夠或浪費）。

**(d) `.cortex/work-items.yaml` schema 不擴充。** 簡報問「是否需要擴充
schema 支援單筆『這個 source 這一刻起歸屬 new_work_id』宣告」——**不需要**。
現行 `links`／`excludes` 的表達力已經足夠描述遷移後的終態（新 work_id
`links` 裡有它、舊 work_id `links` 裡沒有它），問題從來不是 schema 表達力
不夠，而是**寫入次數**（多次分開寫 vs 一次原子寫）與**讀取時機**
（program 內凍結 vs 每次重新讀取可能已經變動的 override）。§2(a)(b) 已經
解決這兩點，不需要新的 override row 欄位。

### 3. 墓碑機制：本設計下不再需要，但需要一張收尾票清掉 W1 遺留的兩個

見 §2(b)：`migrate` 用凍結 authority，不依賴墓碑重新讓舊 work_id 可
`load_work_authority`。**因此本 ADR 不新增任何墓碑資料結構**——簡報問的
「墓碑的資料結構、存放位置、何時建立何時清除」在新設計下沒有對應物。

但 W1 案例遺留的兩個墓碑檔案（`docs/superpowers/workstreams/feat-work-gc/todo.md`、
`docs/superpowers/workstreams/design-task-type-taxonomy/todo.md`，
commit message 自己承諾「abandon 完成後由終態 PR 移除」但至今未移除）是
既存事實，不屬於本 ADR 的範圍（不改動任何非 `docs/**`／`openspec/**`／
`changelog.d/**` 檔案），但**必須**留一張後續 chore 票追蹤，避免這兩個
「已由 -v2 重識別接手」字樣的墓碑 Todo 永久變成噪音——與 `#344 work GC`
無關（見下段），需要獨立處理。

### 4. 與 `#344 work gc`（`paulsha_cortex/coordinator/gc.py`）的關係：完全正交

已逐行核對 `gc.py`（281-324 行 `scan()`）：它只讀 `git worktree list`／
`git branch`（`list_worktrees`、`list_local_branches`），用內容層驗證鏈
（`git merge-base --is-ancestor` → `git cherry`）判斷 worktree／local branch
是否已合併，回收範圍明確排除「remote branch、PR、delivery journal、
correlation」（`gc.py:1-13` 模組 docstring）。**`gc.py` 從頭到尾沒有 import
或讀取 `.cortex/work-items.yaml`、`WorkAuthority`、或任何 `docs/superpowers/
workstreams/**` 路徑**——它甚至不知道 work_id 這個概念存在，只認 git ref
與 worktree 路徑。

結論：**`migrate`／墓碑都不需要讓 `gc.py` 感知**，兩者操作的是完全不相交的
資料面（`gc.py` 只碰 git worktree pool 與 local branch；`migrate` 只碰
`.cortex/work-items.yaml` 與 workflow registry 的 run 狀態）。簡報提出的
「GC 回收邏輯是否要感知墓碑避免誤收」——查證結果是不需要，因為 GC 根本不會
掃到 `docs/superpowers/workstreams/**` 底下的檔案。

### 5. 與 source-owner transfer（#217、design #208 D）的關係：`migrate` 是完成
既有 claim-time 守門的上游動作，不重複、不衝突

`work_bridge.py:288-373`（`_other_owner_ongoing_runs` +
`start_canonical_workflow`）已經是「claim 新 work_id 時，若舊 work_id 仍有
`ongoing` run 聲稱重疊來源就 fail-closed」的既有機制——這條路徑完全不動，
`migrate` 不改寫 `work_bridge.py` 任何一行。

兩者的分工：`migrate` 負責把「override 裡的來源歸屬」與「舊 run 的生命週期」
一次性、原子性地收斂完成（等同於現況要跑 5 個 PR 才能達到的終態）；
`start_canonical_workflow` 的既有守門則保證**在 `migrate` 完成之前**，
`cortex work start <new_work_id>` 不會誤判可以啟動——這正是它原本設計要
擋的情境。`migrate` 執行完之後，`_other_owner_ongoing_runs` 對
`new_work_id` 查詢時會發現舊 work_id 已無 `ongoing` run（已被
`migrate` 的凍結 CAS abandon 掉），守門自然放行，**不需要修改
`work_bridge.py` 任何判斷邏輯**。

兩者對「同一個來源」的定義（`f"{repo}#{number}"` 字串）已經一致
（`work_bridge.py:303` 與 `work_actions.py` 的 `expected_issues` 建構方式
相同），不會有各說各話的風險。`migrate` 本身**不**呼叫
`start_canonical_workflow`——啟動新 work_id 的 workflow 是操作者後續另一個
明確的 `cortex work start new_work_id` 呼叫，職責邊界維持現狀（`migrate`
只管身分歸屬與舊 run 收尾，不管新 workflow 何時、用什麼 combo 啟動）。

## State machine

```
                    ┌─────────────────────────┐
                    │  cortex work migrate     │
                    │  <old> --new-work-id <new>│
                    └────────────┬─────────────┘
                                 ▼
                 ┌───────────────────────────────┐
                 │ 1. load_work_authority(old)     │  ← 既有 execute_work_action
                 │    （失敗：不變量錯誤，直接拋出，  │     dispatch 前流程，不動
                 │     零副作用）                    │
                 └────────────┬───────────────────┘
                              ▼
                 ┌───────────────────────────────┐
                 │ 2. 前置檢查（零副作用，全部失敗都  │
                 │    不寫任何檔案）：                │
                 │  - --source 對應 authority 現有   │
                 │    links 完整涵蓋                 │
                 │  - 若 authority 有 ongoing run：   │
                 │    --expected-run-id 必填且精確CAS │
                 │    且該 run 沒有 active Job／PR    │
                 │    ref／passed ship／             │
                 │    CompletionRecord（複用既有       │
                 │    abandon 的拒絕條件）             │
                 │  - 若沒有 ongoing run：             │
                 │    --expected-run-id 必須留空       │
                 └────────────┬───────────────────┘
                              ▼ 全部通過
                 ┌───────────────────────────────┐
                 │ 3. 凍結：authority、             │
                 │    pre_migration_snapshot_hash、 │
                 │    （若有 run）run.issue_refs／   │
                 │    run.openspec_refs 存進本地變數 │
                 └────────────┬───────────────────┘
                              ▼
                 ┌───────────────────────────────┐
                 │ 4. _mutate_override_batch：     │
                 │    單一 _write_override 呼叫，   │
                 │    舊 work_id 移除全部 --source   │
                 │    links；新 work_id 加入全部     │
                 │    --source links（若新 work_id  │
                 │    row 不存在則建立）              │
                 │    寫入 cortex-work-migrate-       │
                 │    intent/v1 evidence（CAS 建檔，  │
                 │    冪等：重送同參數不重寫）          │
                 └────────────┬───────────────────┘
                              ▼
                 ┌───────────────────────────────┐
                 │ 5. authority 沒有 ongoing run？   │──yes──▶ 寫入
                 └────────────┬───────────────────┘      cortex-work-migrate-
                              │no                          complete/v1，
                              ▼                            回傳 migrated，結束
                 ┌───────────────────────────────┐
                 │ 6. 嘗試 load_work_authority(new) │
                 └────────────┬───────────────────┘
                    raises          resolves
                       │               │
         digest未變 ───┤               ▼
                       │      ┌──────────────────────┐
                       ▼      │ 7. abandon：凍結 CAS   │
      回傳 awaiting-snapshot- │  （§2(b)），複用既有     │
      refresh（可安全重送，    │  _abandon_action 判斷   │
      無副作用）              │  邏輯本身                │
                              └───────────┬──────────┘
        digest已變仍 raises               ▼ 成功
                       │        寫入 cortex-work-migrate-
                       ▼        complete/v1，回傳 migrated
      needs_human:
      new-identity-not-
      confirmed-after-refresh
      （附診斷，人工介入）
                                          │ abandon CAS 失敗
                                          ▼（run 狀態被外部改變，如已進 delivery）
                              needs_human: migrate-abandon-cas-mismatch
                              （override 已達終態，僅舊 run 需人工收尾——
                               可用既有 `cortex work abandon` 手動處理，
                               或人工確認後忽略）
```

**失敗回滾點**：

- 第 1、2 步任何失敗：零副作用，直接拋錯，等同今天呼叫任何其他 action 失敗
  的行為，不留殘留狀態。
- 第 4 步 override 寫入失敗（fsync／replace 錯誤）：`_write_override` 本身
  是 temp-file + `os.replace` 的 all-or-nothing 寫入（既有實作，
  `work_actions.py:304-312`），失敗時 target 檔案維持寫入前內容，不會有
  半寫入的 yaml。
- 第 6-7 步的 `awaiting-snapshot-refresh`：**唯一預期需要 operator 重送同一
  指令的狀態**，且重送是全冪等的（intent evidence 已存在時 §4 的
  `_write_override` 會產生內容完全相同的 payload，`_write_override` 天生
  冪等於「相同內容→相同 byte-for-byte 檔案」，不會二次觸發任何額外變更）。
- 第 7 步 abandon CAS 失敗（run 在 migrate 呼叫之間被別的路徑推進，例如
  ship）：`needs_human`，但**不回滾 override**——sources 已經確定屬於
  new_work_id（這是我們想要的終態），只是舊 run 沒能自動收尾，操作者可用
  既有 `cortex work abandon`／或確認該 run 其實該保留（例如它其實已經合法
  進入 delivery，代表 migrate 的前置判斷跟 abandon 判斷之間發生了 race，
  這種情況下「舊 work_id 的 run 反而是真正該完成的那個」，需要人工判斷）。

## 驗收判準（供下一票對照）

引入 `migrate` 動詞後，W1 批次原本需要的 **5 個獨立 PR、跨 8 小時 49 分鐘**
的手動 yaml 拉鋸，應可壓縮為：

- 若舊 work_id 沒有 ongoing run（多數未來的重識別情境屬此類）：**1 次
  `cortex work migrate` CLI 呼叫**，零額外 PR、零人工判斷中間狀態。
- 若舊 work_id 有 ongoing run（W1 案例屬此類）：**同一個 `cortex work
  migrate` 呼叫送兩次**（第一次觸發 override 寫入並回報
  `awaiting-snapshot-refresh`；等 Monitor 下一次 rescan 後第二次呼叫完成
  abandon），且兩次呼叫之間**不需要**操作者手動編輯 yaml、不需要建立墓碑
  檔案、不需要判斷「現在能不能安全下一步」——這個判斷由 `snapshot_hash`
  比對自動完成。

可觀察陳述（供下一票驗收）：對 W1 案例的等價 fixture（舊 work_id 有 confirmed
issue+openspec+ongoing run，新 work_id 尚未 confirm），呼叫
`_migrate_action` 兩次後，斷言：

1. `.cortex/work-items.yaml`（fixture 版）只被 `_write_override` 呼叫
   **恰好一次**（不是 5 次、不是 2 次）。
2. 全程沒有任何新建 `docs/superpowers/workstreams/**/todo.md`（沒有墓碑）。
3. 舊 run 的終態是 `superseded`，`evidence/work-abandon/` 下有一筆合法
   `cortex-work-abandon/v1` record，且其 `authority_digest` 對應的是**凍結
   前**（遷移前）的 authority，不是遷移後的空 authority。
4. 新 work_id 之後呼叫 `load_work_authority` 能正確 resolve 出遷移過來的
   `mapped_issues`／`mapped_openspec`。

## Alternatives considered

### 放寬 `claim.py:717-734` 碰撞檢查，引入合法「遷移中」雙 owner 暫態

不採用。這條檢查是全系統唯一的 confirmed-authority choke point，
`tests/test_claim_source_owner_atomic.py` 目前只驗證「絕不允許雙 owner」，
沒有任何 fixture 涵蓋合法暫態；放寬後任何忘記收斂遷移的殘留狀態都會變成
「兩個 work_id 同時可以 claim/ship 同一個 issue」——這是資料損毀級後果，
且沒有回頭路（一旦某個 run 已經在雙 owner 暫態下被 dispatch，很難確定性地
判斷哪個才是「正確」的那個）。§2 的凍結-CAS 設計能在不碰這條不變量的前提下
達到同樣的使用者體驗（1-2 次呼叫），沒有理由承擔這個風險。

### `migrate` 直接呼叫 `start_canonical_workflow` 順便啟動新 workflow

不採用。啟動新 workflow 涉及 combo 選擇（`select_combo`）、可能的
`--combo` override、`needs_human_reason` 等一整組獨立語意，把它塞進
`migrate` 會讓一個動詞背兩種職責（身分歸屬遷移 + workflow 啟動），且測試
矩陣會相乘（每個 migrate 案例都要再乘上 combo 選擇的所有分支）。維持現狀：
`migrate` 完成後，操作者照舊呼叫 `cortex work start new_work_id`。

### 用 timeout/backoff 等待 Monitor rescan，而非比對 `snapshot_hash`

不採用。W1 實測的 PR 間隔從 10 分鐘到近 9 小時不等，任何寫死的 timeout
不是太短（誤判成永久失敗）就是太長（明明已經 refresh 了還在空等）。
`snapshot_hash` 是既有欄位（`claim.py:163`），比對它就能精確回答「Monitor
是否已經看過我剛才的寫入」，不需要猜時間。

### 擴充 `.cortex/work-items.yaml` schema，新增 `migrated_from`／`migrated_to` 欄位

不採用（見 Decision §2(d)）。現行 `links`／`excludes` 已經足夠表達遷移後的
終態；問題出在寫入次數與讀取時機，不是 schema 表達力，新增欄位只會讓
`_validate_override_payload`／`load_work_item_overrides` 兩處 schema 檢查
都要跟著擴充，卻不解決 5-PR 拉鋸的根本原因。

## Consequences

### Positive

- W1 那種橫跨 8 小時、5 個 PR、手動判斷「能不能下一步」的識別遷移，收斂成
  1-2 次 CLI 呼叫，且第二次呼叫的時機由系統自己判斷（`snapshot_hash`
  比對），操作者不需要盯著等。
- 墓碑機制整個消失（§3），減少一種需要「記得之後清掉」的殘留產物。
- 不碰 `claim.py` 的碰撞不變量與 `work_bridge.py` 的 source-owner-transfer
  守門，兩者的既有測試覆蓋（`test_claim_source_owner_atomic.py` 等）完全
  不需要重新審視安全性論證。

### Costs and risks

- `awaiting-snapshot-refresh` 仍然是一個「操作者要記得重送」的手動步驟
  （只是從「5 個 PR 手動判斷」降為「同一指令重送一次，系統告訴你什麼時候該
  重送」）；periodic runner 若要自動重送，需要額外一票評估是否該給
  `migrate` periodic recovery authority（目前設計刻意不給，比照
  `recover-repair-commit`／`retry-build` 等敏感 recovery action 的既有
  保守慣例：periodic runner 不取得這類 authority）。
- 第 7 步 `needs_human: migrate-abandon-cas-mismatch` 這個邊角案例
  （override 已達終態但舊 run 因 race 收尾失敗）需要操作者理解「來源已經
  遷移，但舊 run 還在」這個部分完成狀態，比現行 `abandon` 單一動作的
  all-or-nothing 語意稍微複雜；設計上已盡量把它限制在極窄的 race window
  （前置檢查在寫入前已經驗過 run 沒有 active Job／PR／passed ship／
  CompletionRecord，只有寫入後到 abandon 呼叫之間的極短時間才可能被
  別的路徑搶先推進）。
- W1 案例遺留的兩個墓碑檔案不在本 ADR 範圍內清除，需要獨立追蹤（見 §3）。

## Rollout and rollback

本票（design-doc）不落地任何程式碼，`git diff` 只涵蓋
`docs/**`／`changelog.d/**`／`CHANGELOG.md`。落地分期建議：

1. **Code PR A**：`_mutate_override_batch`、`_migrate_intent_record`／
   `_migrate_complete_record`（CAS evidence，仿 `_abandon_record`）、
   `_migrate_action`（無 ongoing run 的簡單路徑），純新增函式，不改既有
   action 行為。
2. **Code PR B**：`_migrate_action` 補上 ongoing-run 的 abandon 分支
   （§2(b) 凍結 CAS）與 `awaiting-snapshot-refresh` / `needs_human:
   new-identity-not-confirmed-after-refresh` 分支。
3. **Code PR C**：`cli.py` 白名單＋參數、`execute_work_action` 白名單
   （`migrate` 才真正對外可呼叫）。
4. **Chore PR D**：清掉 W1 案例遺留的兩個墓碑檔案（§3），此時可以真的用
   `cortex work migrate` 走一次（若 `feat-work-gc`／`design-task-type-
   taxonomy` 當下仍有殘留 authority）驗證新動詞，或單純直接刪除墓碑檔＋
   override row（因為實際 abandon 早已完成，墓碑本身已經沒有作用）。

任一階段可獨立 revert，因為每個 PR 都是純新增/白名單擴充，不修改既有
`link`／`unlink`／`abandon` 的既有行為與既有測試斷言。

## Verification

- `tests/test_work_actions_migrate.py`（新檔）：
  - `_mutate_override_batch` 對多 work_id、多 source 的單次 payload 變更
    只呼叫一次 `_write_override`（spy／monkeypatch 計數）。
  - 無 ongoing run 路徑：單次呼叫完成，`new_work_id` 之後可正確
    `load_work_authority`。
  - 有 ongoing run 路徑：第一次呼叫回傳 `awaiting-snapshot-refresh`；
    模擬 Monitor 重掃（更新 fixture snapshot 檔）後第二次呼叫完成 abandon，
    寫入 `cortex-work-migrate-complete/v1`。
  - 冪等：intent 已存在時重送第一階段不二次寫檔；complete 已存在時重送
    回傳 `already-migrated`，不二次 mutate run。
  - **回歸樁**：把 `_migrate_action` 內部改回「呼叫兩次
    `_mutate_override`（先 exclude 舊、再 link 新）」會讓「單次
    `_write_override` 呼叫計數」的斷言 fail——這是簡報要求的
    「換回目前 5 步手動流程會讓新測試 fail 的斷言點」。
  - abandon CAS 使用凍結 authority：override 寫入後（`mapped_issues` 已變
    空）abandon 仍能用凍結副本成功比對 `run.issue_refs`——若把凍結副本改回
    「呼叫時重新 `load_work_authority(old_work_id)`」會讓這個測試 fail
    （因為屆時 `mapped_issues` 已空，`expected_issues` 變 `()`，
    CAS 恆不相等）。
  - 前置檢查複用 abandon 既有拒絕條件（active Job／PR ref／passed ship／
    CompletionRecord 存在時 migrate 直接拒絕，零副作用）。
- CLI 白名單測試（併入既有 `tests/test_coordinator_cli_flags.py` 或同類
  檔案）：`migrate` 出現在 `choices`；`--new-work-id`／`--source` 缺漏時
  argparse 報錯；`--expected-run-id` 與「是否有 ongoing run」的必填/禁填
  邏輯有覆蓋。
- 本票（design-doc）自我檢查：`grep -rn "def test_migrate\|_migrate_action"
  tests/ paulsha_cortex/` 應為 0 命中，確認文件未誤植 code。
