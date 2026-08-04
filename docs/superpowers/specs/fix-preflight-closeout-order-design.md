---
status: accepted
work_item: fix-preflight-closeout-order
---

# fix-preflight-closeout-order Design

## Decisions

### D1 在 ship validator 內重排三段，不動 phase spine

邊界重畫落在 `build_production_ship_validator.validate`（`work_bridge.py:1378`）內部：固定順序 local closeout → review attestation 確認 → external ship mutation。`WORKFLOW_PHASES`（`workflow.py:14`）與 `WorkflowRun` ship invariants（`workflow.py:534-557`）原封不動；`workflow.py` 只新增 ship transition 子階段常數 `SHIP_TRANSITION_STAGES = ("local-closeout", "pr-preflight", "external-ship")` 與比照 `validate_workflow_phase_transition`（`workflow.py:701`）的單調前進驗證函式，供 validator 與 stop reason 語彙共用。

理由：validate 是 ship 的唯一生產入口（`manager.py:6450-6474` 的 advance 與 `refresh-completion` 都經它），重排它即完成邊界重畫；改 phase spine 會牽動 run 持久化投影與所有既有 run 的相容性（#205 教訓：schema 面改動的爆炸半徑遠大於行為面）。archive 後的 candidate reset（`registry.py:1441` 的 `_manager_reset_workflow_after_archive`）既有語意「回 verify 重新把關」保留——local closeout 產生新 candidate 後仍走完整 verify → review，不因前移而縮短把關。

### D2 archive 段前移且入口不再要求 PR binding

`_ship_action`（`work_actions.py:2645`）的 archive 段（`work_actions.py:2904-2924`）前移到 validate 的 local closeout 段：builder worktree 內 active change 目錄存在時，依序 `_validate_local_archive_inputs`（`work_actions.py:108`）→ 官方 `openspec archive` → `_commit_archive_and_require_reverification`（`work_bridge.py:783`）→ candidate reset。這段完全不觸碰 `_pr_metadata`（`work_bridge.py:1416`）、`_ship_binding`（`work_actions.py:563`）與任何 `gh` 呼叫。`_ship_action` 內的 archive 段與 remote archive 檢查（`work_actions.py:2955-2956`）保留為防禦性後盾，語意不變。

理由：archive gate 的判準（tasks 全勾、openspec validate、policy_check、changelog fragment）全部是本地 deterministic 事實，被 `pr_number` 前置條件擋住是結構錯置。保留 `_ship_action` 原段是因為它同時服務 crash-resume 與 out-of-band 情境，重複執行時 active change 已不存在、自然 no-op。

### D3 archive commit 去除內嵌 push，closeout 零 external mutation

`_commit_archive_and_require_reverification` 內嵌的 `_push_exact_candidate` 呼叫（`work_bridge.py:841`）移除；push 統一由 external ship 段的既有路徑（初次 `work_bridge.py:1438`、既有 PR `work_bridge.py:1514`）承擔。`_record_manager_ship_job` 的 openspec-archive job row 紀錄保留（觀測事實不受 push 時點影響）。

理由：issue 驗收「無 GitHub authorization 時零 external mutation」。archive commit 是本地事實，push 是 ship transition 的 external mutation，兩者耦合正是 #83「lifecycle 沒閉環」教訓在 pre-PR 側的重演——push 失敗會讓本地已完成的 archive 陷入不可恢復中間態。風險：既有測試假設 archive 後 remote 立即更新；緩解見 Plan task 4 的回歸鎖定。

### D4 preflight 失敗回 trusted typed stop，resume 不落 gate_status="failed" 死巷

preflight 失敗（`work_bridge.py:1437`、`work_bridge.py:1512`）不再 raise 裸 RuntimeError，改回傳 trusted `needs_human` 結果：`{"trusted": True, "status": "needs_human", "reason": "pr-preflight-blocked", "head": candidate, "commit_id": candidate, "ref": <preflight evidence>, "hash": <sha256>}`，經 `_write_json_evidence` 落地 preflight evidence 後綁定 ref/hash——`validate_ship_result`（`manager.py:6676-6702`）已接受 `needs_human` 狀態，不需放寬其驗證。ship advance 的例外 handler（`manager.py:6467-6473`）維持原樣：typed stop 走正常回傳路徑，不觸發 `gate_status="failed"`；意外例外仍 fail-closed。

理由：現行「例外＝failed」把「可預期的外部邊界停止」與「不變量破壞」混為一談，正是 issue 症狀「本來可以本地完成的 closeout 也無法推進」的直接成因。以既有 trusted result 通道表達 stop，不新增例外型別階層，改動面最小；`authority_granted` 式的診斷／授權分離沿用 #261 模型——stop 可觀測、可 resume，但不授予任何 ship authority。

