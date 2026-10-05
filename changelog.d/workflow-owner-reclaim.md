# #1261 workflow lane 的 builder 工作區改走 owner-bound reclaim

- **派工帶身分（`manager._dispatch_workflow_card`）**：會寫檔的 workflow build 卡在 provision 前算出
  owner identity 與 attempt，傳給 `creator.create()` 寫進工作區 marker，同時記進 job 記錄。身分形狀
  比照 slice lane 的 `{repo, work_id, slice_id}`，`slice_id` 是這張卡的 job task
  （`wf-<run_id 雜湊>-<card>`），所以身分同時綁住 Work Item、run 與 card；attempt 每張 job 一個
  `uuid4().hex`。身分在 provision 前就以 registry 的 owner identity 契約驗過。收不下 owner identity 的
  workspace creator 不設 TypeError 退路，與 slice lane 一樣 fail closed。
- **唯讀卡維持 Manager 直接回收**：launcher 為 write-forbidden（跑 `cortex-job-ro@`）的卡不帶身分。那份
  模板把 `<pool>/%i` 掛成 `ReadOnlyPaths`，#1167 的 helper 在裡面清不掉工作區；而且 job 寫不進工作區，
  不會有 builder 擁有的 inode，Manager 自己就收得掉。
- **回收前核對（`manager._trusted_build_workspace_target`）**：job 記錄帶身分時，身分必須恰好是本 run、
  本卡導出的那一組，marker 的身分與 attempt 必須逐字相同；job 不帶身分時 marker 也不得帶。marker 被拿掉
  身分、換成別人的，或自己長出身分，都以 `workspace-owner-identity-mismatch`／
  `job-owner-identity-invalid` 跳過，不刪任何東西。`owner_reclaim` 的核准（Manager 撰寫的 job spec、
  marker digest、nonce、pool 範圍、unit 身分）沒有任何放寬。
- **RC 涵蓋**：release profile 的 `owner-bound-reclaim` 驗的是與 lane 無關的協定（直接以 creator 帶身分
  provision，經 `reclaim_worktree` 回收）。workflow lane 的端到端驗收由 deployment canary closeout 的
  `build worktree reclaim is incomplete` 檢查負責：它在三 UID、`systemd-template` 的 RC 容器裡，逐一確認
  該 run 每張 build 卡（含唯讀卡）的工作區都已回收，#1261 正是這項檢查抓到的。
- 新增 `tests/test_workflow_owner_reclaim_1261.py`（20 個測試）：派工的 marker 與 job 帶身分、每張 job
  各自的 attempt、唯讀卡維持 Manager 路徑、不支援身分的 creator 在 provision 前 fail closed、正式採信
  路徑經 builder unit 回收且 Manager 不做 dirty scan、marker 竄改／身分不符／unit 失敗／完成紀錄無效時
  fail closed 且不刪任何東西，以及以真的 Manager 半與 helper（只模擬 `systemctl start --wait`）回收
  workflow 工作區、marker 在核准後被換掉時 helper 拒絕。
- 既有測試：`tests/git_fixtures.py` 與 14 個測試檔的 workspace creator 測試替身補上 `WorktreeCreator`
  protocol 已宣告的 `owner_identity`／`attempt_id` 參數；`test_quota_admission_dispatch_wiring_839.py`
  比對兩次獨立派工時，把每次各自產生的 `attempt_id` 列為允許不同的欄位。派工行為的斷言沒有改動。
- 限制：#1261 之前派出的 workflow build 卡沒有身分，三 UID 下仍需 operator 處理既有殘留。
