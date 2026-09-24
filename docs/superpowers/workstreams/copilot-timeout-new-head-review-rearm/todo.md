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
- 本次只做 planning 草案；未讀寫 Cortex state、未觸發 resume、retry、review request 或 GitHub mutation。

## Sizing and gate status

Task type `fix` 使用 repo `fix-standard` combo。Plan 宣告 `domain_breadth=0`（一個 production module）、`state_consistency=1`（Manager 由單一 delivery journal run row 寫入許可與 append-only epoch history，並以 run／舊 ship hash／新 candidate／authority／binding 比對）；`invariant_count=8` 是下列可驗收性質的數量，`artifact_classes` 為 source/tests/documentation。

Sizing 由 repo helper `current_sizing_snapshot()` 對此 accepted 三件組與 packaged `fix-standard` 重算，回傳 `(5, yellow)`；完整維度為 `domain_breadth/state_consistency/acceptance_surfaces/spec_stability/orchestration = 0/1/2/0/2`。輸入為 9 cards、9 persona bindings、2 gate-spine 項及 R-09/R-16/R-19 三條規則；三件完整且無 missing kind。若產品實際選到不同 combo，派工前重算，不沿用此投影。

## Tasks

- [ ] **T1 tests／RED**：新增 `tests/test_copilot_timeout_new_head_rearm_1021.py`，使用現有 fake ship／Manager work action harness。覆蓋：(1) 只有明示 resume 可建立一次性許可；(2) 舊 stop A、新 candidate/verified/PR HEAD B 且 tree/authority/binding 全一致才前進；(3) 請求前已存在有效 B review 時採信，不再 request；(4) 無有效 B review 時 request 一次、產生 B epoch；(5) 同 B stop／重送 resume 不再 request；(6) A 的 review、error／非 Copilot／非支援 state review 不授權 B；(7) authority／binding／tree／PR HEAD race、未解 current thread、checks 或 mergeability 不符皆 fail closed；(8) 在 `review-requesting` 持久化後、API 前中止，以及 API 後、response 持久化前中止，兩者重播僅採信有效 B review，否則記 outcome-unknown 且絕不重送；(9) malformed／衝突 append-only history 保持 fail closed。另斷言不會寫入 maintainer attestation 或 merge authorization 來代替 review。
- [ ] **T2 source／Manager explicit-resume permit**：在 `_claim_action` 唯一 canonical ongoing run 且 journal 精確保存 `needs_human / copilot-review-timeout` 時，以 run id、old stop hash、current candidate、WorkAuthority digest、binding hash、明示 resume actor 與 Manager transition id 建立 Manager-owned permit；相同 tuple 的重複 resume 冪等，歧義／可觀測漂移 fail closed。不得接受 caller 指定的 candidate/head/review 作授權。
- [ ] **T3 source／epoch transition and history**：在 `_ship_action` 僅針對 old stop head != current exact verified candidate 延後 reason guard；重讀 preflight、PR HEAD、tree 與 binding 後，在 Manager 單 writer 序列化範圍內比較 state 再消費 permit。先 append 舊 stop/request/review snapshot；新 epoch 記 actor／transition id 與 candidate binding。復用 #948 review adoption；否則持久化 `review-requesting` 後只 request 一次。same-head stop 保留原 stop、不開新 epoch。
- [ ] **T4 source／fail-closed retry behavior**：守住 request crash／race，未知結果轉 `copilot-review-request-outcome-unknown` 而不重送；所有舊 HEAD review、authority drift、PR/thread/check/mergeability 失敗不得取得新 review authority 或 merge authorization。未解 current threads 照既有 gate 處理。
- [ ] **T5 regression／integration**：相關 `tests/test_work_actions.py`、`tests/test_copilot_review_adopt_existing.py`、`tests/test_delivery_orchestrator.py` 與 production ship validator/work action wiring 測試通過；保留既有 review-age 判定及 #948 behavior。加入 manager queue → resume → ship 的單 writer 條件比較測試及重複／可觀測 state replay 漂移負例；不能把 atomic replace 測成跨 writer CAS。
- [ ] **T6 docs／policy／delivery**：更新 `docs/unified-work-lifecycle.md`，說清楚舊 HEAD timeout 只有明示 resume 且新 candidate 經 exact read-back 才建立新 review epoch，same-head stop 不重送，未知 request 結果 fail closed；新增 `changelog.d/copilot-timeout-new-head-review-rearm.md` 並同步 `CHANGELOG.md [Unreleased]`。無 CLI 變更；完成 repo 要求的 tests、`git diff --check` 及帶 PR 上下文的 policy check。CI、PR review、merge、installed runtime evidence 分開記錄。

## Invariants counted

1. Explicit resume is the only permit source.
2. Exact run, authority, candidate, tree and delivery binding form the comparison tuple.
3. Only old-stop HEAD → different verified new HEAD can rearm.
4. Old stop/review epoch history is append-only and remains readable.
5. New-head review is adopted/requested once; old-head review is never carried forward.
6. Same-head stop and repeated resume do not create unbounded requests.
7. HEAD/authority/thread/check/mergeability races and malformed identity fail closed.
8. No agent self-attestation or automatic maintainer authority substitutes for Copilot review.
