## ADDED Requirements

### Requirement: Ship前必須官方archive並建立完整PR metadata
Manager MUST使用官方`openspec archive -y <change>`，並確認tasks全勾、canonical specs合法、doc references同步與changelog fragment存在。PR title/body/labels MUST先完整擬定為zh-TW；body引用mapped issue MUST使用closing keyword。Local手工搬移change MUST NOT視為archive gate通過。

#### Scenario: OpenSpec tasks未完成
- **WHEN**change tasks仍有未勾項目
- **THEN**Manager停止ship並設needs_human或回到相應phase
- **THEN**不得開PR或merge

### Requirement: Preflight必須綁定PR context與exact tree
Manager MUST先跑`python3 -m policy_check --repo .`，再執行configured `PSC_PREFLIGHT_CMD`。首次使用draft PR metadata，既有PR修正 MUST帶`--pr N`。Preflight MUST涵蓋pytest、`openspec validate --all`與PR-context policy；push前與merge前 MUST重跑。只有近期full-suite evidence tree hash等於current tree時 MAY使用`--skip-tests`。

#### Scenario: Full suite evidence來自舊tree
- **WHEN**current tree hash與evidence tree hash不同
- **THEN**Manager拒絕`--skip-tests`並重跑full suite

#### Scenario: 既有PR仍停在修復前HEAD
- **WHEN**WorkflowRun已綁定既有PR，但fresh verify/review通過的exact Candidate尚未出現在該PR branch
- **THEN**Manager先以`--pr N`對乾淨exact-Candidate checkout完成preflight，再冪等push授權的`feature/*` ref並重讀remote HEAD
- **THEN**remote HEAD未精確等於Candidate時不得進入current-HEAD delivery gate

### Requirement: Delivery review與GitHub gates必須綁定current HEAD
開PR後 Manager MUST等待所有required checks terminal-green，並要求恰好一種current-HEAD delivery review authority：request `@copilot`所得的authenticated review，或經control queue建立的typed `maintainer-review` evidence。有效Copilot review MUST非error且`commit_id`等於current HEAD；maintainer evidence MUST immutable且精確綁定repo/work/run/authority digest/PR/current HEAD/actor/verdict。所有current findings threads MUST resolved或outdated。每次push MUST使舊review evidence失效。兩種authority MUST保留實際kind/ref/hash，MUST NOT把maintainer evidence偽裝成Copilot；merge authorization與CompletionRecord MUST保存同一種實際delivery review kind，並持續要求恰好一種。Delivery review MUST NOT取代ForeignReview。Manager讀取checks、statuses與reviews的REST pagination MUST使用目前支援的typed shell-free gh argv，MUST NOT要求本機gh未提供的`--slurp`；空輸出或任一malformed page MUST fail-closed。

#### Scenario: Push後只有舊HEAD review
- **WHEN**PR HEAD改變且最新Copilot review仍綁前一commit
- **THEN**Manager重新request review並阻止merge

#### Scenario: Check cancelled或thread unresolved
- **WHEN**任一required check failed/cancelled/pending，或current thread unresolved
- **THEN**Manager不得merge

#### Scenario: Paginated delivery facts含malformed page
- **WHEN**authenticated gh pagination回傳空stream或任一頁不是合法JSON
- **THEN**Manager停止current-HEAD gate並不得merge

#### Scenario: Maintainer attest綁定舊HEAD
- **WHEN**PR HEAD已從A推進到B，但maintainer evidence綁定A
- **THEN**Manager拒絕merge authorization並要求B的新attestation
- **THEN**不得改寫evidence或把它記成Copilot review

#### Scenario: Copilot stop切換為typed maintainer authority
- **GIVEN**delivery journal停在`copilot-*` needs-human reason，且WorkflowRun綁定一份exact-HEAD maintainer-review evidence
- **WHEN**ship validator提供該evidence完整的path/hash pair
- **THEN**Manager MAY重入同一delivery transaction，並在merge authorization前重驗immutable evidence
- **AND**external merge、target cardinality、未知stop reason、不完整path/hash、stale Candidate或binding mismatch MUST維持fail-closed

#### Scenario: Maintainer-authorized merge建立CompletionRecord
- **GIVEN**merge authorization保存的current-HEAD delivery review kind為`maintainer-review`
- **WHEN**Manager驗證remote closure並建立CompletionRecord
- **THEN**trusted evidence MUST保留`maintainer-review`的實際ref/hash，並拒絕缺少delivery review或同時宣告Copilot與maintainer authority

