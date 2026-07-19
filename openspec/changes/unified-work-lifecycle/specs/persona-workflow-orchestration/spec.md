## ADDED Requirements

### Requirement: Manager必須是WorkflowRun與mutation的單一writer
Manager MUST以registry schema v2保存`WorkflowRun`與`WorkflowStep`，並以`repo/work-id/authoritative source revisions` claim key確保restart idempotency。V1 registry MUST先建立不可覆寫atomic backup才升v2；舊jobs/slices MUST保存為legacy records且 MUST NOT推測work item。CLI mutation MUST經control request queue。

每張非Manager card MUST由production dispatcher建立綁定run、claim、repo、source revision、phase、card、persona與model identity的durable Job。Manager MUST只在該Job successful terminal後建立canonical coordinator-root evidence並原子綁回Job；caller-supplied evidence path/hash MUST被拒絕。Periodic terminal poll MUST可在restart後由registry重建目前card並繼續推進；同phase所有card通過前 MUST NOT前進。

Claim key MUST雜湊該work item的canonical semantic authority與provider/source revisions，MUST NOT納入snapshot sequence、written-at、whole-fleet hash或其他repo noise。

#### Scenario: Claim後crash/restart
- **WHEN**相同claim key在restart後再次送達
- **THEN** Manager回傳既有WorkflowRun
- **THEN**不重複建立job、branch、worktree或PR

#### Scenario: Snapshot只有fleet metadata更新
- **WHEN**同一work item的provider/source revisions與confirmed refs未變，但snapshot sequence、written-at或其他repo資料改變
- **THEN**Manager重用既有WorkflowRun與claim key，只更新snapshot provenance

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

### Requirement: Workflow manifest必須保留每步persona binding與execution contract
Deck compiler MUST把每張card的`persona_binding`、`skill_ref`、task-specific action、commit policy與test policy寫入workflow manifest；Manager MUST依step使用planner、builder、reviewer或manager persona，不得以global builder覆蓋。Default combo MUST為`feature-oneshot`。每個WorkflowStep MUST保存phase、persona、card、executor/model/domain、inputs、outputs、execution contract與gate result。

#### Scenario: Combo含不同persona cards
- **WHEN** Deck compile feature-oneshot
- **THEN** manifest逐step保存原card persona binding
- **THEN** launch metadata與manifest persona一致

### Requirement: 不完整規格必須經異質雙模型brainstorm
Artifact只有在frontmatter `status: accepted`、必要章節存在且沒有blocking decision marker時才算accepted。Marker parser MUST只把獨立行`TBD`、`[TBD]`、`Decision: TBD`、`決策：未定`或Open Questions中的實際項目視為blocking，MUST忽略inline說明與fenced code。Accepted spec/design/plan缺失或有blocking marker時，primary planner MUST先產question pack；secondary planner MUST來自不同independence domain且只回evidence；primary MUST整合並落檔。Secondary選擇 MUST依可用的`agy/google → claude/anthropic → codex/openai`順序排除primary domain；無異質model、unknown identity或malformed output MUST fail-closed。

Planner subprocess與manifest plan card MUST只在temporary disposable checkout執行，並以read-only executor模式啟動；Claude MUST使用`plan`且停用tools、Codex MUST使用`--sandbox read-only`。Manager MUST在成功、nonzero或exception路徑驗sandbox與operator worktree的檔案、empty dirs、directory symlinks與stable metadata；snapshot遇權限錯誤也 MUST先恢復安全traversal再依baseline還原entries、mode與xattrs，restore fault MUST fail-closed。Primary只回傳structured artifact content；Manager MUST在scan時持久化canonical ref、kind、work item與content hash authority，replacement MUST逐欄符合該authority及manifest outputs，不得信任caller hash或filename推測。新檔 MUST no-clobber。Artifact、immutable或既存同內容brainstorm evidence、expected gate ref與registry phase update MUST由durable intent journal形成recoverable transaction；registry未commit的save fault MUST rollback，已commit的restart/resume MUST逐operation重驗type/hash/mode/evidence後保留產物，drift MUST設`needs_human`並保留journal，且不得覆蓋其他work item。

