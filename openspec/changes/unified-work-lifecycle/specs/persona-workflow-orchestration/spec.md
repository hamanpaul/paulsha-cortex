## ADDED Requirements

### Requirement: Manager必須是WorkflowRun與mutation的單一writer
Manager MUST以registry schema v2保存`WorkflowRun`與`WorkflowStep`，並以`repo/work-id/authoritative source revisions` claim key確保restart idempotency。V1 registry MUST先建立不可覆寫atomic backup才升v2；舊jobs/slices MUST保存為legacy records且 MUST NOT推測work item。CLI mutation MUST經control request queue。

每張非Manager card MUST由production dispatcher建立綁定run、claim、repo、source revision、phase、card、persona與model identity的durable Job。Manager MUST只在該Job successful terminal後建立canonical coordinator-root evidence並原子綁回Job；caller-supplied evidence path/hash MUST被拒絕。Periodic terminal poll MUST可在restart後由registry重建目前card並繼續推進；同phase所有card通過前 MUST NOT前進。

#### Scenario: Claim後crash/restart
- **WHEN**相同claim key在restart後再次送達
- **THEN** Manager回傳既有WorkflowRun
- **THEN**不重複建立job、branch、worktree或PR

#### Scenario: Malformed v1 registry
- **WHEN**migration輸入schema malformed或backup無法durably寫入
- **THEN** Manager拒絕migration且原檔不變

### Requirement: Claim必須manual-first且auto需要confirmed Todo與label issue
`cortex work start` MUST能手動啟動confirmed Todo。Auto claim MUST同時要求confirmed Todo、confirmed GitHub issue mapping與issue label `cortex:auto-on-going`。Todo缺issue MUST NOT自動建issue，必須建立或維持`needs_human:missing_issue`。移除label MUST只阻止尚未claim工作，不中止active run。

#### Scenario: Issue-only帶auto label
- **WHEN** open issue有auto label但沒有confirmed Todo
- **THEN** item維持topic且不建立WorkflowRun

#### Scenario: Todo缺issue後人工link
- **WHEN** Todo因missing issue停在needs_human，operator link issue後呼叫resume
- **THEN** Manager重用既有run/claim metadata並繼續
- **THEN**不自動建立第二張issue

### Requirement: Workflow manifest必須保留每步persona binding
Deck compiler MUST把每張card的`persona_binding`寫入workflow manifest；Manager MUST依step使用planner、builder、reviewer或manager persona，不得以global builder覆蓋。Default combo MUST為`feature-oneshot`。每個WorkflowStep MUST保存phase、persona、card、executor/model/domain、inputs、outputs與gate result。

#### Scenario: Combo含不同persona cards
- **WHEN** Deck compile feature-oneshot
- **THEN** manifest逐step保存原card persona binding
- **THEN** launch metadata與manifest persona一致

### Requirement: 不完整規格必須經異質雙模型brainstorm
Artifact只有在frontmatter `status: accepted`、必要章節存在且沒有blocking decision marker時才算accepted。Marker parser MUST只把獨立行`TBD`、`[TBD]`、`Decision: TBD`、`決策：未定`或Open Questions中的實際項目視為blocking，MUST忽略inline說明與fenced code。Accepted spec/design/plan缺失或有blocking marker時，primary planner MUST先產question pack；secondary planner MUST來自不同independence domain且只回evidence；primary MUST整合並落檔。Secondary選擇 MUST依可用的`agy/google → claude/anthropic → codex/openai`順序排除primary domain；無異質model、unknown identity或malformed output MUST fail-closed。

Planner subprocess與manifest plan card MUST只在temporary disposable checkout執行，並以read-only executor模式啟動；Claude MUST使用`plan`且停用tools、Codex MUST使用`--sandbox read-only`。Manager MUST在成功、nonzero或exception路徑驗sandbox與operator worktree的檔案、empty dirs、directory symlinks與stable metadata；任何修改 MUST fail-closed，operator checkout MUST回復且不得殘留。Primary只回傳structured artifact content；Manager MUST先驗所有path均綁定目前work/change與manifest outputs。新檔 MUST no-clobber；既有TBD artifact只有在current hash等於scan baseline時才可CAS replacement。Artifact、immutable brainstorm evidence與registry phase update MUST由durable intent journal形成recoverable transaction；save fault MUST rollback整組，restart/resume MUST依persisted brainstorm gate reconcile，且不得覆蓋其他work item。

#### Scenario: Agy可用且primary非Google
- **WHEN** completeness gate觸發且agy live capability/identity probe通過
- **THEN** secondary使用Google domain回傳evidence
- **THEN** primary負責final decisions與artifact write

#### Scenario: 只剩same-domain model
- **WHEN**所有可用secondary都與primary同domain
- **THEN** WorkflowRun設needs_human且不進build

### Requirement: Verify與Review必須產生manifest-declared report
Verify與review terminal payload MUST列出實際report outputs；每個output MUST匹配該card的manifest glob、存在於綁定repo root且content hash可重驗。Canonical coordinator evidence locator MUST只作gate evidence，MUST NOT被計為report output。

#### Scenario: Reviewer只有canonical evidence而沒有report
- **WHEN**review job成功結束但沒有產生manifest宣告的report
- **THEN**Manager拒絕terminalize或phase advance
- **AND**canonical evidence path不得補足缺少的report

### Requirement: Agy launcher必須使用safe plan sandbox
`agy` launcher MUST使用headless print、plan mode與sandbox，MUST NOT加入unsafe permission bypass。Model identity registry MUST登錄`agy + Gemini 3.1 Pro (High)`為`google`並由`doctor --probe-live`驗capability；版本字串 alone MUST NOT視為可用。

#### Scenario: CLI介面漂移
- **WHEN**agy存在但plan/sandbox smoke失敗或model identity不符
- **THEN**doctor回non-ready diagnostic
- **THEN**Manager不得選agy執行brainstorm

### Requirement: Brainstorm peer與Foreign Reviewer必須是獨立gate
Builder MUST在`feature/<issue>-<slug>` worktree執行deterministic Candidate verification。Foreign Reviewer MUST使用不同於Builder的independence domain並在detached exact Candidate HEAD審查。Brainstorm peer、ForeignReview與Copilot review MUST保存不同gate/evidence refs，任一 MUST NOT取代另一個。

#### Scenario: Brainstorm peer也是可用reviewer
- **WHEN**同一model曾產生planning evidence
- **THEN**該evidence不能滿足ForeignReview
- **THEN**Manager仍需按review gate選擇不同於Builder domain的reviewer step
