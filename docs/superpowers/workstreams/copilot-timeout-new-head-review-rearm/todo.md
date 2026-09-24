---
status: accepted
work_item: copilot-timeout-new-head-review-rearm
domain_breadth: 0
state_consistency: 1
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
---

# 新 candidate exact-head review 的 timeout stop 恢復（#1021）

## Boundary

- Issue：[hamanpaul/paulsha-cortex#1021](https://github.com/hamanpaul/paulsha-cortex/issues/1021)；對應 [spec](../../specs/copilot-timeout-new-head-review-rearm-spec.md)、[design](../../specs/copilot-timeout-new-head-review-rearm-design.md)。
- 唯一預期 production module：`paulsha_cortex/coordinator/work_actions.py`；其中 `_claim_action` 接收 Manager queued explicit resume，`_ship_action` 驗證／消費 rearm 許可、保留 epoch 並依既有 gate 推進。action 種類與 `requested_by` actor 可在此 module 取得；Manager 自產 transition id，不宣稱持有 queue `req_id`。若實作證據證明必須跨出此 module，先停止、記錄具體缺口並另開 issue，不以擴權或 caller payload 繞過。
- 會新增／更新測試、`docs/unified-work-lifecycle.md`、`changelog.d/copilot-timeout-new-head-review-rearm.md` 及 `CHANGELOG.md [Unreleased]`；不新增 CLI。
- 不改 `manager.py`、`work_bridge.py`、`registry.py`、control schema、WorkflowRun schema、deck、model identity、review-attest、merge authorization、reviewer、timeout 長度或 finding budget。#1020（準時提交晚觀測）、#948（request 前既有 review）、#871（maintainer authorization）與 #935（其它 review/thread 情境）維持各自範圍；本票只沿用其已落地 gate。
- 本規劃 PR 已在 `.cortex/work-items.yaml` 新增唯一 work item `copilot-timeout-new-head-review-rearm`，與既有 `copilot-review-adopt-existing` 分開。

## 現況證據

- Live issue #1021：#862 run `workflow-9dc654fef3850cc68deb` 的 registry candidate、verified head 與 PR #954 HEAD 是 `a9cd95f7a23a378e2e058ec46993893a97bb3cd6`；journal 的 `copilot-review-timeout` stop 仍綁 `fac52740868a893b73151d8a8cdede016bcd3e24`。PR 為 OPEN/CLEAN、11 checks 綠、4 threads resolved；新 HEAD 尚無 Copilot review。
- Installed Manager service 使用 runtime pin `7fa4716b`（2026-09-23）；該 pin 的 `work_actions.py` 與目前 repo `main` byte-identical。`_ship_action` 在 needs-human reason guard 先回舊 `copilot-review-timeout`，早於 preflight、候選／PR HEAD 對齊與新 review request；一般 `resume` 只重入相同 validator，故需要此票窄化此 guard。
- 已有 `#948` exact-head review adoption：只採信同 HEAD、Copilot author、COMMENTED/APPROVED、非 error review，並走正常 finding/thread gate。此票不重寫該判準。
- 本次產品修正已落地於 `paulsha_cortex/coordinator/work_actions.py`：明示 `resume` 會為舊 HEAD timeout 建立一次性 rearm permit，`_ship_action` 只在 exact new HEAD／preflight／binding／PR facts 全數重讀相符時消費，保留舊 review epoch 歷史並以 `review-requesting`／`copilot-review-request-outcome-unknown` fail-closed request 路徑處理 crash/race。

## Sizing and gate status

Task type `fix` 使用 repo `fix-standard` combo。Plan 宣告 `domain_breadth=0`（一個 production module）、`state_consistency=1`（Manager 由單一 delivery journal run row 寫入許可與 append-only epoch history，並以 run／舊 ship hash／新 candidate／authority／binding 比對）；`invariant_count=8` 是下列可驗收性質的數量，`artifact_classes` 為 source/tests/documentation。

Sizing 由 repo helper `current_sizing_snapshot()` 對此 accepted 三件組與 packaged `fix-standard` 重算，回傳 `(5, yellow)`；完整維度為 `domain_breadth/state_consistency/acceptance_surfaces/spec_stability/orchestration = 0/1/2/0/2`。輸入為 9 cards、9 persona bindings、2 gate-spine 項及 R-09/R-16/R-19 三條規則；三件完整且無 missing kind。若產品實際選到不同 combo，派工前重算，不沿用此投影。

## Tasks

- [x] **T1 tests／RED**：新增／擴充 `tests/test_copilot_timeout_new_head_rearm_1021.py`，覆蓋 explicit resume permit、exact-head adoption/request、same-head 不重送、invalid review rejection、PR HEAD race、`review-requesting` replay outcome-unknown、request error 不重送，以及 append-only history／rearm metadata。
- [x] **T2 source／Manager explicit-resume permit**：`_claim_action` 只在唯一 canonical ongoing run 的 `needs_human / copilot-review-timeout` stop、且 current verified Candidate 與舊 stop HEAD 不同時，建立 Manager-owned permit；相同 tuple 的重複 resume 冪等，caller 額外授權欄位會被拒絕。
- [x] **T3 source／epoch transition and history**：`_ship_action` 只對舊 HEAD timeout ＋有效 permit 延後 early return，重讀 exact new HEAD／preflight／binding／PR facts 後 append `delivery_review_epochs`，保留舊 timeout/review snapshot，並把 transition id／actor／binding 摘要寫入新 epoch。
- [x] **T4 source／fail-closed retry behavior**：`review-requesting` 先 durable 再 request；API/HEAD race/crash uncertainty 轉 `copilot-review-request-outcome-unknown`，同一 HEAD replay 不重送。舊 HEAD／error／非 Copilot／非支援 state review 仍不得授權新 HEAD。
- [x] **T5 regression／integration**：`tests/test_copilot_review_adopt_existing.py`、`tests/test_ship_lane_no_openspec_911.py` 與 `tests/test_work_actions.py` 的緊鄰 ship/resume regression scope 通過，保留 #948 exact-head adoption 與既有 maintainer re-entry 行為。
- [x] **T6 documentation／docs／policy／delivery**：已更新 `docs/unified-work-lifecycle.md`、`changelog.d/copilot-timeout-new-head-review-rearm.md`、`CHANGELOG.md [Unreleased]`，且不新增 CLI。

## Invariants counted

1. Explicit resume is the only permit source.
2. Exact run, authority, candidate, tree and delivery binding form the comparison tuple.
3. Only old-stop HEAD → different verified new HEAD can rearm.
4. Old stop/review epoch history is append-only and remains readable.
5. New-head review is adopted/requested once; old-head review is never carried forward.
6. Same-head stop and repeated resume do not create unbounded requests.
7. HEAD/authority/thread/check/mergeability races and malformed identity fail closed.
8. No agent self-attestation or automatic maintainer authority substitutes for Copilot review.