#### Scenario: Agy可用且primary非Google
- **WHEN** completeness gate觸發且agy live capability/identity probe通過
- **THEN** secondary使用Google domain回傳evidence
- **THEN** primary負責final decisions與artifact write

#### Scenario: 只剩same-domain model
- **WHEN**所有可用secondary都與primary同domain
- **THEN** WorkflowRun設needs_human且不進build

### Requirement: Verify與Review必須產生manifest-declared report
Manager MUST在每張card dispatch時持久化output目錄baseline。Verify與review terminal payload MUST列出實際report outputs；每個output MUST匹配該card的manifest glob、存在於綁定repo root、為該job後新建或相對baseline已更新，且report frontmatter MUST精確綁定WorkflowRun、card與Candidate。Canonical coordinator evidence MUST保存current與baseline hash；其locator MUST只作gate evidence，MUST NOT被計為report output。

Verify/review identity MUST以schema v2明示`review` capability且其independence domain MUST不同於Builder。Reviewer launcher MUST採executor enforced read-only mode並只在exact Candidate的disposable clone執行；Claude reviewer MUST使用非互動`dontAsk`與safe-mode而非Plan Mode、不得載入Candidate customization、remote session或MCP，且工具面 MUST只暴露OS-sandboxed Bash並 MUST要求structured JSON object。Filesystem MUST拒讀home、runtime user sockets與Docker sockets；symlink aliases MUST先解析、去重成canonical targets，仍只重開Candidate、必要Python user-site工具鏈與解析後的官方SRT package root。Bash MUST在fail-if-unavailable、禁止unsandboxed fallback、Candidate deny-write且隔離credential paths/env的OS原生sandbox內執行。Reviewer subprocess MUST採非密鑰環境正向allowlist且 MUST NOT啟動login shell。缺Claude Code 2.1.187+、必要CLI surface、sandbox runtime dependency、live native smoke、以實際review filesystem policy執行的smoke或Unix-socket seccomp smoke MUST fail-closed。Checkout後 MUST移除所有Git remotes；Claude protected-path bind targets MUST只建立在deterministic disposable session root，exact Candidate MUST固定在其`candidate/` checkout，且兩者 MUST分離以維持material tree與Git status乾淨。Manager MUST在terminal、launch failure及operator retry清理路徑重驗原Candidate完整tree snapshot，任何drift MUST fail-closed。Agent MUST只回substantive verification/findings與inline report body。Manager MUST從durable Job建構slice/Candidate、builder/reviewer job IDs、launch identity、finding ID/blocking/state與frontmatter。Report MUST限於phase專屬`reports/verify/*.md`或`reports/review/*.md`，並以durable intent journal將multi-report CAS、canonical evidence與registry bind包成可rollback/roll-forward transaction；partial write、save fault或crash replay MUST不得留下未綁定publication。Journal recovery path MUST為canonical repo內phase專屬Markdown path，任何`..`、symlink、duplicate或repo escape MUST fail-closed。Agent提供的immutable binding欄位或直接Candidate/report寫入 MUST NOT被採信。

#### Scenario: Claude reviewer受Plan Mode或sandbox dependency阻擋
- **WHEN**Claude reviewer無法執行non-mutating test、sandbox dependency缺失、要求unsandboxed fallback，或`exited-0`卻沒有terminal payload
- **THEN**Manager不得建立evidence或自動重派
- **AND**只有run/claim/repo/source/card/phase/Candidate/builder/reviewer identity/output/sandbox snapshot全部精確綁定、原始Candidate root精確等於已驗證Builder Job worktree且Candidate未漂移時，explicit operator resume才可保留舊Job/log後重派

