# ADR-0001: 統一 Work Item read model 與 Manager single-writer workflow

- Status: Accepted
- Date: 2026-07-17
- Decision owners: paulsha-cortex maintainers
- Related issue: `hamanpaul/paulsha-cortex#14`

## Context

現有 Monitor 只投影 repo 內 workstream/stage 文件，Coordinator 則以 Job/Slice 為中心處理已派工工作。GitHub issue、superpowers spec/plan、active OpenSpec、PR、review/check 與 remote archive 尚未形成同一個可驗證 read model。結果是「有議題」、「有待辦」、「agent 正在跑」與「已可信完成」容易被相鄰訊號錯誤等同。

同時，Deck 已保存 `persona_binding`，但 workflow manifest 還不能保證每個 step 使用 card 指定 persona；缺少 planner 與異質 brainstorm gate。Delivery 雖已有 deterministic verification 與 foreign review 基礎，仍缺 GitHub current-HEAD Copilot、thread、preflight、remote archive 與 merge 後 closure 的完整 orchestrator。

本決策必須保留既有安全邊界：Monitor 是 read model、Manager 是唯一 writer；provider failure 不得變成 authoritative empty；模糊關聯不得授權 mutation；對 `paulsha-hippo` 維持零 runtime dependency。

## Decision

### 1. 使用四態 lifecycle，加正交 facets

公開 lifecycle 固定為 `topic → todo → on-going → done`，內部以 `ongoing` 表示第三態。`blocked`、`needs_human`、`degraded` 是 facet，phase 是 WorkflowRun 內部進度，兩者都不擴充 lifecycle enum。

Reducer 順序固定為：受影響 provider degraded 時 freeze prior projection；否則 active WorkflowRun → `ongoing`；strict closure → `done`；confirmed active Todo artifact → `todo`；其餘 open issue → `topic`。Issue reopen 或 OpenSpec re-activate 會從 done 回退，不使用 monotonic-only 狀態機。

### 2. Monitor 聚合 provider last-good，Manager 持有 mutation authority

Monitor 讀 authenticated `gh api`、repo artifacts、override 與 Manager registry，建立 `WorkItem`/`WorkSource` read model。Provider 每次成功才替換自己的 source snapshot；失敗保留 last-good 並標 degraded。Durable file 固定為 `$PSC_MONITOR_STATE_ROOT/work-items.snapshot.json`，default root 為 `$PSC_AGENTS_ROOT/monitor`，schema 固定 `work-items-snapshot/v1`。

Manager 是 WorkflowRun、WorkflowStep、claim、GitHub label/PR/review request/merge、CompletionRecord 的唯一 writer。CLI mutation 只送 control request；Monitor 不因 scan 結果直接建立 workflow。

### 3. 關聯 authority 明確化並 fail-closed

Confirmed association 只接受：repo `.cortex/work-items.yaml` override、markdown `work_item` frontmatter、GitHub closing reference、Manager workflow metadata。來源的 confirmed ownership 必須唯一；collision 令 provider degraded 並阻止 dispatch/merge。

Title、slug、branch、issue number token 等 heuristic 只可做 inferred display group。至少要兩個獨立訊號、沒有 competing candidate，且 `--explain` 完整揭露。`unlink` 寫入 exclusion，不允許之後被 heuristic 靜默合回。

### 4. Workflow 保存 persona binding 與 domain separation

新增 planner persona 與 versioned WorkflowRun/WorkflowStep。Deck compiler 把每張 card 的 `persona_binding` 寫入 workflow manifest；不得套用 global builder persona。預設 combo 為 `feature-oneshot`。

Completeness gate 在 accepted spec/design/plan 缺失、出現 TBD 或未決策項時要求異質 brainstorm：primary planner 產 question pack；secondary planner 只回 evidence，且 independence domain 不同；primary 整合與落檔。Secondary 預設選 `agy/google → claude/anthropic → codex/openai`，排除 primary domain；無可用異質 identity 時 fail-closed。

`agy` launcher 只使用 `--print --mode plan --sandbox`，禁止 unsafe bypass；model identity registry 預設登錄 `agy + Gemini 3.1 Pro (High) → google`，deployment 必須 live probe，不能只相信靜態版本字串。

### 5. Delivery 是 bounded、current-HEAD、remote-verified closure

Builder 使用 `feature/<issue>-<slug>` worktree；deterministic verification 與 foreign exact-HEAD review 沿用既有 Candidate/evidence gate。Brainstorm peer 與 foreign reviewer 是不同 gate，Copilot review也不能替代 foreign reviewer。