### D5 延伸 verify_authority_in_input_snapshot 做 post-materialize 實檔驗證，不另建機制

`verify_authority_in_input_snapshot`（`review.py:261-292`）新增可選 `workspace_root` 參數：提供時逐 authority ref 重讀 workspace 實際檔案、重算 sha256 比對 frozen baseline，缺檔或 drift 即 raise（沿用既有錯誤語彙）。既有 snapshot-row 驗證語意與呼叫端（`manager.py:6056`）不變。`prepare_review_worktree`（`review.py:226`）擴充接受 authority mapping 與 input snapshot：checkout 後以 `manager.py:4010-4031` 相同的 seed 寫法 materialize frozen refs，再呼叫延伸後的驗證；`manager.py:924` 呼叫端補傳參數。materialization 紀錄（相對路徑、sha256、source revision、candidate SHA）寫入既有 evidence 流（`_write_json_evidence`），不新增 job row 欄位。

理由：架構裁決明定延伸既有雛形。workflow sandbox 路徑已被 `_validate_workflow_input_snapshot`（`manager.py:3316`、呼叫於 `manager.py:6074`）覆蓋，真正的缺口是 slice-based 路徑零 materialize——補齊為同一機制而非第二套，避免兩套驗證器漂移。不新增 job row 欄位是 #205 教訓（providers 投影白名單外的新欄位使整份 projection degraded）。

### D6 stop reason 語彙對齊 #275 邊界

pre-PR 停止統一輸出 reason 與下一個合法 operator action（如 `awaiting-pr-authorization`：本地 closeout 已完成，等待 operator 授權建 PR），落在 resume 回傳與 status 呈現。本票只定義語彙與停止點；terminal outcome contract（stop 如何映射 terminal transition）屬 #275，W3 依本票穩定的邊界銜接。

理由：與 #275 的邊界若不先寫死，W3 會被迫同時動順序與 contract 兩個自由度；先穩定「stop 在哪裡、叫什麼」讓後續 work 只需接語意。

## 風險與緩解

- **#83 教訓（out-of-band merge → stranded run；閉環 > 事後 GC）**：重排新增「closeout 已完成、ship 未授權」的合法中間態，若無明確 resume 路徑會製造新的 stranded 形態。緩解：該中間態是 typed stop（D4/D6），reason 直指下一步合法 action，重複 resume 冪等（archive 已 present 時 no-op、preflight 可重跑）；Manager single-writer 原則不變，不引入任何需要手動 git 操作的恢復。
- **#175 教訓（re-claim 繼承已關閉 PR → delivery journal 孤兒）**：`_push_exact_candidate` 依賴 journal row 存在（`work_bridge.py:655` `delivery push journal missing canonical run`），push 時點延後不得改變 `_load_work_run` 建 row 與 `_rebase_delivery_journal_authority` 的時序假設。緩解：push 前的 row 建立鏈完整保留在 external ship 段內（`work_bridge.py:591-602`），local closeout 不讀寫 delivery journal；以 pre-PR 全流程測試鎖定 journal 在 closeout 期間零變更。
- **#208 教訓（retry 重複執行已完成 stage、reviewer input contract 在 dispatch 後才驗證）**：順序調整不得引發已完成 stage 重跑——archive 冪等（active change 目錄不存在即跳過）、preflight 失敗只 invalidate preflight 本身、不 invalidate 已完成的 verify／review evidence；materialize 驗證在 dispatch 前 fail-closed（D5），不讓 reviewer 在缺 authority 下產生要重跑的不可靠 PASS。
- **preflight typed stop 被誤當成功**：stop 結果 `status="needs_human"` 經 `validate_ship_result` 嚴格驗證且不寫入任何 gate passed 狀態；回歸測試斷言 stop 後 `gate_status != "passed"` 且無 PR／push 副作用。
- **archive 不 push 造成 remote 檢查落空**：`_ship_action` 的 `remote.active_openspec_absent`／`archive_present` 檢查（`work_actions.py:2955-2956`）在 external 段 push 之後執行，順序保證 remote 已含 archive commit；以「本地 archive 完成後建 PR 銜接全鏈」測試覆蓋。
- **slice-based 路徑 materialize 撞既有 worktree 內容**：seeds 沿用 expect_absent＋內容相等容忍的既有寫法（`manager.py:4023-4031`），已存在且內容一致時視為已 materialize，內容不一致 fail-closed，不覆寫。