#### Scenario: Reviewer嘗試改寫Candidate或manifest授權過寬
- **WHEN**reviewer在disposable clone外改寫原Candidate，或terminal report path離開該phase專屬Markdown root
- **THEN**Manager拒絕terminalize、清除disposable clone，且不得發布report或canonical evidence

#### Scenario: Report publication中途失敗
- **WHEN**多report寫入、canonical evidence建立或registry bind任一步驟失敗或crash
- **THEN**Manager依durable intent journal安全rollback未綁定內容，或在已綁定evidence時roll-forward相同hash內容後清除journal

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
Builder MUST在`feature/<issue>-<slug>` worktree執行deterministic Candidate verification。Foreign Reviewer MUST使用不同於Builder的independence domain並在detached exact Candidate HEAD審查。Brainstorm peer、ForeignReview與current-HEAD delivery review MUST保存不同gate/evidence refs，任一 MUST NOT取代另一個；delivery review MUST明確標為`copilot`或`maintainer-review`，不得互相偽裝。

#### Scenario: Brainstorm peer也是可用reviewer
- **WHEN**同一model曾產生planning evidence
- **THEN**該evidence不能滿足ForeignReview
- **THEN**Manager仍需按review gate選擇不同於Builder domain的reviewer step
- **AND**該identity仍須明示`review` capability；只有planning capability不得被選用

### Requirement: Declared inputs必須形成hash-bound handoff
Manager MUST先以canonical brainstorm evidence綁定的scope與artifact ref/kind/hash，把brainstorm新發布artifact原子併入WorkflowRun planning authority，並保存不可變發證source revision；PR refresh MAY更新run目前source revision但MUST NOT改寫發證revision。Legacy active run也MUST只由相同evidence reconcile，MUST NOT從mutable workspace猜測authority；`brainstorm_required=true`卻缺evidence MUST fail-closed。Manager接著MUST在launch前解析目前card與同phase既有card的declared input globs；每個glob MUST至少命中一個regular non-symlink UTF-8檔案。Planning artifact命中 MUST與WorkflowRun的planning authority ref/hash一致；builder worktree缺檔時 Manager MAY從該authority原子seed，但evidence/operator artifact drift、destination conflict或未授權的同glob替代檔 MUST fail-closed。Codex MUST使用`workspace-write`；workflow的`commit_policy=required`及legacy fanout／dispatch／retry-build的builder persona MUST取得明確commit capability，並只將Git解析且驗證的current-worktree gitdir、shared objects、current-branch ref/reflog parent directories以`--add-dir`開放。Launcher MUST清除inherited Git repository selectors，MUST NOT使用sandbox bypass或把Git write directories交給planner、verify或review。Symlink、detached HEAD、invalid或escape metadata MUST拒絕required-commit launch。Manager MUST把pattern/path/hash/authority/content locator保存為Job input snapshot，terminalize時 MUST重驗檔案hash；dispatch exception MUST恢復`needs_human` stop facet。

#### Scenario: Legacy run缺brainstorm發布artifact authority
- **WHEN**同一active run已保存canonical brainstorm evidence，但planning authority只含brainstorm前artifact
- **THEN**explicit resume先驗evidence scope/hash與workspace exact artifact hashes，再原子補齊同一run authority
- **THEN**Manager從evidence採用並固化brainstorm發證source revision；後續PR refresh改變run目前source revision仍可重驗同一evidence
- **THEN**evidence缺失、漂移或artifact不在planner outputs時保留`needs_human`且不得launch

#### Scenario: Builder worktree缺accepted plan
- **WHEN**pending build card宣告accepted plan但獨立builder worktree尚無該檔
- **THEN**Manager只從相同run的planning authority驗hash後原子seed
- **THEN**Job、prompt與canonical evidence保存相同input snapshot