### Requirement: Finding處理必須bounded且不得偷換reviewer
真finding MUST由Builder修正並加regression test；誤報 MUST由Reviewer留下evidence後resolve。每個HEAD最長等待15分鐘，最多兩輪fix/re-review。Timeout或第三輪仍有finding MUST設`needs_human`，不得替換reviewer或繞過gate。

#### Scenario: 第三輪仍有finding
- **WHEN**兩輪fix/re-review後current HEAD仍有finding
- **THEN**Manager停止automation並列出evidence與next actions
- **THEN**不得merge或選另一reviewer清除budget

### Requirement: Merge前後必須重讀remote terminal facts
Merge前 Manager MUST重新讀HEAD、mergeability、checks、threads、closing issues與archive diff，revision race MUST中止merge。Merge MUST使用`gh pr merge --merge`且 MUST NOT使用GitHub `--auto`。Merge後 MUST fetch default branch並驗merge ancestry、all mapped issues closed、active OpenSpec消失、remote archive存在、Todo tasks完成，才可寫versioned CompletionRecord並投影done。

#### Scenario: Merge後active planning path已移入official archive
- **GIVEN**Manager delivery journal已以exact Candidate與merge authorization記錄`merged`
- **AND**accepted OpenSpec planning paths已由同一workflow的official archive移出active change目錄
- **WHEN**operator resume觸發remote closure reconciliation
- **THEN**Manager MUST直接重驗merged journal與remote closure，不得因active planning path已不存在而回退到planning-authority reconciliation
- **AND**journal identity、Candidate、merge commit或authorization不完整時 MUST維持原本的fail-closed planning reconciliation與ship validator檢查

#### Scenario: Terminal facts使WorkAuthority revision前進
- **GIVEN**immutable merge authorization精確綁定merge當下的WorkAuthority digest、run、delivery targets、Candidate與tree
- **AND**PR merge後issue closed、PR closed與OpenSpec archived使current WorkAuthority digest自然前進
- **WHEN**Manager重播`merged`或cached `done` remote closure
- **THEN**authorization MUST以其immutable pre-merge digest重驗，不得要求它等於terminal WorkAuthority digest
- **AND**此例外只適用post-merge terminal reconciliation；merge-authorized與merge前所有gate仍 MUST精確等於current WorkAuthority digest，且evidence wrapper/ref/hash/review binding任一drift仍 MUST fail-closed

#### Scenario: Completion Draft在terminal authority前進後重試
- **GIVEN**Manager已為同一run與Candidate保存一份合法Completion Draft
- **WHEN**重試只改變`completed_at`，或default branch、provider、WorkAuthority與其他closure語意已前進
- **THEN**Manager MUST以排除`completed_at`的normalized closure語意hash選取immutable draft revision；相同語意 MUST沿用首份draft及其時間戳，語意前進 MUST建立新revision且保留舊draft
- **AND**既有target為symlink、malformed或同一語意key內容不符時 MUST fail-closed，不得覆寫或刪除audit evidence

#### Scenario: Per-card evidence派生run-level CompletionRecord binding
- **GIVEN**verify與ForeignReview由同一WorkflowRun的不同card dispatch，因而各自保存不同per-card slice identity
- **WHEN**Manager建立Completion Draft
- **THEN**Manager MUST先重驗每份原始canonical envelope的run、job、phase、Candidate、path與hash，再以共同WorkflowRun ID寫入closure verification與review evidence
- **AND**CompletionRecord strict reader MUST同時重驗run-level slice、Candidate、builder/reviewer job與evidence hash；原始per-card evidence不得被改寫或以未驗證payload取代

#### Scenario: Post-archive repair保留Manager ship audit
- **GIVEN**Manager已於archive Candidate完成official archive並保存passed ship evidence，後續retry-build產生其tested descendant final Candidate
- **WHEN**terminal closure重驗`openspec-archive`與`policy-commit` ship cards
- **THEN**Manager MAY接受archive job綁定final Candidate的Git ancestor，但 MUST要求registry中的archive step仍為Manager-owned passed authority、job/evidence完整且ancestry查詢成功
- **AND**`policy-commit` MUST仍精確綁定final Candidate；unrelated archive commit、ambiguous job、evidence mismatch或ancestry error MUST fail-closed

Existing PR metadata transaction的title/body PATCH、labels PUT與後續PR/issue identity reread MAY只在明確HTTP 502/503/504時以finite bounded delay重試；這些操作 MUST保持冪等且每次成功後仍完整reread。PR create、Candidate push、review request、merge與其他delivery side effect MUST NOT取得此retry authority。Auth、rate-limit、其他HTTP error或malformed response MUST立即fail-closed。

