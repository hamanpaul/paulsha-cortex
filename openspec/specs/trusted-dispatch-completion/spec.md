# trusted-dispatch-completion Specification

## Purpose
定義 cortex 從 Job exit、deterministic verification、ForeignReview、CompletionRecord 到 target-ancestry dependency release 的 fail-closed 信任鏈與人工復原契約。
## Requirements
### Requirement: Execution與delivery狀態必須分離
系統MUST以versioned atomic coordinator state分別保存Job execution與Slice delivery。exit code 0只能令Job進入`exited`，不得直接產生CompletionRecord、`completed` Slice或滿足`depends_on`。系統MUST拒載legacy或未知schema且不得自動清空或migration。

#### Scenario: Agent成功退出但尚未驗證
- **WHEN** builder Job以exit code 0結束
- **THEN** Job成為`exited`且Slice進入verification path
- **THEN** downstream維持blocked

#### Scenario: 啟動遇到legacy state
- **WHEN** coordinator讀到缺少支援schema version或含legacy `done`的`jobs.json`
- **THEN** coordinator拒絕啟動並顯示state路徑與archive/remove指引
- **THEN** 原state檔保持不變

#### Scenario: 使用舊低階direct dispatch
- **WHEN** operator嘗試使用沒有spec/plan/verification metadata的legacy direct dispatch介面
- **THEN** CLI明確拒絕並指示使用spec-driven control request
- **THEN** 系統不寫Job、Slice或CompletionRecord

#### Scenario: Daemon未運行時要求mutation
- **WHEN** operator呼叫`dispatch/fanout/tick/complete/slice-action`且沒有manager daemon可消費control request
- **THEN** CLI以明確錯誤結束且不直接寫`jobs.json`

### Requirement: Candidate必須接受deterministic ResultVerification
系統MUST在builder exit後固定exact Candidate SHA，確認dispatch base為其ancestor且兩者不同，並依dispatch時pin住的contract驗required artifacts、`must_change`、persona scope、明列的policy/docs/security commands、task commands與full suite。command MUST使用typed argv且不得經shell；env只保留`PATH`、`HOME`、`LANG`、`LC_ALL`、`TMPDIR`、`VIRTUAL_ENV`中既有值。Candidate command/full suite MUST exit 0；base full suite可non-zero但runner本身必須可信完成。缺失、timeout、signal、exception、兩邊皆non-zero、未知或不完整evidence MUST fail-closed。

#### Scenario: Exit 0但必要產物缺失
- **WHEN** Candidate的builder Job exited但verification找不到required artifact
- **THEN** Slice進入`needs_human`
- **THEN** 系統不建立CompletionRecord

#### Scenario: Candidate ref被force-update
- **WHEN** manager固定Candidate後branch ref偏離該SHA或Candidate不再是dispatch base descendant
- **THEN** 原verification/review不能沿用
- **THEN** Slice進入`needs_human`

#### Scenario: Builder沒有產生新commit
- **WHEN** builder Job exited且branch HEAD等於dispatch base
- **THEN** Slice進入`needs_human`，即使既有artifact存在且tests全綠
- **THEN** 系統不建立no-op proof或CompletionRecord

#### Scenario: Informational文件不需要semantic review
- **WHEN** `docs_class=informational`或`trivial`且deterministic checks全部通過
- **THEN** 系統以明確`review_policy=not-required` proof令Slice進入`verified`
- **THEN** reviewer Job與GateEvaluation可為空

### Requirement: Normative與code task必須取得ForeignReview
系統MUST為`normative`與`code` Candidate建立獨立reviewer Job。reviewer的`independence_domain` MUST不同於builder，且manager MUST以launch metadata固定explicit executor/model identity與detached exact Candidate checkout。每個reviewer Job的GateEvaluation MUST terminal後immutable；stale input只能清除Slice current ref並記reason，不得修改舊evaluation。finding category MUST限定為`correctness|acceptance|security|data-loss|race|scope-bypass|verification-bypass|style|pre-existing-out-of-scope`，severity MUST為`critical|important|minor`，且每筆MUST含非空summary、recommendation與`evidence[]`。evidence item MUST為repo-relative path、positive line或null、non-empty detail。manager MUST以category、summary與排序後evidence的sorted-key JSON SHA-256產生finding ID。

