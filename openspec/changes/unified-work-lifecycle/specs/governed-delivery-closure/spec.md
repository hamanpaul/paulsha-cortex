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

### Requirement: Delivery review與GitHub gates必須綁定current HEAD
開PR後 Manager MUST等待所有required checks terminal-green，並要求恰好一種current-HEAD delivery review authority：request `@copilot`所得的authenticated review，或經control queue建立的typed `maintainer-review` evidence。有效Copilot review MUST非error且`commit_id`等於current HEAD；maintainer evidence MUST immutable且精確綁定repo/work/run/authority digest/PR/current HEAD/actor/verdict。所有current findings threads MUST resolved或outdated。每次push MUST使舊review evidence失效。兩種authority MUST保留實際kind/ref/hash，MUST NOT把maintainer evidence偽裝成Copilot。Delivery review MUST NOT取代ForeignReview。

#### Scenario: Push後只有舊HEAD review
- **WHEN**PR HEAD改變且最新Copilot review仍綁前一commit
- **THEN**Manager重新request review並阻止merge

#### Scenario: Check cancelled或thread unresolved
- **WHEN**任一required check failed/cancelled/pending，或current thread unresolved
- **THEN**Manager不得merge

#### Scenario: Maintainer attest綁定舊HEAD
- **WHEN**PR HEAD已從A推進到B，但maintainer evidence綁定A
- **THEN**Manager拒絕merge authorization並要求B的新attestation
- **THEN**不得改寫evidence或把它記成Copilot review

### Requirement: Finding處理必須bounded且不得偷換reviewer
真finding MUST由Builder修正並加regression test；誤報 MUST由Reviewer留下evidence後resolve。每個HEAD最長等待15分鐘，最多兩輪fix/re-review。Timeout或第三輪仍有finding MUST設`needs_human`，不得替換reviewer或繞過gate。

#### Scenario: 第三輪仍有finding
- **WHEN**兩輪fix/re-review後current HEAD仍有finding
- **THEN**Manager停止automation並列出evidence與next actions
- **THEN**不得merge或選另一reviewer清除budget

### Requirement: Merge前後必須重讀remote terminal facts
Merge前 Manager MUST重新讀HEAD、mergeability、checks、threads、closing issues與archive diff，revision race MUST中止merge。Merge MUST使用`gh pr merge --merge`且 MUST NOT使用GitHub `--auto`。Merge後 MUST fetch default branch並驗merge ancestry、all mapped issues closed、active OpenSpec消失、remote archive存在、Todo tasks完成，才可寫versioned CompletionRecord並投影done。

V1 WorkflowRun只支援唯一mapped PR、唯一OpenSpec change與唯一Todo path。任一類有多個confirmed refs時 Manager MUST設`needs_human:multiple-delivery-targets-unsupported`，MUST NOT只選其中一個完成、merge或寫CompletionRecord。Merge authorization MUST只雜湊stable semantic preflight result與immutable evidence hashes，MUST NOT納入stdout、stderr或duration；v2 authorization MUST保存實際current-HEAD review kind/ref/hash。`merge-authorized` crash recovery MUST先依durable authorization與authenticated merge status reconcile，已merge時 MUST NOT重跑preflight；既有v1 Copilot authorization只可按原schema重播，不得重新解釋成maintainer authority。

#### Scenario: Work item有多個delivery targets
- **WHEN**current WorkAuthority含多張PR、多個active OpenSpec或多個Todo path任一情形
- **THEN**V1 Manager轉needs_human且不得對單一target投影done
- **THEN**CompletionRecord不得把未驗證refs標示完成

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
