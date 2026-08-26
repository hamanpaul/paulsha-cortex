## ADDED Requirements

### Requirement: Phase 2 closeout MUST reconcile shipped tree with retained candidates

系統 MUST 以 latest default-branch tree 與 #789/#790/#791 exact final heads 做
artifact-level provenance 比對；被宣稱 superseded 的能力若未存在於 shipped tree，MUST
經 RED regression 與 governed semantic transplant 補齊，不得只靠 PR comment、release
名稱或相鄰功能推論已整合。

#### Scenario: superseded comment 與 tree 不一致

- **WHEN** retained candidate 含具名 production module/spec/test，而 released tree 缺少該產物
- **THEN** closeout MUST 視為未完成並建立可追溯整併
- **THEN** issue/Todo MUST NOT 在對應 code、review、CI 與 release evidence 前標為完成

### Requirement: Recovered trust-root capabilities MUST retain their fail-closed contracts

Recovered tree MUST 同時提供：Copilot exact pinned payload/wrapper；完整 generated-vs-installed
asset inventory；production-shaped agent-loop qualification seam。PATH/HOME shadow、symlink/path
escape、functional drift、credential-byte exposure、scripted-probe bypass、SKIP/fallback/quota/model
mismatch MUST 導致對應 gate 失敗。

#### Scenario: one recovered capability is absent or degraded

- **WHEN** 任一 focused regression 發現 wrapper identity、inventory coverage 或 agent-loop evidence
  不符合 exact contract
- **THEN** 整體 Phase 2 closeout MUST fail closed
- **THEN** 其他兩項能力的綠燈不得作為 waiver

### Requirement: Package release and live deployment canary MUST remain distinct

Recovered code 的 GitHub Release MUST 由 deterministic、credential-free、exact-SHA release-profile
qualification 解鎖；provider/model/Manager GitHub/full intake-to-closeout MUST 只由獨立
deployment-canary profile 驗證。兩種 evidence 不得互換。

#### Scenario: source is released but no live canary exists

- **WHEN** exact-main RC 與 GitHub Release 成功，但沒有同 SHA deployment canary
- **THEN** Phase 2 source/package MAY 標為 shipped
- **THEN** 當下 production/provider 健康 MUST 保持未證實，且不得寫成 release blocker 已由 canary 證實

### Requirement: Closeout release MUST be immutable and exact-bound

Recovered code 合併後 MUST 使用新 patch version，建立指向 exact default-branch commit 的
annotated tag、non-draft/non-prerelease GitHub Release 與唯一 wheel asset；下載 wheel hash MUST
與同 commit release-profile RC evidence 完全一致。

#### Scenario: recovered code is merged after v0.1.9

- **WHEN** closeout code 不存在於 immutable `v0.1.9` tree
- **THEN** 系統 MUST NOT 改寫 `v0.1.9`
- **THEN** 必須以新 patch release 承載 recovered code 後才可宣稱該 code 已發布