#### Scenario: 不同CLI但同一independence domain
- **WHEN** reviewer executor與builder不同但兩者model identity映射到相同domain
- **THEN** GateEvaluation成為`absent`
- **THEN** Slice進入`needs_human`

#### Scenario: Verdict綁定stale Candidate
- **WHEN** verdict的subject HEAD或input hashes與current Candidate不一致
- **THEN** verdict只保留為audit evidence且不能成為current evaluation
- **THEN** 系統需要新的reviewer Job才能繼續

#### Scenario: Reviewer回報blocking finding
- **WHEN** validated verdict包含cortex policy分類為blocking的finding
- **THEN** GateEvaluation成為`rejected`
- **THEN** Slice進入`needs_human`且不釋放downstream

### Requirement: Completion必須由target ancestry與一致證據證明
系統MUST只在verified Candidate為configured remote-tracking target branch ancestor時完成Slice。CompletionRecord MUST帶schema version、input hashes、builder Job ID、Candidate、target、verification ref與review policy。required review MUST保存non-null reviewer Job/Gate refs；not-required MUST保存null refs與docs class+contract hash proof。系統MUST先atomic寫CompletionRecord，再atomic標記Slice`completed`；readiness MUST同時驗兩者與current ancestry。

#### Scenario: Review通過但Candidate尚未merge
- **WHEN** Slice已verified但Candidate不是remote target ancestor
- **THEN** Slice維持`verified`
- **THEN** `depends_on`維持不滿足

#### Scenario: Record寫入後state更新前crash
- **WHEN** CompletionRecord已atomic寫入但Slice仍為`verified`
- **THEN** readiness回false
- **THEN** restart重新fetch target，只在record、Slice與current ancestry完全匹配時補完`completed`

#### Scenario: Crash window期間target移除Candidate
- **WHEN** CompletionRecord已寫但restart時Candidate已不是remote target ancestor
- **THEN** 系統不得把Slice補成`completed`
- **THEN** Slice維持blocked並呈現可診斷reason

#### Scenario: Downstream dispatch重新驗actual base
- **WHEN** downstream在readiness判斷後準備建立worktree
- **THEN** 系統解析remote target的actual base SHA並重新驗每個upstream Candidate為其ancestor
- **THEN** 任一驗證失敗時不得建立或launch downstream worktree

### Requirement: Human recovery必須明確且可追蹤
系統MUST提供local `retry-build`、`retry-verify`、`retry-review`與`abandon` actions。CLI MUST透過既有atomic control request queue送出action；daemon/manager作為state單一writer保存action、actor與結果。status MUST一次列出所有Slice狀態、阻擋理由、evidence摘要與合法下一步，不得將remote或agent自述視為human override。

#### Scenario: Operator重跑review
- **WHEN** `needs_human` Slice有可信verification evidence且operator提交`retry-review`與actor
- **THEN** 系統保存action history並建立新的reviewer Job與GateEvaluation
- **THEN** 舊evaluation維持immutable audit record

#### Scenario: Status呈現多筆人工事項
- **WHEN** 多個Slice同時處於`needs_human`
- **THEN** 單次status response列出全部Slice、原因與允許action
- **THEN** 系統不要求operator逐筆互動確認

### Requirement: Slice spec必須能宣告per-slice builder identity且宣告值經registry驗證

spec frontmatter MUST 接受 optional `executor`/`model_id` 成對宣告（皆非空字串）；僅宣告其一 MUST 產生 `invalid-frontmatter` parse_error 且 field 指向缺漏欄。dispatch 前宣告的 `(executor, model_id)` MUST 存在於 model-identities registry（packaged＋instance custom 合併）；unknown identity MUST fail-closed——該 slice 不建 worktree、不啟動任何 model session、標 `needs_human`，錯誤訊息 MUST 列出可用 identity 清單——且 MUST NOT 靜默退回 fanout 層預設。單一 slice 驗證失敗 MUST NOT 影響同批其他 slice 派工。`EMITTED_FRONTMATTER_FIELDS` MUST 與 runtime 解析契約同步納入兩欄；deck compile MUST NOT 輸出這兩欄。

#### Scenario: 單一 specs-dir 內異質 executor 各自派工

- **WHEN** 同一 specs-dir 內兩個 ready slice 各宣告不同的已註冊 `executor`/`model_id`，operator 執行一次 fanout
- **THEN** 兩個 slice 各自以宣告 identity 構建 launcher 派工，model 進入 job dispatch argv
- **THEN** 各 job row 的 `executor`/`model_id` 記錄宣告值，可供稽核

