# Refine P1 進件校正與審查交接

日期：2026-09-07。文件基底：`79ba644780bf1c697c722ac24a297e7d02416100`。

## 範圍與證據等級

本次只整理9份 todo 與本審查報告；不改產品 code、tests、work-items 註冊表、
umbrella/spec、changelog、operator/runtime checkout 或服務。#822 已派工的 canary
材料不在本次改動範圍。未 commit／push／建立 PR，整合與必要 changelog 由主流程處理。

審查判準：未處置的缺陷／缺口為 FAIL；明文承認、影響有界且列管的殘餘風險
不單獨構成 FAIL。PASS 只表示目前進件敘述可進入實作／驗收，不能代替 RED／GREEN、
CI、merge、installed runtime 或 live lifecycle 證據。

使用 doc-coauthoring 的結構／歧義檢查整理已提供的任務脈絡；未再啟動 Claude 或
付費模型評測。獨立讀者驗證交主流程讀 diff 完成，本報告不冒充其結論。

## 審查發現與文件處置

| 正式 workstream | 原稿判定 | 已寫入的最小修正 | 實作狀態 |
|---|---|---|---|
| daemon-tick-clock-not-idle | PASS；新增防護要求 | 保留完成的 FakeClock／傳參驗收，新增有限正值規則、NaN／Inf／非正值測試 | 未實作／未測 |
| registry-persist-hygiene | FAIL | history fixture 改用 record_action；writer keyword／fault wrapper 相容；hardlink fallback；必要 audit 追溯界線 | 未實作／未測 |
| fix-dirty-recheck-idempotency | canonical FAIL；綜合稿可用 | canonical hash／state／gate／candidate／refs 比對，同 path 改內容恰好記一次 | 未實作／未測 |
| fix-superseded-terminal-replay | canonical FAIL；綜合稿可用 | 原子清綁／持久 supersession／consume、合法 recovery fixture、restart／completed proof 保護 | 未實作／未測 |
| executor-durable-backoff | FAIL | corrupt store 為 unknown，不解除 cooldown；重複觀測冪等，重啟與 skip 證據；分開完整動態 quota scope | 未實作／未測 |
| outcome-taxonomy-signals | FAIL | reason-aware diagnostic 消費端；排除裸 not-found 誤判；producer→persist→poll 回歸 | 未實作／未測 |
| launcher-session-and-timeout | FAIL | 加離線 CLI duration 合約 smoke；明示 session≠cgroup；以含 #820 基底開發 | 未實作／未測 |
| monitor-refresh-thread-backlog | FAIL | 明定事件屏障／W／pending bound／D／公平性與停止 fence；不以任意100事件≤2次作契約 | 未實作／未測 |
| bucket-c-evidence-dedup | 不應另行派工 | 移除 accepted/work_item authority，改成兩份 canonical todo 的交接索引 | 索引文件已整理 |

以上是發現已在文件處置的記錄；主流程仍需讀 diff 驗證語意並跑正式 intake gates，
不能把本表改寫成所有開發 work item 已 PASS／完成。

## 關鍵 source／test 錨點

以下行號對上述基底核對，後續 code 變更時應以函式名稱重新定位。

- #819：`paulsha_cortex/coordinator/manager_daemon.py:1449` periodic lane；
  `:1458` not-skipped 才推時鐘；`:1474` failure 會推時鐘。
  `tests/test_manager_daemon_tick_backoff.py` 的 `_FakeClock` 可建決定性 regression。
- #821：`paulsha_cortex/coordinator/registry.py:1571` 的 update_slice 只更新 refs；
  `:1650`／`:1655`／`:1673` 才是 record_action 的三種 history append。
  `:572` 原子 writer、`:617` persist、`:478` normalization on load。
  `tests/test_workflow_production_wiring.py:5690` 與
  `tests/test_planning_publication_transaction_536.py:553` 是單參 fault wrappers。
- #496：`paulsha_cortex/coordinator/manager.py` 的 `complete_tick` dirty recheck
  與 `_apply_verification_result`；`registry.py` 的
  `_read_current_verification_evidence_hash`／`_normalize_loaded_slice_verification`。
  `tests/test_pre_candidate_recovery.py::test_candidate_worktree_dirty_reevaluation_on_tick`
  只有既有真實轉換測試，需要另補 unchanged／same-path-changed 變體。
- #497：`manager.py:555` allowed_slice_actions 與 `:1740` candidate guard；
  `registry.py:1575`／`:1579` 只在參數非 None 時更新綁定／candidate。
  `tests/test_pre_candidate_recovery.py:139` 是合法 candidate=None fixture；
  `:198` 另有 candidate 存在則 fail-closed 的守衛測試。