Manager MUST在existing PR metadata write前先authenticated reread PR title/body與issue labels；三者與canonical metadata全部精確一致時 MUST NOT發出PATCH或PUT。任一欄drift時才 MAY執行冪等PATCH/PUT，且成功後 MUST再次完整reread並驗exact identity。Initial reread、post-write reread或shape validation任一失敗 MUST fail-closed，MUST NOT把write omission解釋為未驗證的skip。

V1 WorkflowRun只支援唯一mapped PR、唯一OpenSpec change與唯一Todo path。任一類不是恰好一個confirmed ref時 Manager MUST設`needs_human:multiple-delivery-targets-unsupported`，MUST NOT只選其中一個完成、merge或寫CompletionRecord。若該stop發生於immutable delivery binding建立前，operator修正repo-local correlation並explicit resume後，Manager MAY在current authority已收斂為恰好一組PR/OpenSpec/Todo時重綁同一run的delivery journal並清除此特定stop；已建立binding或其他`needs_human`原因 MUST NOT由此路徑清除。Merge authorization MUST只雜湊stable semantic preflight result與immutable evidence hashes，MUST NOT納入stdout、stderr或duration；v2 authorization MUST保存實際current-HEAD review kind/ref/hash。`merge-authorized` crash recovery MUST先依durable authorization與authenticated merge status reconcile，已merge時 MUST NOT重跑preflight；既有v1 Copilot authorization只可按原schema重播，不得重新解釋成maintainer authority。

#### Scenario: Work item有多個delivery targets
- **WHEN**current WorkAuthority含多張PR、多個active OpenSpec或多個Todo path任一情形
- **THEN**V1 Manager轉needs_human且不得對單一target投影done
- **THEN**CompletionRecord不得把未驗證refs標示完成

#### Scenario: Operator補齊唯一delivery target後續作
- **WHEN**run在delivery binding建立前因缺少或具有多個target而停止，且operator修正confirmed correlation後explicit resume
- **THEN**Manager只在current authority恰為一張PR、一個active OpenSpec與一個Todo path時重綁journal並清除此target-cardinality stop
- **THEN**既有binding或其他needs_human原因仍維持fail-closed

#### Scenario: Official archive後review finding需要Candidate修正
- **WHEN**Manager-owned official archive step已通過，但fresh verification或review對archived Candidate留下blocking finding
- **THEN**operator可用exact Candidate CAS執行retry-build，Manager保留archive step authority並重開最後builder與後續verify/review gate
- **THEN**新Candidate必須單調延伸archived Candidate；若任何其他ship step已通過則拒絕retry

#### Scenario: Merge後issue仍open
- **WHEN**PR已merge但任一mapped issue仍open
- **THEN**CompletionRecord不得通過strict closure
- **THEN**WorkItem維持on-going並顯示next action

#### Scenario: Local archive未進remote default branch
- **WHEN**local worktree有archive但authenticated remote default branch沒有
- **THEN**Manager不得寫terminal CompletionRecord或投影done

### Requirement: Deployment前必須live doctor與單repo canary
`cortex doctor --probe-live` MUST檢查gh auth/permissions、auto label、preflight executable、model identities、agy headless smoke與service paths。系統 MUST先在`paulsha-cortex`用低風險docs-only issue完成異質brainstorm→build→ForeignReview→archive→preflight→typed current-HEAD maintainer review→merge commit→done canary；通過前 MUST NOT在其他repo啟用auto label。

#### Scenario: Canary任一gate失敗
- **WHEN**canary未完成strict closure或doctor任一required probe失敗
- **THEN**fleet auto rollout保持disabled
- **THEN**Monitor read-only rollout仍可繼續並顯示diagnostics

### Requirement: Interactive CLI與service必須解析相同instance runtime roots
各specific `PSC_*_ROOT` process override MUST最高優先；其次是可一次覆寫所有derived roots的process `PSC_AGENTS_ROOT`。兩者未設定時interactive command MUST依`PSC_INSTANCE`（default `cortex`）讀取installer管理的`$HOME/.agents/core/runtime/<instance>-manager.env`，並使用相同的agents/control/coordinator/specs/run/monitor/project-config roots；不存在安裝檔時才從`$HOME/.agents`推導。Bootstrap env若為symlink、malformed或root非絕對路徑 MUST fail-closed，installer也MUST拒絕覆寫symlink bootstrap。Installer MUST保存`PSC_INSTANCE`，Monitor socket client MUST使用production monitor config的socket path。

#### Scenario: 安裝beta instance後由interactive CLI操作
- **WHEN**operator設定`PSC_INSTANCE=beta`且未覆寫specific runtime root
- **THEN**CLI control queue與beta manager service使用相同control root，CLI與monitor service使用相同project config、run root與socket
- **THEN**不得掃描猜測其他instance或fallback到generic runtime root
