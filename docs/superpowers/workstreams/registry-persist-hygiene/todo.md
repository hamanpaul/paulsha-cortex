---
status: accepted
work_item: registry-persist-hygiene
---

# Registry persistence 與 history 有界化

## Boundary

- Issue：`hamanpaul/paulsha-cortex#821`；相關 #496／#781／#818。
- 範圍限 `coordinator/registry.py` 的無變更寫入短路、slice history 輪替、rollback
  備份與 stale temp 清理。不改 `COORDINATOR_STATE_SCHEMA_VERSION`、
  `_reload_if_changed` 偵測、dirty recheck 決策、tick 時鐘或跨 process 鎖。
- 新欄位只加在 slice row，不新增 jobs.json 根欄位；monitor 對根 keys 有白名單。
- digest no-op 只減少真正無變更的 `update_status` 同狀態自轉／`update_job` 同
  worktree 等寫入；`record_action` 的新 timestamp 是真實內容變更，#496 才負責
  消除 dirty recheck 的重複 append。`update_slice` 本身不追加 history。
- 字串 schema `"1.0"` migration 與 instance decommission 屬完整 refine plan 的
  相容性／ownership 工作；不誤引 #815 作為本票實作依據。
- 輪替後的首筆／近期 history 與截斷計數不等於完整封存。current evidence refs、
  completion record 引用的必要證據必須保持可追且不可被清理；完整長期 archive／
  retention 由 umbrella successor 列管，本票不宣稱提供全歷史重播。需完整歷史的
  既有部署在 archive 能力完成前，先由主流程保存原 registry 備份再啟用截斷。

## Tasks

- [ ] `_persist` 產生穩定 JSON（`ensure_ascii=False, indent=2, sort_keys=True`），
      以實際 UTF-8 bytes 的 SHA-256 比對 `_last_persisted_digest`；相同且 state
      檔仍存在時不呼叫原子 writer、mkstemp 或 replace。state 檔遭刪除仍須重建。
- [ ] `_write_payload_atomically(payload)` 保留 dict 位置參數呼叫契約。若新增
      optional `serialized=` 以避免重複序列化，需同步讓既有 fault-injection
      wrappers 接收／轉送該 keyword，不改其故障條件與 rollback 斷言：
      `tests/test_workflow_production_wiring.py::fail_plan_transition` 與
      `tests/test_planning_publication_transaction_536.py::crash_on_plan_transition`。
      v1 migration 原本只傳 dict 的路徑仍有效；不得用吞 TypeError 掩蓋相容性錯誤。
- [ ] digest 只在成功寫入後更新；`_load` 必須在 normalization 可能觸發 `_persist`
      之前以磁碟原始 bytes 建立 digest。rollback 重新 `_load()` 時恢復原 digest，
      不得讓失敗候選的 digest 遮蔽下一次正確寫入。
- [ ] 新增 `DEFAULT_SLICE_HISTORY_LIMIT=500`，可由
      `PSC_COORDINATOR_SLICE_HISTORY_LIMIT` 或 `JobRegistry(history_limit=...)`
      覆寫，僅接受正整數。三種列表 `evidence_history`／`evaluation_history`／
      `actions` 共用輪替 helper：limit≥2 保留首筆及最新 limit−1 筆；limit=1
      保留最新一筆，避免第一筆永久遮蔽當前事件；每個丟棄項累計至
      `slice_row["history_truncated"][key]`，不重設已有計數。
- [ ] `_validate_loaded_slice` 以複本正規化超限 history，不就地修改原始 payload，
      使既有 `slices != payload["slices"]` 偵測可落盤一次；第二次載入不再改寫。
      缺少 `history_truncated` 的舊檔正常讀取；`_copy_slice` 為該巢狀 dict 建立
      獨立複本。非法 limit／counter 類型或負值需有明確拒絕測試。
- [ ] rollback snapshot 優先以 `os.link(state_path, backup)` 建立，同 filesystem
      成功時不重複複製整檔；EPERM／EXDEV／不支援 hardlink 等 OSError 時退回既有
      bytes 複製與 fsync 流程。這不同於 `_write_v1_backup` 的「先複製到 tmp 再
      link 命名」，不得混淆成本；保留替換後 fsync 失敗時的 rollback 語意。
- [ ] construction 時只清理 state 目錄內已確認不屬於活躍 writer 的 stale temp：
      `tmp*.tmp`／`tmp*.backup.tmp`／`tmp*.rollback.bak`，grace 不低於 30 秒，
      不遞迴、不跟隨 symlink、不刪 subdirectory／jobs.json／v1 backup／人工備份。
      若無法證明 writer 已結束或此目錄具有獨占使用權，略過清理並留下診斷；
      檔案 mtime 超過 grace 本身不代表無活躍 writer。不要於 reload／persist 清理。
- [ ] 新增 `tests/test_coordinator_registry_persist_hygiene.py`：一次 mutation 後
      連續三次 `_persist()`、同狀態 `update_status` 均 mkstemp=0、mtime_ns／inode
      不變；刪除 state 後可重建；writer error／rollback／normalization 的 digest
      正確；保留既有 #501 normalization 與 released claim-key load 測試。
- [ ] history fixture 必須使用真正追加的 `record_action`：limit=5，12 次帶
      `evidence_refs` 的 action 後 evidence_history／actions 為 5、保留首筆與第12筆、
      dropped=7；另用 `evaluation_refs` 驗 evaluation_history。覆蓋 limit=1、
      limit=2、超限載入、二次載入 no-op、既有 counter 延續、對 copy 改值不污染 live row。
      不得用 `update_slice(current_evidence_refs=...)` 期待 history 追加。
- [ ] 測 hardlink 成功、EPERM／EXDEV fallback、replace 前後故障；stale sweep
      保留活躍／未知 ownership／新檔／symlink／子目錄／人工備份，僅刪已證明失活
      且逾 grace 的匹配項。不存在目錄正常建置；無權讀 state 仍維持既有錯誤語意，
      不因 sweep 的容錯而偽裝 registry 已成功載入。
- [ ] 補本 workstream changelog fragment 與 `CHANGELOG.md [Unreleased]`；透過
      Cortex 記錄 RED／GREEN、fault/rollback regression、必要完整 gates 與 runtime
      寫入次數證據。不得把單純 digest 改善宣稱為 #496 的 E1 頻率已修。
