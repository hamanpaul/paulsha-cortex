# Bucket-C 進件校正交接索引

本檔是歷史材料的導覽，不是 accepted plan／work item authority；沒有獨立
work_item，不得註冊或派工第三個 bucket-C 工作。

## 正式驗收來源

- [#496 dirty recheck 冪等](../fix-dirty-recheck-idempotency/todo.md)：
  canonical 內容 hash、同路徑改內容仍記錄、未變結果不追加、fail-closed 保留。
- [#497 superseded terminal replay](../fix-superseded-terminal-replay/todo.md)：
  明確解除 job 綁定、持久 supersession、合法 action fixture、重啟與多 terminal 防重播。
- [#821 persistence hygiene](../registry-persist-hygiene/todo.md)：
  digest no-op、history 有界與原子備份；它不取代 #496 的 append 冪等修復。

## 已移植校正

- #501 的 contract／evidence hash 分離已存在於本次基底，#496 讀
  `current_verification_evidence_hash`，不重做或假設仍需污染 contract hash。
- #496 比較內容而不是 evidence path；同 path 不等於同結果。
- #497 不依賴 `update_slice(builder_job_id=None, candidate=None)` 解除綁定，
  因該 API 把 None 視作未提供。recover fixture 必須是無合法 candidate SHA 的
  needs_human slice，並明確提供 `PSC_REPO_ROOT`；有 SHA 的 dirty slice 走 retry-build。
- 保留 #383 fanout 語意、不可變證據／quarantine 與歷史可稽核性，不以刪除 job
  或放寬 evidence writer 避開衝突。

原紀錄的92,403筆 history、每小時增加527–600筆及58.7MB jobs.json 是歷史觀察，
不是此次 host 的最新測量。進件文件完成不代表上述修正、測試或 installed runtime
已交付；各正式 work item 分別記錄完整 Cortex lifecycle 證據。
