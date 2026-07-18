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

### Requirement: Copilot與GitHub gates必須綁定current HEAD
開PR後 Manager MUST等待所有required checks terminal-green並request `@copilot`。有效Copilot review MUST非error且`commit_id`等於current HEAD；所有current findings threads MUST resolved或outdated。每次push MUST建立新review epoch並重新request，舊HEAD review MUST NOT沿用。Copilot MUST NOT取代ForeignReview。

#### Scenario: Push後只有舊HEAD review
- **WHEN**PR HEAD改變且最新Copilot review仍綁前一commit
- **THEN**Manager重新request review並阻止merge

#### Scenario: Check cancelled或thread unresolved
- **WHEN**任一required check failed/cancelled/pending，或current thread unresolved
- **THEN**Manager不得merge

### Requirement: Finding處理必須bounded且不得偷換reviewer
真finding MUST由Builder修正並加regression test；誤報 MUST由Reviewer留下evidence後resolve。每個HEAD最長等待15分鐘，最多兩輪fix/re-review。Timeout或第三輪仍有finding MUST設`needs_human`，不得替換reviewer或繞過gate。

#### Scenario: 第三輪仍有finding
- **WHEN**兩輪fix/re-review後current HEAD仍有finding
- **THEN**Manager停止automation並列出evidence與next actions
- **THEN**不得merge或選另一reviewer清除budget

### Requirement: Merge前後必須重讀remote terminal facts
Merge前 Manager MUST重新讀HEAD、mergeability、checks、threads、closing issues與archive diff，revision race MUST中止merge。Merge MUST使用`gh pr merge --merge`且 MUST NOT使用GitHub `--auto`。Merge後 MUST fetch default branch並驗merge ancestry、all mapped issues closed、active OpenSpec消失、remote archive存在、Todo tasks完成，才可寫versioned CompletionRecord並投影done。

#### Scenario: Merge後issue仍open
- **WHEN**PR已merge但任一mapped issue仍open
- **THEN**CompletionRecord不得通過strict closure
- **THEN**WorkItem維持on-going並顯示next action

#### Scenario: Local archive未進remote default branch
- **WHEN**local worktree有archive但authenticated remote default branch沒有
- **THEN**Manager不得寫terminal CompletionRecord或投影done

### Requirement: Deployment前必須live doctor與單repo canary
`cortex doctor --probe-live` MUST檢查gh auth/permissions、auto label、preflight executable、model identities、agy headless smoke與service paths。系統 MUST先在`paulsha-cortex`用低風險docs-only issue完成異質brainstorm→build→ForeignReview→archive→preflight→Copilot→merge commit→done canary；通過前 MUST NOT在其他repo啟用auto label。

#### Scenario: Canary任一gate失敗
- **WHEN**canary未完成strict closure或doctor任一required probe失敗
- **THEN**fleet auto rollout保持disabled
- **THEN**Monitor read-only rollout仍可繼續並顯示diagnostics
