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
