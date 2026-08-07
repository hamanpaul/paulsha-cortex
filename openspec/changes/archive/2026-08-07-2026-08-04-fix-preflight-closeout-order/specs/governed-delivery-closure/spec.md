---
status: accepted
work_item: fix-preflight-closeout-order
---

## ADDED Requirements

### Requirement: 本地 deterministic closeout 必須先於且獨立於 PR metadata preflight

ship validator MUST 以固定順序執行三段：local closeout、review attestation 確認、external ship mutation。local closeout（canonical untracked report 清理、archive gate 檢核、官方 `openspec archive`、archive commit 與 candidate reset）MUST 在 run 無 PR binding（`pr_refs` 為空）且無 PR metadata 的情況下即可完成，MUST NOT 以 `pr_number` 或 PR metadata 建構成功為前提。PR metadata 建構或 preflight 失敗 MUST NOT 使已完成的 local closeout 結果失效或不可恢復。

#### Scenario: pre-PR run 完成本地 archive closeout

- **WHEN** run 為 review-complete、`pr_refs` 為空且 builder worktree 內 active OpenSpec change 目錄存在，Manager 執行 ship validate
- **THEN** 官方 archive 與 archive commit 完成、candidate reset 回 verify 重新把關
- **THEN** 全程未執行任何 `gh` 或 `git push` 呼叫

#### Scenario: PR metadata preflight 失敗不阻塞本地 closeout

- **WHEN** 本地 archive closeout 已完成，PR metadata preflight 失敗
- **THEN** closeout 結果原樣保留，run 停在明確的 PR-binding 邊界且可直接 resume 重試

### Requirement: PR metadata preflight 只在 PR-specific transition 執行且失敗可恢復

PR metadata preflight 的 metadata 模式 MUST 只在即將建立 PR 的 transition 執行；`--pr` 模式 MUST 只在既有 PR 需要 push／merge 的 transition 執行；其餘階段 MUST NOT 執行 PR metadata preflight。preflight 失敗 MUST 產生 typed、可恢復的停止（trusted `needs_human` 結果，reason 標示 `pr-preflight-blocked` 並綁定 exact candidate 與 preflight evidence）；resume 對此類停止 MUST NOT 設 `gate_status="failed"` 死巷，status MUST 顯示下一個合法 operator action。非 typed 的意外例外 MUST 維持既有 fail-closed 語意。

#### Scenario: preflight 失敗後 resume 可續

- **WHEN** PR metadata preflight 失敗產生 typed stop，operator 再次 resume
- **THEN** run 直接重試 preflight，不需 registry surgery，`gate_status` 未曾落入 `"failed"`

#### Scenario: 意外例外仍 fail-closed

- **WHEN** ship validate 拋出非 typed 的意外例外
- **THEN** run 設 `needs_human` 且 `gate_status="failed"`，維持既有 fail-closed 行為

### Requirement: 無 GitHub authorization 時零 external mutation

local closeout 段 MUST NOT 執行任何 GitHub 或 remote mutation：archive commit MUST NOT 內嵌 push。push、PR 建立、PR metadata 寫入、copilot request 與 merge MUST 全部留在 external ship 段並沿用既有 operator authorization 模型；lifecycle 順序調整 MUST NOT 導致自動開 PR、push 或 merge。

#### Scenario: archive commit 不觸發 push

- **WHEN** local closeout 完成 archive commit 產生新 candidate
- **THEN** 未執行任何 push；新 candidate 的 push 由 external ship 段在取得授權後承擔

#### Scenario: 無授權時停在邊界

- **WHEN** run 已完成本地 closeout 但 operator 尚未授權 external ship
- **THEN** 零 external mutation，status 顯示下一個合法 operator action

### Requirement: review workspace 必須 materialize exact frozen artifacts 並以 hash attestation fail-closed

reviewer dispatch 前，Manager MUST materialize 全部 frozen planning authority artifacts 到 review workspace，並以 workspace 實際檔案內容重算 sha256 與 frozen baseline 完全一致。materialization 紀錄 MUST 含相對路徑、content sha256、source revision 與 candidate SHA；缺任一項 MUST NOT dispatch reviewer。slice-based foreign review worktree 與 workflow reviewer sandbox 兩條路徑 MUST 適用同一機制，MUST NOT 只依 prompt 宣稱。reviewer verdict MUST 回填其實際讀取的 authority hashes；缺漏、ref 集不一致或 hash drift MUST fail closed，MUST NOT 接受 PASS。

#### Scenario: frozen artifact hash drift

- **WHEN** materialize 後 workspace 內某 authority 檔內容與 frozen baseline sha256 不符
- **THEN** hash 驗證 fail closed，reviewer 不被 dispatch

#### Scenario: verdict 回填不符

- **WHEN** reviewer verdict 的 authority hashes 與 frozen baseline 不一致或缺漏
- **THEN** verdict 被拒絕，MUST NOT 記為 PASS

### Requirement: untracked overlay 不得成為隱形 authority

materialize 到 review workspace 的 seeds MUST 全部由 Manager 以 atomic、不可覆寫方式寫入，且逐檔在 input snapshot 與 evidence 紀錄可稽核；紀錄之外的 untracked planning／report 檔 MUST NOT 被當作 review 或 closeout 的輸入 authority。

#### Scenario: 非紀錄內 untracked 檔不得為 authority

- **WHEN** review workspace 存在不在 materialization 紀錄內的 untracked planning 檔
- **THEN** 該檔不得作為 review 輸入 authority，驗證只承認紀錄內且 hash 相符的 artifacts
