---
status: accepted
work_item: openspec-remote-archive-authority-reconciliation
---

# OpenSpec local-active／GitHub-archived authority 衝突收斂設計（#961）

## Decisions

### D1 先收集，再對每個 semantic key 裁決

在 _authority_from_canonical_row 保留現有 confirmed source 過濾、來源欄位檢查與 semantic_source_revision 呼叫。把目前 semantic_sources.setdefault 後立即比較的單 pass 改為兩段：

1. 遍歷 confirmed sources，把每個非 None 的 (key, value) 和原 source 一起收進 observed[key]。
2. 逐 key 處理：只有一個唯一 value 時照既有結果寫回；多個 value 時呼叫窄範圍 resolver；resolver 回 None 即拋原 AuthorityValidationError，否則寫回 archived value 並記一行 warning。

所有與此修正無關的 source skip、identity validation、mapping lists 與 sorted output 都保持既有順序和語意。以 set 判斷 value 群，不依 source 遍歷次序決定權威。

### D2 使用純 resolver 並只接受 exact proof chain

新增純 helper，契約建議為：
_merged_remote_archive_resolution(*, key, rows, repo, confirmed, providers) -> str | None

helper 不讀檔、不呼叫 GitHub、不修改 snapshot。它只判定 #887 R5：
- key 必須等於 openspec:{repo}:{ref}，且 rows 的唯一 values 恰為 active 與 archived 的精確 identity revision。
- active/archived contributing sources 的 provider 角色必須分別是 repo:{repo} 與 github-terminal:{repo}。
- confirmed github_pr sources 必須非空，每個 source 狀態在 {closed, merged}。
- providers[github-terminal:{repo}] 必須是 mapping 且 status=ok；observations 必須是 mapping，remote_prs 必須是 list。
- 每個 confirmed PR source_id 必須在 remote_prs 中恰找到一筆同 id、merged_with_merge_commit is True 的 row。不存在、重複（即使都為 true）、型別不符或來源 ID 對不上即回 None。

不把 PR status=closed 視為 merge proof，也不憑 OpenSpec archived tree row 本身推論 PR 已 merge；同一 PR 的 GitHub terminal ancestry result 才是 allow condition。

### D3 Resolver 失敗直接沿用原錯誤

不引入新 exception、fallback reason 或 degraded acceptance。resolver 回 None、其他 semantic key 衝突或超過兩個 values 時，使用現有錯誤：
- base message: confirmed semantic work authority revisions conflict
- reason_code: row-malformed
- field: source_revisions
- repo/work_id 仍用既有安全 diagnostic label

這讓所有既有呼叫者繼續 fail-closed，且不需要改監控或 Manager 呼叫鏈。

### D4 只替換一個 revision value，其他投影不動

Arbitration 只決定 semantic_sources[key] 的最終 value，不增刪 key、不改 source_id membership，也不重算 mapped_openspec 的來源內容。source_revisions 仍照既有 key 排序產生；mapped_openspec／changes 仍照既有 sorted(set(...))。所以成功結果在 value/digest 層必須 bit-for-bit 等同於「相同 snapshot 移除本機 active source」的 WorkAuthority。

### D5 Warning 只觀測 resolver 成功事件

在主收斂 loop 的 resolver success 分支記一行 warning。repo、work_id 和 change label 由安全診斷值輸出，change 一律經 _diagnostic_label；不得輸出 path、workspace root 或 raw snapshot bytes。非收斂 conflict 不另加 warning，保留原 exception 作為拒絕結果。測試需分開驗證「一個 successful key 一行」及「失敗時 exact exception」。

### D6 測試沿用 source shapes，避免與 #962 混 scope

測試新增在 tests/test_post_merge_authority_restart_guard.py。用本地小型 canonical snapshot fixtures 建出三類 confirmed provider sources，沿既有 tests/test_monitor_work_review_regressions.py 的 source/remote_prs shape；不要從 issue 現場複製 workspace path，也不要透過 provider network call。

測試以 public WorkAuthority load 為主要 oracle：
1. 可收斂正例及移除 local source 的 digest/source_revisions/mapped_openspec 等同性。
2. 遍歷 source 順序及 warning 一次性。
3. 缺失/錯誤 remote merge proof 和其他 R7 values 的 fail-closed。
4. 保留既有 claim/provider tests，不修改原斷言。

#962 後續會在同一新測試檔加入 run completion cases；按 issue dependency 先合併 #961，再擴充同檔。

### D7 五維 sizing 與 Yellow 分支結論

本票 scope 是 claim.py 中單一 coordinator module 的純 authority projection arbitration，沒有 durable write 或新狀態。以 live #961 的 fix 類工作和目前 repo sizing contracts 計算：

| 維度 | 分數 | 推導 |
|---|---:|---|
| domain_breadth | 0 | 僅一個 production module（claim.py），仍在 coordinator 同一領域 |
| state_consistency | 0 | 不寫 registry/journal/outcome、不新增 persisted state；只決定 WorkAuthority projection |
| acceptance_surfaces | 2 | fix-standard 有 2 個 gate_spine；repo code-change sizing 固定帶 R-09/R-16/R-19 三條規則，signal=5，mechanical score=2 |
| spec_stability | 0 | spec/design/todo 三件 accepted、AC 完整、無未解 blocking marker |
| orchestration | 2 | fix-standard 有 9 cards 且 9 張都有 persona_binding，符合 cards>1 且 bindings>1 |
| **總分／band** | **4 / Yellow** | 五維加總；band 門檻 0–3 Green、4–6 Yellow、7–10 Red |

Inputs are verified from the current repo: deck task type fix maps to fix-standard; its combo has 9 cards, 2 gate_spine entries, and every card has a persona_binding; planning.ACCEPTANCE_SURFACE_RULES is {R-09,R-16,R-19}; compute_sizing_score implements the thresholds above. invariant_count and artifact_classes are required plan frontmatter declarations, not extra score dimensions.

這是 #961 獨立 scope 的 sizing，不把 #887 原始 7/Red aggregate 壓低或移除 R1–R8。由於本票是 4/Yellow，無需再對 #961 做 issue-backed 子拆分；#962 的五維 sizing 仍由其自己的 accepted intake 計算，不在本設計背書。

## Alternatives considered

- 全面忽略 OpenSpec active/archived conflict：拒絕。會把任何來源漂移都視為 archive，擴大 authority 信任範圍。
- 以 github_pr status=closed 判定 merge：拒絕。當前 GitHub issues source 對已 merge PR 可以只表現為 closed，缺少 merge commit ancestry。
- 永遠採 archived whenever present：拒絕。remote provider degraded、PR 未 merged、provider 角色對調或其他 semantic key 都應繼續 fail-closed。
- 先刪除本機 source 再 load：拒絕。這會把 operator workspace mutation 當成 correctness requirement；本票應在 canonical snapshot 的 authority arbitration 層安全判定。
- 直接採第一筆／最後一筆 source：拒絕。結果會依 provider/source iteration order 改變。

## Operational boundary

本設計不增加 runtime IO 或 durable state；canonical snapshot provider status、confirmed sources 與 remote_prs 只作為 resolver 的既有輸入。任何必要 evidence 欄位缺席就回到原 AuthorityValidationError。此處只讓 #887 的特定已 merged authority refresh 能繼續 load；#962 仍獨立負責 Manager merge authorization、run recovery 與 CompletionRecord。
