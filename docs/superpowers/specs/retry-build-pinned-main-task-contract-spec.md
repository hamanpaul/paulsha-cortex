---
status: accepted
work_item: retry-build-pinned-main-task-contract
---

# Builder 固定 C/M task contract 規格（#973 B1c）

## Requirements

本票是 B1 aggregate 的 task-construction implementation child。依賴 #972 typed tuple 與 B1a exact-M source pin naming/identity contract；**不依賴 B1b private ref 已 provision**。僅改 `work_actions.py` 共用 task helper，供 operator retry-build 與 B2 Manager automatic caller 使用。

### R1 Pre-reset tuple validation
在 reset/dispatch 前，只驗同一 WorkflowRun 的 typed C/M、repair kind、完整 conflict paths、Candidate CAS `C`、SHA 格式與 B1a 已確認的 Manager source M pin identity。確定將使用的 Builder private ref deterministic name/token，但不得要求該 per-job clone ref 已存在；B1b provisioning 發生在 reset 後、Builder launch 前。任何缺欄、格式錯、不同 run、C CAS 不符或 Manager source pin mismatch 都先拒絕；禁止讀浮動 `origin/main`。

### R2 Bind exact task input and reservation
產生唯一 bounded Builder task input，明確傳 run/C/M、repair kind、完整 conflict paths、預期 private ref name/token，以及 automatic path 的 B0 deterministic reservation id。Manual path 不偽造 reservation/adjudication。兩個入口共用同一純 task builder。此處宣告預期 ref 不代表 Builder clone 已含 M，也不代表 merge 已發生。

### R3 Post-reset provisioning gate
B1b 在 isolated Builder clone provision 後、Builder launch 前，把 Manager exact-M source pin explicit fetch 到上述 private ref，並驗證此 ref 的 object SHA 精確等於 M、HEAD/feature/base 仍 C。缺 ref、錯 SHA、token mismatch 或 fetch failure 必須禁止 Builder launch。Automatic caller B2 以保留的 B0 typed snapshot 呼叫 #990 durable stop；reservation 已 commit 就不退款。Manual caller 保持 #989 operator recovery authority。

### R4 Require real merge; preserve CHANGELOG
Task 要求 Builder 從 C merge預期 private pinned-M ref，建立真 non-fast-forward D，parents `[C,M]`。只允 clean-behind 和唯一 CHANGELOG `[Unreleased]` 頂端插入 conflict 自動修；完整保留 C/M entries 各一次、Candidate 在前，保留其他無衝突 M 變更；其他衝突完整列出並停。

### R5 No proof/adoption or delivery here
本票只 author task input。B3 owns true Git object proof/quarantine/adoption; B4 owns atomic ref CAS; C owns D gates/ship. 不採信 D、harvest、push 或聲稱 prompt 已證明 merge。

## Verification

驗證 pre-reset helper 可在 deterministic private ref 尚不存在時成功生成 exact C/M task；缺 B1a source pin、錯 C/M/run/ref name 則在 reset 前拒絕。另測 B1b post-reset import success、missing/wrong ref/import failure 均不 launch Builder，auto path由 B2使用 B0 context 呼叫 #990，reservation remains consumed。驗證 manual/automatic task equality、no floating main lookup、bounded classification。真 bare-Git merge/CHANGELOG tests 由 B3 作為驗收；本票不以 prompt text 冒充真 Git proof。

## Boundary and sizing

Production only `coordinator/work_actions.py`; B1b 的 clone provision seam 在 `seams.py`。domain=0,state=1；complete accepted fix-standard artifacts yield **5 / Yellow**.
