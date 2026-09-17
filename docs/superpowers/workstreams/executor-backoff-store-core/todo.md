---
status: accepted
work_item: executor-backoff-store-core
domain_breadth: 0
state_consistency: 2
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
---

# Executor backoff store core child A

## Boundary

- Parent：`executor-durable-backoff`／#825；本件只實作新 `paulsha_cortex/coordinator/executor_backoff.py`。母 [spec](../../specs/executor-durable-backoff-spec.md)、[design](../../specs/executor-durable-backoff-design.md)、[todo](../executor-durable-backoff/todo.md) 的其他 AC 保留。
- 同 work_item [spec](../../specs/executor-backoff-store-core-spec.md)／[design](../../specs/executor-backoff-store-core-design.md) 為八組 AC 與 T01–T16 oracle，含 review R1 新增的 bounded capacity/decode/fold。C/D terminal provenance、strict reader、retention、production reconciliation 尚未接線；store component green 不等母安全層啟用。
- 本 child owner 為 [#850](https://github.com/hamanpaul/paulsha-cortex/issues/850)。下列 checkbox 追蹤 child A 的 pre-archive build 工作；archive／merge／issue closure／installed qualification 仍由後續 phase 處理，不讀寫 live state／呼叫模型。

## Tasks

- [x] **Intake source/tests/documentation**：逐項讀回父與 child 三件組，核對新 store 單模組、八組 AC、T01–T16；沿 accepted boundary 實作 child A，不把本件宣稱成 #825 或 live admission 完成。
- [x] **source/tests/input contract**：在唯一新模組固定 executor/model identity、terminal key、event_epoch，以及 caller-supplied authority／raw payload／payload_fingerprint／evidence_ref／parsed reset parser metadata 與 reset_provenance；persisted policy envelope 固定 rate/quota base、multiplier、max exponent 與 margin。缺 identity／非法 authority／不支援 outcome envelope／cyclic payload／stale reset／衝突 event 一律 unknown，不從 registry/settings/arrival now 代填。
- [x] **source/tests/store schema**：實作含 `scope` 的 `executor-backoff/v1` strict reader、fresh disk query、可恢復時附 machine-readable last-known floor 與明確 diagnostics；真 missing 只表示本機無已知 backoff。補 cross-root scope mismatch、unknown schema/read/permission faults，拒 corrupt→empty。
- [x] **source/tests/bounded resource profile**：依 design D2.1 固定 `bounded-ledger/v1` 上限，先 bounded read／lexical count 再 decode；replace 前完整容量判斷，超限整批 unknown 且原 bytes/acks 不變。補 at-cap duplicate unchanged、新 event 拒收、過深/超大/計算預算中止與成長 fd 邊界。名目 1024 events／2 MiB／1 MiB 與 1 秒 compute deadline 為並列限制；host/load 先撞到 compute budget 時照樣 fail-closed。
- [x] **source/tests/process atomicity**：穩定 `flock`、鎖內 fresh RMW、唯一 temp、sync/replace/durability barrier、finally cleanup；補交錯 writer、lock timeout、replace 前後 fault 與 K0/K1/K2 真 kill 的 process-boundary tests。durability 無法確認時回 unknown，不假裝 rollback。
- [x] **source/tests/immutable ledger and ack**：same-key payload/time/policy/identity 必須一致，成功 commit 才落 ack；完整 replay 不重寫 bytes/hits/deadline。跨 process/fresh instance 與同 key 衝突負例已覆蓋，未改 provider_outcome 既有 keys。
- [x] **source/tests/event-time fold**：依 design D4 由完整事件穩定排序重算 episode/hits/max deadline；覆蓋 reset1000→舊 reset300、反向 permutation、same-time tie、deadline 相等新 episode、quota base 與 frozen policy replay，不靠 arrival-time 假綠。
- [x] **source/tests/expiry and no early clear**：expiry/clear/restart 保留 events/ack/policy 且不釋放 ledger 額度，舊 replay 不復活；active clear 以 `active-cooldown` 拒絕，容量/計算/磁碟 fault 均不丟已接收事件。
- [x] **source/tests/reconciliation seam**：public status 保留 raw store observation 與 reconciliation 分離；新增 supplied inventory seam，可在 fixture caller inventory 下回 `pending`／`complete`／`unknown`、列出 missing/conflicting terminal keys 與 evidence ref。無 supplied inventory 時維持 `unverified`；replace/read/durability faults 仍 fail-closed，不用 always-complete fake 背書 production C/D。production caller inventory sourcing／lane 接線仍由 #825 後續 child 承接。
- [x] **tests/negative controls**：`tests/test_executor_backoff.py` 現已覆蓋 strict reader、bounded resource、duplicate-at-cap、lock timeout、process kill、expired replay、unknown→missing/TTL forgetfulness 等 mutant RED oracle；所有 wait 有界、finally 放行，無真 provider/模型/daemon。
- [x] **tests/compatibility and CI**：已跑 `python -m pytest tests/test_executor_backoff.py tests/test_provider_backoff.py tests/test_provider_outcome.py -q` 與 `python -m pytest -q`，保留既有 GitHub backoff／provider outcome 斷言；Manager gate 環境的 exact pytest 由後續 phase獨立重跑採信。
- [x] **documentation/CLI help and tests**：文件明列 raw missing/valid 不等 admission/餘量足夠、C/D 尚缺與 API/lock/retention residual；真跑 `cortex status --help`、`cortex stat --help`、`cortex tick --help`、`cortex dispatch --help` 相容，不新增 inspect/clear/override CLI。
- [x] **documentation/changelog and policy**：已更新 `changelog.d/executor-backoff-store-core.md` 與 `CHANGELOG.md [Unreleased]`，並完成 local `git diff --check` hygiene；PR-context `policy_check`、symlink attestation 與 merge policy gate 由後續 phase 在對應環境採信，不改 VERSION。
- [x] **tests/planning completeness and sizing**：沿用 accepted authoring evidence，child A 維持單模組 domain=0／state=2／invariants=8 的可派邊界；本次 build work 未改既有 sizing/boundary 結論，只把對應 spec/design/todo/OpenSpec tracking 與實作同步，不把 child A 宣稱成 #825 完成。
- [x] **documentation/review and delivery evidence**：本次 pre-archive build 已完成 focused/full pytest、CLI help smoke，且 builder 流程已跑 strict spec review 與 code-quality review（workflow/session evidence，不另寫入 repo artifact）；archive／merge／issue closure／installed qualification 與 production lane evidence 仍由後續 phase處理，A 不執行 live admission rollout，也不關閉 #825/R05/R08/R09。
