# Quota admission enforce 操作手冊

本手冊說明如何準備、啟用、觀察與回復 quota admission enforce。先在 shadow 模式確認設定和 observation 覆蓋，再安排沒有執行中 job 的維護時段切換開關。

## 1. 開關與重啟時機

唯一的 enforce 開關是 PSC_QUOTA_ADMISSION_ENFORCE=on。quota_admission_enabled() 在每次呼叫時讀 Manager process 的環境；未設定或不是 on 時維持 shadow。PSC_QUOTA_RESERVATION_ENFORCE 已移除，設定它不會改變行為。（出處：paulsha_cortex/coordinator/quota_admission.py:quota_admission_enabled）

Manager 從 user unit 載入 $HOME/.agents/core/runtime/<instance>-manager.env。更新這個環境檔後，必須重啟該 instance 的 Manager，新的環境才會生效。quota-pools.json 則在每次 dispatch／periodic tick 重新讀取；Manager 會以檔案內容 digest 快取解析結果，所以修改內容不需重啟。（出處：paulsha_cortex/deploy/templates/manager.service.tmpl:EnvironmentFile、paulsha_cortex/coordinator/manager_daemon.py:_quota_admission_context_for、_load_quota_pools_config_cached）

重啟 user unit 會依 KillMode=control-group 一併停止該 unit cgroup 內的 job；文件也明示 Manager restart 不保證 job 存活。因此只能在確認沒有執行中 job 後切換開關。（出處：paulsha_cortex/deploy/templates/manager.service.tmpl:KillMode、docs/unified-work-lifecycle.md:Headless launcher session boundary）

操作步驟：

1. 執行 cortex inspect status --json，確認沒有執行中的 job。
2. 編輯 $HOME/.agents/core/runtime/<instance>-manager.env，加入 PSC_QUOTA_ADMISSION_ENFORCE=on。
3. 執行 cortex recover service restart --instance <instance>。
4. 重啟後再讀一次狀態與 quota receipts，確認載入的是預期設定。

## 2. Binding 與 provider 覆蓋

長期 binding 建議使用穩定的 identity subject：

~~~json
{"kind":"identity","executor":"<executor>","model_id":"<model_id>"}
~~~

以 executor 和 model_id 綁定，不要複製某張卡解析出的 resolved profile key；該 key 可能隨卡片的 launch contract 或 requirements 改變。若同時有精確 profile binding，精確 binding 優先並覆寫 identity binding 的 pool/window 集合。（出處：paulsha_cortex/coordinator/quota_observation.py:_parse_binding_subject、paulsha_cortex/coordinator/quota_admission.py:_resolve_binding_coverage）

enforce 下，已綁 pool 的候選必須有 fresh observation，且所有綁定 pool/window 都要足額。沒有可用 collector、collector 回報 gap，或 observation 過期時，候選會是 unknown-remaining-quota 並被排除。可以先在 shadow 模式觀察這類 provider；若刻意不替該 identity 綁 pool，它會是 unmanaged，照既有派工規則放行，也就不受 quota admission 保護。（出處：paulsha_cortex/coordinator/quota_admission.py:assess_candidate_quota、paulsha_cortex/coordinator/quota_sources.py:provider_read_contract）

用目前 quota-pools 設定檢查曾出現在 admission receipts、但沒有任何 binding 涵蓋的 resolved profile：

~~~bash
cortex quota bindings --report --config "$QUOTA_CONFIG" --json
~~~

此報告唯讀，不會啟動 provider collector。（出處：paulsha_cortex/porcelain/quota.py:_run_bindings_report）

## 3. Fresh observation、TTL 與排程

enforce 下的受管候選需要 fresh observation。cortex quota observe 使用設定檔的 lease_ms 作 observation TTL；未設定時預設為 300000 ms。Manager periodic tick 的程式預設間隔是 300 秒，可由 PSC_MANAGER_INTERVAL_SECONDS 覆寫。TTL 應大於實際 tick 間隔；未設定 lease_ms 時，預設 TTL 與預設 tick 相同，不符合嚴格大於的條件，應在設定檔明確設較長 TTL。（出處：paulsha_cortex/porcelain/quota.py:_run_observe、paulsha_cortex/coordinator/manager_daemon.py:DEFAULT_TICK_INTERVAL、default_tick_interval）

Manager periodic tick 會重新評估 quota wait 並 reconcile reservation，但不會呼叫 provider collector。請另行排程週期性執行下列命令，並依 provider 狀況設定逾時：

~~~bash
cortex quota observe --config "$QUOTA_CONFIG" --json
~~~

目前沒有由 Cortex 安裝的 quota observe timer；觀測排程需由 operator 維護。（出處：paulsha_cortex/porcelain/quota.py:_run_observe、paulsha_cortex/coordinator/manager_daemon.py:build_periodic_tick_runner）

## 4. Admission 判定與等待

目前預設 demand 版本是 dispatch-unit:v2：每個受管 pool/window 以一個原生計量單位作准入門檻；若單位語意是 0–1 比例（如 AGY），門檻為 0.01（1%）。這不是模型用量預測。Manager 依既有排序逐一評估候選；enforce 下某候選不可行時會排除並嘗試下一個候選。全部候選都不可行時，不建立 job，run 進入 needs_human，reason 為 quota-admission-insufficient，並寫入 mode=enforced、outcome=wait、retry_eligible=true 的 receipt。（出處：paulsha_cortex/coordinator/quota_admission.py:DISPATCH_UNIT_DEMAND_VERSION、estimate_demand、paulsha_cortex/coordinator/manager.py:_dispatch_workflow_card、_quota_admission_stop）