- #825：`coordinator/provider_outcome.py` 已有 reset_at 與 authority 分級，
  `manager.py` 的 `_runtime_preflight_gate`／`_provider_failure_reroute` 是候選 gate
  入口；`autonomy.dispatch_ready` 是 slice 派工入口。新 store 本身尚未存在。
- #826：`manager.py:10533` 對所有 runtime_diagnostic dict 清除 classification，
  `:10543` 目前只考慮 retryable；只加 enum 與欄位寫入不足以交付。
  `outcome_taxonomy.py:621` 文字層維持 provider/model text 分離。
- #823／#824：`launcher.py` 的 SubprocessLauncher.launch 共用 popen_kwargs 與
  兩個 Popen 路徑；#820 可能改變精確行號，不能沿用舊並行衝突說明。
  真 process-group 測試和 systemd runner/template 測試必須分清證明範圍。
- #827：`monitor/watcher.py:130` 每事件 Timer；`monitor/service.py:317` 第二層
  Timer、`:337` 呼叫 refresh_project；`monitor/work_api.py:502` 全域 refresh 鎖；
  `tests/test_stage9_project_monitor_service.py:977` 只有三次事件的 burst 測試，
  `:1199` 只有 watch state 的 unwatch 測試，均不足以證明慢 provider／stop 競態。

## 需主流程保留的裁決與邊界

1. #819 有限正值規則一致；保留 env 非法值安全回預設、CLI 顯式非法值拒絕的
   相容政策，兩者不能讓 NaN／Inf／零／負值成為生效門檻。manual lane／cgroup-aware
   capacity 仍是後續範圍，不是這次不小心遺漏的設定入口。
2. #821 limit=1 明定保留最新筆；limit≥2 維持首筆＋近期資料。截斷計數不是完整
   archive，必要 current/completion evidence 仍可追溯；完整長期封存由 umbrella
   successor 列管。需全歷史的現場須先保存 registry 備份，再啟用截斷。
3. #821 stale temp 清理要求能證明失活／目錄獨占，mtime 超過30秒本身不夠；
   不為清理擴張跨 process lock scope。無證據即保留並診斷，不任意刪除。
4. #825 只交付已知失敗後的最小持久退避；動態選模／共享 quota pool／任務用量
   預估／reservation 交完整 plan。這些後續工作不得因本票 merge 被標完成。
5. #823 session 隔離不保證 systemd restart survival；#824 CLI smoke 必須實際
   跑到不耗模型用量的參數 parser。若不支援該旗標，交主流程決定 adapter／版本路徑。
6. #827 文件要求 bounded provider timeout／stop fence；若實作選定的 adapter
   根本沒有可驗證的 timeout，主流程需處理依賴，不以多開 thread 冒充完成。
7. #496／#497 是兩個正式 work item；bucket-C 索引不得再註冊。#497 的
   supersession／state／binding 原子提交須有故障注入測試，不能只靠多次 persist 的
   註解宣稱 atomic。

## 文件檢查與交付邊界

本次只執行純文件範圍／空白／識別字檢查；未執行產品 unit/integration tests、
正式 planning/authority mutation、policy-check、CLI flag smoke 或 runtime canary。
所有 todo 的工作項維持未勾選；不存在代填的 evidence hash 或預先勾好的 CI／部署結果。

純文件檢查結果：變更路徑與授權10檔完全一致；8份正式 todo 的 work_item／
`## Tasks` 符合對應路徑，bucket-C 無 authority frontmatter；10檔均無尾端空白、
預勾工作項、個人絕對路徑或佔位字。tracked diff 的 `git diff --check` 成功；
產品 code／tests／work-items／CHANGELOG／#822 路徑的 diff 為空。

主流程整合時需補正式 changelog fragment／Unreleased、帶 PR context 的 policy-check，
並確認每個 accepted todo 的 authority 與 issue／work registration 一致。不要對已凍結
的 #822 canary 改 plan bytes，也不要提交 operator 的 #828 或其他 repo 無關工作。

## 主流程整合補註

`.cortex/work-items.yaml` 的 `trust-root-executor-hardening` 排除 #692／#763，
是接手時已存在的 Claude collision 修正，不是新增產品功能。核對基底中 #692
已有 `trust-root-home-fail-closed`、#763 已有 `manager-gitconfig-delivery`，
各自 canonical todo 引用自己的 issue；hardening 的正式 issue 為 #665。
保留 excludes 是為維持分責與唯一 authority，不重開、重派或改變這些工作的驗收。