#### Scenario: 只宣告 executor 未宣告 model_id

- **WHEN** spec frontmatter 只宣告 `executor` 而缺 `model_id`
- **THEN** parse 產生 `invalid-frontmatter` parse_error 且 field 指向 `model_id`，slice 維持 hold 不派工

#### Scenario: unknown identity fail-closed 且不波及同批

- **WHEN** 某 slice 宣告 registry 沒有的 `(executor, model_id)` 對，同批另一 slice 未宣告
- **THEN** 該 slice 不啟動任何 model session、標 `needs_human`，錯誤訊息列出 registry 可用的 `executor/model_id` 清單
- **THEN** 同批另一 slice 照常以 fanout 層預設派工

### Requirement: 未宣告per-slice identity的slice行為位元不變

未宣告 `executor`/`model_id` 的 slice MUST 沿用呼叫端傳入的 fanout 層預設 launcher，dispatch 全路徑（prompt、pinned inputs、worktree、dispatch_head、handoff、commit-required 轉換、`--allow-unsafe` canary 限制）MUST 與現行為位元一致；parse meta 僅新增值為 None 的兩個 key。

#### Scenario: 既有 specs 未宣告 identity

- **WHEN** 既有 spec（無 `executor`/`model_id` 欄位）經 fanout 派工
- **THEN** 使用 fanout 層預設 launcher，派工行為與宣告欄位落地前完全一致
- **THEN** 不觸發任何 model-identities registry 載入或驗證

### Requirement: 批外depends_on必須有顯式分類診斷

依賴診斷 MUST 三分：dep 在本批 metas 內未完成 → `deps-unsatisfied:<id>`（維持現行字串）；dep 不在本批但 handoff 目錄存在該 slice_id 的 manifest → `deps-external:<id>`；dep 不在本批且無任何 handoff trace → `deps-unknown:<id>`。`cortex status` 的 `held[].reasons` MUST 呈現此分類；`cortex ready` 對含 `deps-unknown` dep 的 slice MUST 於 stderr 印顯式診斷且 stdout JSON 與 exit code MUST 不變。cycle 偵測對批外邊 MUST 維持不算環，MUST NOT 因 `deps-unknown` 拒絕整批。

#### Scenario: depends_on 打錯字

- **WHEN** 某 slice 的 `depends_on` 指向一個不存在於本批且無任何 handoff manifest 的 slice_id
- **THEN** `cortex status` 的 held reasons 對該 slice 顯示 `deps-unknown:<id>`
- **THEN** `cortex ready` 於 stderr 印出對應診斷，stdout 輸出與 exit code 不變

#### Scenario: 合法跨 specs-dir 依賴

- **WHEN** 某 slice 依賴另一 specs-dir 已派工過、handoff 目錄留有 manifest 但尚未滿足的 slice
- **THEN** held reasons 顯示 `deps-external:<id>` 而非 `deps-unknown:<id>`
- **THEN** 滿足性判定與釋放行為維持現行（manifest 有效即釋放下游）

#### Scenario: in-batch 未完成依賴維持原字串

- **WHEN** dep 存在於本批 metas 且尚未完成
- **THEN** held reasons 維持 `deps-unsatisfied:<id>`，既有消費者不受影響

### Requirement: request層明確宣告的builder identity必須經registry驗證

fanout／tick／dispatch request 與 periodic tick 在 executor 與 model 皆為明確值（含 daemon default 帶入）時，MUST 於派工前以 model-identities registry 驗證 `(executor, model)`；unknown MUST fail-closed——request 回 error、periodic tick 記 tick error 且本輪不派工——錯誤帶可用 identity 清單且 MUST NOT 啟動任何 model session。model 未指定時 MUST 維持現行為（不做 registry 驗證，executor 白名單照舊把關）。

#### Scenario: fanout request 帶未註冊 model

- **WHEN** operator 送出帶明確 `--executor`／`--model` 且 registry 查無該對的 fanout request
- **THEN** request 以 error 結束並列出可用 identity 清單，未啟動任何 model session、未派任何 job

#### Scenario: 不帶 model 的既有呼叫不受影響

- **WHEN** operator 送出只帶 `--executor`（無 `--model`）的 fanout request
- **THEN** 派工行為與現行完全一致，不觸發 registry 驗證