Ship 由 Manager 執行：官方 `openspec archive -y`、tasks/spec/docs/changelog 檢查、zh-TW PR metadata、快速 policy、`PSC_PREFLIGHT_CMD` CI-parity preflight、GitHub checks、current-HEAD Copilot review、resolved/outdated threads、final HEAD race check、`gh pr merge --merge`。每次 push 重請 Copilot；最多兩輪 fix/re-review，每 HEAD 等 15 分鐘；逾限轉 needs_human。

Merge 後 fetch default branch，確認 merge ancestry、issues closed、active OpenSpec 消失、remote archive 存在、Todo tasks 完成，再寫 CompletionRecord。只有全部成立才投影 done。

### 6. 公開 contract versioned，舊 ProjectState 暫時相容

CLI JSON schema URI 固定為 `cortex-work/v1`；WorkItem/WorkSource 欄位、排序、`on-going` spelling 與 explain trace 依 `CONTEXT.md` 定義。Monitor socket新增 `list_work_items`、`get_work_item`、`explain_work_item` 與 work subscription；既有 ProjectState request/response 保留一個 release cycle並標 deprecated。

## Alternatives considered

### 以 GitHub Project 作唯一 truth source

不採用。Repo 內 spec/plan/OpenSpec 是實作與驗收 authority，且 local uncommitted overlay 對 operator 有用；全搬到 GitHub 會失去 file-driven workflow。不過 terminal done 仍以 authenticated GitHub/default branch 為 canonical，local overlay不能證明完成。

### 以標題/branch slug 自動合併所有來源

不採用。這能提高表面命中率，卻會在同名 issue/spec 或跨 repo slug collision 時授權錯誤工作。Heuristic 僅供 display/explain。

### 每個 provider 失敗就清空來源

不採用。Network/rate-limit/mount race 會造成 false removal、false done 或重複 dispatch。採 per-provider last-good 與 degraded freeze。

### 增加 `blocked`、`needs_human` lifecycle state

不採用。這會混淆「工作是否開始」與「目前是否需要介入」，也讓 state reducer 與 filter 膨脹。改用 facets。

### 讓 CLI 直接寫 workflow registry

不採用。這會破壞 single-writer、使 restart idempotency 與 claim key 競態不可證明。所有 mutation 經 Manager queue。

### 使用 GitHub auto-merge 或只依 branch protection

不採用。目前 repo 沒有可依賴的 branch protection且 `allow_auto_merge=false`；Manager 必須在 exact HEAD 重新讀 gate 後主動 merge commit。

## Consequences

### Positive

- 使用者看到的四態具有跨來源、可解釋且可回退的明確語意。
- Provider outage 不再造成 destructive projection。
- Confirmed/inferred 分離，阻止模糊匹配升級成 mutation authority。
- Persona binding、planner peer 與 reviewer domain separation 可由 manifest/evidence 驗證。
- Done 成為 remote terminal fact，而非 agent claim。

### Costs and risks

- Authenticated GitHub snapshots、review threads 與 checks 增加 rate-limit/latency；預設 300 秒 refresh，900 秒 freshness gate。
- v1 JSON registry/snapshot 會成長；只有 scale/audit 證據顯示不足時才導入 journal/database。
- Copilot不保證 push 後自動重審，Manager 必須 request + bounded wait，可能導致 needs_human。
- 其他 forge v1只能顯示 read model；terminal delivery 留 needs_human。

## Rollout and rollback

1. 先落地 issue #4 scan stability、清理 stale active OpenSpec，建立乾淨 baseline。
2. PR A 僅啟用 Monitor read model/CLI read path，保留舊 ProjectState API。
3. PR B 升 workflow registry v2 並加入 persona/agy；v1 state 原子備份，legacy job/slice 不自動關聯 work item。
4. PR C 才啟用 manual/label claim 與 delivery mutation；auto default off。
5. PR D 補 doctor/docs/service migration，以 repo 內 docs-only canary 驗全鏈。
6. Canary 通過前，其他 repo 不加 `cortex:auto-on-going` label。

Rollback 可停用 Manager auto claim 並退回前一版 binary；不得用舊 binary 改寫新 registry。Monitor v1 snapshot 可刪除後重建，但若 provider degraded，應保留檔案供診斷，不把空 scan 當 truth。

## Verification

- Provider failure/restart、state reducer/reopen、correlation collision與 override restart fixtures。
- Workflow claim idempotency、missing issue、persona binding/domain separation、agy failure fixtures。
- Old-HEAD/error Copilot review、unresolved thread、checks/HEAD race、remote archive與 merge ancestry fixtures。
- Full pytest、`openspec validate --all --strict`、policy check、pinned preflight、live docs-only canary。
