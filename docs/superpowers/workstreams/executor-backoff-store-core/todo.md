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
- 本 child owner 為 [#850](https://github.com/hamanpaul/paulsha-cortex/issues/850)，本批只登錄 accepted 規劃，未 freeze／dispatch。下列全部是產品工作待辦，不是此次已執行項目；不讀寫 live state／呼叫模型。

## Tasks

- [ ] **Intake source/tests/documentation**：逐項讀回父與 child 三件組，核對新 store 單模組、八組 AC、T01–T16；記 accepted source refs/hashes、R1–R9 分工、C/D 未接線與 quota residual，不自動推為 #825 完成。
- [ ] **source/tests/input contract**：在唯一新模組固定 envelope、三態 observation／獨立 reconciliation、canonical fingerprint、scope/exact identity、immutable terminal time/key、policy/reset provenance。缺/衝突 unknown，absent hint 與缺資料分開；不從 registry/settings/arrival now 代填。
- [ ] **source/tests/store schema**：實作 executor-backoff/v1 strict reader、fresh disk query、已驗 last-good 與明確 error diagnostics；真 missing 只表示本機無已知 backoff。補 T01/T10/unknown schema/read/permission faults，拒 corrupt→empty。
- [ ] **source/tests/bounded resource profile**：按 design D2.1 固定 store/input/event/identity/JSON depth-node-token/canonical bytes/sort-fold/compute deadline 界；decode/copy 前 bounded read/lexical count、replace 前完整容量判斷，超限整批 unknown 且原 bytes/acks 不變。補 T13–T16，包括 cap+1/成長 fd/過深結構、兩 process 搶最後額度、at-cap exact duplicate 仍 unchanged、衝突/新事件拒收、計算超限釋鎖；不新增 GC、不靠 disk-full 或無界 future/thread timeout 冒充資源界。
- [ ] **source/tests/process atomicity**：穩定 flock、鎖內 fresh RMW、唯一 temp、sync/replace/durability barrier、finally cleanup；補 T02/T03/T09/T12，包括 stale-reader、不同 identity、lock timeout、replace 前後 exception faults。T09 另須按 design D3.1 的 K0（replace 前）、K1（replace 成功而 dir barrier 前）、K2（barrier 後）真正 kill 僅由測試自建的隔離 writer；barrier 定位、bounded kill/join，fresh process 重讀 data＋data/directory sync＋durable fixture inventory 對帳、驗 flock 釋放與 replay 冪等。exception-only 不替代死亡測試，SIGKILL 不等斷電；無法確認 durability 不可回成功。
- [ ] **source/tests/immutable ledger and ack**：驗 same-key payload/time/policy/identity 一致，成功 commit 才 ack；完整 replay 不重寫 bytes/hits/deadline。補 T04、跨 process/fresh instance、hint/缺 identity、同 key 衝突負例；不改 provider_outcome 既有 keys。
- [ ] **source/tests/event-time fold**：依 design D4 由完整事件穩定排序重算 episode/hits/max deadline；補 T05/T06 的 reset1000→舊 reset300、反向/all permutations、same-time tie、deadline 相等、新 episode、混合 absent、不同 arrival now、原 policy replay。不得靠 arrival-time 或當前 helper/settings 取得假綠。
- [ ] **source/tests/expiry and no early clear**：補 T07/T11/T14/T15；expiry/clear/restart 仍保留 events/ack/policy 且不釋放 ledger 額度，舊 replay 不復活；未 ack 舊 event 容量內才 fold、超限保留 unknown。active clear/success/auth success/短 reset 不解封，無 TTL GC/checkpoint；容量/計算/磁碟不足均不丟已接收事件。
- [ ] **source/tests/reconciliation seam**：以可跨 process 重讀的 fixture caller inventory 驗 T08/T10：store 只有 A、B terminal 已持久、replace fault、fresh consumer 仍 unknown；可靠 merge/barrier 恢復＋fresh supplied inventory 才 complete。缺 inventory、不可讀/不完整窗口保持 unverified/unknown；不能用 always-complete fake 背書 production C/D。
- [ ] **tests/negative controls**：新增 `tests/test_executor_backoff.py`，逐一記 T01–T16 正例與 mutant RED oracle；隔離證明去掉 flock、只記最後 key、arrival-now、TTL 清 ack、unknown→missing、舊合法檔→complete、sync-error→valid、先無界 read/decode 再檢查、count==cap 拒 duplicate、容量不足刪最舊 ack、忽略 work/deadline budget 都會被抓。所有 wait 有界、finally 放行，無真 provider/模型/daemon。
- [ ] **tests/compatibility and CI**：跑 `python -m pytest tests/test_executor_backoff.py tests/test_provider_backoff.py tests/test_provider_outcome.py -q`，既有 GitHub backoff 原斷言保留；再跑 `python -m pytest tests/ -q` 與既有 CI。新 store 測試未建立時此命令不是已通過證據，環境缺口具體列出。
- [ ] **documentation/CLI help and tests**：文件明列 raw missing/valid 不等 admission/餘量足夠、C/D 尚缺、API/diagnostic/lock/retention residual。真跑候選 `cortex status --help`、`cortex stat --help`、`cortex tick --help`、`cortex dispatch --help`；最後一個是已停用舊低階入口，只驗 help 相容，不執行 dispatch。隔離 help/API smoke，不發 control request、不新增不存在的 top-level inspect 或 clear/override CLI；產品 JSON skip/unknown 真入口留 D/E/F。
- [ ] **documentation/changelog and policy**：產品實作 PR 補 `changelog.d/executor-backoff-store-core.md` 與 `CHANGELOG.md [Unreleased]`，既有 docs/README 只在語意需同步處更新；用真 PR title/body/labels/base/head 跑 `policy_check`、驗 symlinks、`git diff --check`，不裸跑假綠、不改 VERSION。
- [ ] **tests/planning completeness and sizing**：純函式驗三件完整、三個固定 headings、Tasks 的 source/tests/documentation/CLI/changelog coverage；fix-standard 9 cards/9 bindings/2 gates與全規則實算現行8/Red，#831完整case投影6/Yellow另列且未 loaded。刪 spec／改 draft／缺 state 宣告／blocking marker 的負控制須拒，舊算法降低 incomplete score 不能被當可派。
- [ ] **documentation/review and delivery evidence**：Cortex 正式 RED/GREEN、獨立 adversarial review、policy/CI、merge、隔離 installed module/API smoke 分別留 evidence。未處置缺口 FAIL；明示有界 residual 不單獨 FAIL。root 確認正式 sizing 可派才開始產品工作；A 不執行 live admission rollout，也不關閉 #825/R05/R08/R09。