#### Scenario: Linked worktree builder需要建立required commit
- **WHEN**Codex builder的`.git`指向primary workspace外的linked-worktree metadata
- **THEN**Manager只對`commit_policy=required` builder要求明確commit capability；launcher清除inherited Git repository selectors，只額外開放Git驗證出的current-worktree gitdir、shared objects、current-branch ref/reflog parent directories，並保留`workspace-write` sandbox
- **THEN**planner、verify與review不取得這些Git directories；metadata不可信或required-commit worktree為detached HEAD時不得launch

#### Scenario: Operator artifact在accept後漂移
- **WHEN**operator checkout中的planning artifact hash不再等於run authority
- **THEN**Manager在建立Job或launch前拒絕dispatch
- **THEN**不得以其他同glob檔案補位

#### Scenario: Plan/build workflow card明示needs human後由operator重試
- **WHEN**plan/build workflow card的headless process exit 0但terminal schema與run/card binding正確且status為`failed`或`needs_human`
- **THEN**Manager MUST保留舊Job/log且MUST NOT建立passed evidence，periodic MUST維持`needs_human` stop
- **WHEN**operator透過control queue明確resume同一run
- **THEN**Manager MAY重派同一card；malformed或錯誤binding terminal MUST NOT取得此retry authority

#### Scenario: 升級前planning-only Agy被誤派為reviewer
- **GIVEN**latest verify/review Job精確綁定canonical Agy planning identity、current run/claim/repo/source/card/phase/Candidate與manifest outputs，且以generic v1 `passed` terminal退出但沒有canonical evidence
- **WHEN**operator透過control queue明確resume同一run
- **THEN**Manager MAY保留舊Job/log並以具`review` capability的foreign identity重派一次；舊payload MUST NOT被terminalize，periodic與一般retry MUST NOT重派
- **AND**若terminal含prose、第二個fence、錯誤binding或outputs不完整，Manager MUST維持`needs_human`
- **AND**migration classifier MUST同時綁定Job kind、current step outputs與安全worktree/repo/input root；一般dispatch API MUST NOT接受此recovery authority

#### Scenario: Sequential build card推進Candidate
- **WHEN**build card提交的新exact worktree HEAD等於或為目前Candidate的descendant
- **THEN**Manager MUST接受該card並將WorkflowRun Candidate原子推進至新HEAD
- **WHEN**build terminal Candidate不是目前Candidate的descendant，或verify/review Candidate不完全等於凍結Candidate
- **THEN**Manager MUST拒絕該evidence且MUST NOT推進workflow

### Requirement: Headless card prompt必須是bounded execution envelope
每張headless card prompt MUST為versioned structured envelope，至少包含run/work/source revision、phase/card/persona、skill ref、task action、commit/test policy、resolved source material、declared outputs、candidate semantics與exact terminal JSON schema。Source material總量 MUST有上限；超限 MUST fail-closed。Manager已provision worktree時，`worktree-isolation` MUST明示不得建立第二個worktree。

#### Scenario: Legacy pending build card沒有直接inputs
- **WHEN**active v1 manifest的`tdd-red`或`subagent-build`沒有直接inputs，但同phase較早card宣告accepted plan
- **THEN**Manager繼承同phase input contract並建立snapshot
- **THEN**不得把舊passed card靜默改寫成新gate已通過

### Requirement: Interrupted headless job只能由operator恢復
Dead PID且沒有exit sentinel的dispatched job MUST先由Dispatcher保存為failed並將WorkflowRun設為`needs_human`。Periodic runner MUST只reconcile，MUST NOT清除`needs_human`或自動重派；只有經control queue的explicit `work resume`或`workflow resume` MAY清除facet並重試同run/card，舊failed job MUST保留。

#### Scenario: Periodic runner連跑兩輪
- **WHEN**第一輪發現dead PID/no sentinel並將run設為needs_human
- **THEN**第二輪回`operator-resume-required`
- **THEN**不得建立新job