quota-pools 設定檔存在但格式或內容無效時，enforce 會 fail closed：run reason 為 quota-config-invalid，不派 job，wait receipt 的 retry_eligible 為 false。修好設定檔後需手動 resume；periodic tick 不會自動重試這種設定錯誤。（出處：paulsha_cortex/coordinator/manager_daemon.py:_quota_admission_context_for、paulsha_cortex/coordinator/manager.py:_quota_admission_config_invalid_stop、_quota_admission_record_wait_decision）

## 5. Wait 恢復

periodic tick 只挑選目前仍有 enforced/wait receipt、reason 為 quota-admission-insufficient 且 retry_eligible=true 的 run。enforce 仍開啟時，Manager 會用當下的 fresh observation 與目前候選重新評估；至少一個候選可行才自動 resume。receipt 裡的 reset_at_ms 只是提示時間，不會單獨解除等待。（出處：paulsha_cortex/coordinator/manager.py:quota_wait_retry_is_eligible、_quota_wait_has_recovered_candidate、paulsha_cortex/coordinator/manager_daemon.py:build_periodic_tick_runner）

若當下沒有可行候選，status.json 的 workflow_waits 會列出 operator-resume-required。修正 binding、補上 observation 或調整設定後，可執行 cortex work resume "$WORK_ID" --repo "$REPO"；resume 會重新評估，不會只因 reset 時間已到就派工。（出處：paulsha_cortex/coordinator/manager.py:resume_workflow_run、paulsha_cortex/coordinator/manager_daemon.py:build_periodic_tick_runner）

## 6. Rollback 回 shadow

回復時先確認沒有執行中 job，再從 Manager 環境檔移除 PSC_QUOTA_ADMISSION_ENFORCE，並重啟 user unit。非 on 會回到 shadow／既有派工行為；重啟同樣會停止 unit cgroup 內的 job。

**保留 quota-pools 設定檔。** Periodic reconcile 需要該設定建立 quota context；刪除設定檔會讓 context 缺席，reservation reconcile 便沒有這份設定可用。Decision、reservation 與 observation stores 都是 append-only；不要手動刪除或改寫既有事件。（出處：paulsha_cortex/coordinator/manager_daemon.py:_quota_admission_context_for、build_periodic_tick_runner、paulsha_cortex/coordinator/quota_admission.py:AdmissionDecisionStore、paulsha_cortex/coordinator/quota_reservation.py:QuotaReservationAuthority、paulsha_cortex/coordinator/quota_ledger.py:QuotaEventLedger）

## 7. 查核命令與檔案

~~~bash
cortex quota reservations --json
cortex quota bindings --report --config "$QUOTA_CONFIG" --json
cortex inspect status --json
cortex work show "$WORK_ID" --repo "$REPO" --json
~~~

三份 append-only store 位於有效 coordinator root 下；若環境有設定 PSC_COORDINATOR_ROOT，以下路徑以它為根：

| 資料 | 路徑 |
| --- | --- |
| admission decisions | $PSC_COORDINATOR_ROOT/quota-admission-decisions/decisions.jsonl |
| reservations | $PSC_COORDINATOR_ROOT/quota-reservations/reservations.jsonl |
| observations | $PSC_COORDINATOR_ROOT/quota-observations/events.jsonl |

未設定 PSC_COORDINATOR_ROOT 時，以 paths.coordinator_root() 解析出的有效根目錄為準。（出處：paulsha_cortex/config/paths.py:coordinator_root、quota_admission_decisions_root、quota_reservation_root、quota_observation_root；store 檔名：paulsha_cortex/coordinator/quota_admission.py:AdmissionDecisionStore、paulsha_cortex/coordinator/quota_reservation.py:QuotaReservationAuthority、paulsha_cortex/coordinator/quota_ledger.py:QuotaEventLedger）

## 8. 判讀注意

job 終局若由 periodic reconcile 確認，reservation authority 會記錄 confirmed-terminated 並把 reservation state 收斂為 released；這與 settled 不同。終局用量另由 quota ledger 記錄，因此看到 released 不代表沒有記到用量。（出處：paulsha_cortex/coordinator/quota_admission.py:reconcile_bound_reservations、paulsha_cortex/coordinator/quota_reservation.py:QuotaReservationAuthority.reconcile、paulsha_cortex/coordinator/manager.py:_quota_admission_record_terminal_usage）

讀歷史 decision 時要一併看 demand／observation 版本與所選候選欄位。現在的新 receipt 預設是 dispatch-unit:v2；若看到較舊的 dispatch-unit:v1，#1196 修正前的 AGY fractional remaining 不應直接當成目前的 dispatch-unit:v2 門檻解讀。#1197 修正前的 selected_observation_state 也可能反映整體狀態，而非所選候選。新 receipt 的 selected_observation_state 來自所選候選的 CandidateAssessment；較舊 receipt 若缺少此選填欄位，投影應視為 unknown，不要自行推定。（出處：paulsha_cortex/coordinator/quota_admission.py:estimate_demand、AdmissionDecision.selected_observation_state、paulsha_cortex/coordinator/manager.py:_dispatch_workflow_card）

額度准入在 workflow card 的 _dispatch_workflow_card 候選迴圈執行；manager.apply_work_action 的 brainstorm／planning runtime 不經這條 quota admission 路徑。（出處：paulsha_cortex/coordinator/manager.py:_dispatch_workflow_card、apply_work_action）
