### Fixed
- **#638 / trust-root：兩個 spool 的 producer／consumer 權限模型在三分下三處失效——
  verdict 通道（Phase 2a）實際從未成立過，`commit-spool` 繼承了同樣的缺陷**
  （Closes #638）

  operator 在為 #623 的 bundle 回收做端到端實測時撞到三個**獨立**缺陷，全部有實機
  證據；#637 merge 之後降權模式下的成果回收整條不可用，卡在本票。三個缺陷影響既有的
  `review-verdict-spool`（#599）與新的 `commit-spool`（#636／#637）——形態刻意相同，
  所以缺陷也一起繼承。

  **缺陷 1（blocking，已在產品程式碼上重現）**：per-job 目錄以明確 mode 建立會**重設
  ACL mask**，把 default ACL 繼承來的具名條目壓成 `#effective:---`：

  ```
  $ getfacl /var/lib/cortex/coordinator/commit-spool/<key>   # prepare_commit_spool() 建的
  user::rwx
  user:cortex-builder:-wx    #effective:---     ← 繼承來的授權被 mask 遮掉
  mask::---
  ```

  接著以 builder 身分執行 `build_bundle_command()` 逐字產生的命令：
  `fatal: Unable to create '…/commits.bundle.part.lock': Permission denied`。
  兩個實例（`review.py` 的 `spool_dir.mkdir(mode=0o700)` 與 `prepare_commit_spool()`
  的「0700 建立／已存在時重新 chmod 0700」）是**同一個 bug**。

  **修法**：per-job 目錄**不再傳明確 mode**，初始權限交給 default ACL；建立後只
  **檢查**並收窄，而不是用 `chmod` 把 mask 一起壓掉。取捨寫在
  `spool_slot.narrow_inherited_mode()`：`other` 位一律收掉（它與 mask 無關，收窄它
  不影響任何具名條目，所以這一半的「不比 0700 更鬆」無條件保住）；`group` 位只在該
  項**沒有** access ACL 時才收掉——有 ACL 時 group 位**就是 mask**，壓掉它正是缺陷 1
  本身，此時它的值由 operator 的 `default:mask::` 決定，那是授權模型的一部分。因此
  「不比 0700 更鬆」在無 ACL 的部署（含所有既有測試環境）下逐字成立，在有 ACL 的部署
  下被**明確地**讓給 default ACL；那一格真正的邊界是它的容器（`0700 cortex-manager`，
  別的帳號連 traverse 都進不來）＋ per-account 具名條目。若 mode 已經合規則完全不呼叫
  `chmod`——`chmod` 本身就是會重設 mask 的那個動作。

  **缺陷 2**：`wx` 無 `r` 的那一格上，producer 建的檔由 producer 擁有（又常帶降權
  unit 的 `UMask=0077`），consumer 讀不到；容器 owner 的身分不給檔案內容的讀取權。
  **修法**沿用 #637 已實機驗證的繞法並套到 `review-verdict-spool`：producer 寫完後
  自己 `chmod 0644`。bundle 那邊 producer 是 Manager 組出的 `git` 命令（既有的
  `.part` → `chmod` → `mv`）；verdict 那邊 producer 是**模型本身**，它不會自己
  chmod，所以由 wrapper script 在模型結束後補一段
  `{ [ -f <verdict> ] && chmod 0644 <verdict>; } 2>/dev/null || :`，排在 exit
  sentinel **之前**（sentinel 一出現 Manager 隨時可能開始收割），且以存下來的模型
  `$?` 收場（#604：降權模式下 unit 的 exit code 就是這支 script 的 exit code）。

  **缺陷 3**：`seal_review_verdict_spool()` 的 `os.chmod(<verdict>, 0o444)` 由
  Manager 執行，但檔案是 reviewer 擁有的——**只有 owner 或 root 能 chmod**，必然
  `PermissionError`，而該處**刻意不 raise**，所以**無聲失敗**。實測後果：reviewer
  可以在 Manager 判讀之後回頭 `printf TAMPERED > <verdict>`，spec §R2 要守的東西
  從未成立。**修法**同樣沿用 #637：封的是**目錄**（`0500`）——Manager 是目錄的 owner，
  收掉 `w` 讓那一格定版，而 `chmod` 同時把 ACL mask 收成 `---`，reviewer 具名條目的
  `x`（traverse）一併失效，它連既有的 verdict 檔都再也打不開。**pre-seed 守衛語意
  完全保留**（dispatch 前該格必須不存在合法 verdict）。

  **收斂成一套 helper**：新增 `coordinator/spool_slot.py`，兩個 spool 的 per-job
  生命週期（建立 → producer 寫 → consumer 讀 → seal）全部走它。這個 bug 之所以有
  兩個實例，正是因為兩邊各自實作了這一段。`review.py` 與 `job_workspace.py` 只保留
  各自的路徑推導、錯誤面翻譯與守衛訊息（operator 看到的錯誤字串一字未變）。解封
  （`commit-spool` 的 retry：同一個 slice_id 重派）改為**整格重建**而不是 `chmod`
  回去——seal 把 mask 收成 `---`，而正確的 mask 只有讓 default ACL 重新繼承一次才
  拿得到；重建同時涵蓋了 #637 既有的「起跑前清掉殘留」。`review-verdict-spool` 每次
  派工都是新的 reviewer job id，因此不需要、也不該有解封路徑。

  **測試**：新增 `tests/test_spool_permission_model_638.py`。既有測試在單 UID 環境下
  跑，那裡 ACL mask 不影響任何事，所以全綠卻不承載三分的語意——#637 的 CI 全綠卻在
  實機第一步就斷掉正是這個形狀（同類前例：#630「綠靠 cwd 剛好是 repo」、#631「長
  TMPDIR 下 36 個測試靜默 skip」）。因此新測試**自己建出帶 default ACL 的容器**，
  直接斷言具名條目的 **effective 權限**（mask 套用後的結果）而不是斷言 mode；ACL 的
  設定與讀取都走 `system.posix_acl_*` xattr（`setfacl`／`getfacl` 用的同一個核心
  介面），不依賴 `acl` 套件裝了沒。同一組不變式**同時參數化涵蓋兩個 spool**：那一格
  建立後具名條目不得被遮、`other` 位一律收掉、seal 之後 mask 與 effective 皆為
  `---`、retry 解封後授權真的回來、pre-seed 守衛在 ACL 樹上仍然拒絕。另有一條**突變
  驗證**測試把「修法前的形狀（`mkdir(mode=0o700)`）在同一個 fixture 下必須是紅的」
  釘住，避免 fixture 哪天不再建出 ACL 樹而讓上面那些斷言退化成空過。跨 UID 的功能
  驗收（producer 寫得進去／consumer 讀得到／seal 後 producer 改不動，正反各一）需要
  root 才借得到兩個身分，拿不到時**明確 skip 並說明理由**（結構性的 effective-ACL
  斷言在任何支援 ACL 的環境都會跑）；檔案系統不支援 ACL 時同樣明確 skip 並說明
  ——不靜默通過。

  **與 M2（#615）的關係**：本票是 M2 的前置。Phase 2a 的 verdict 通道在三分下從未
  真正成立過，只因 reviewer 目前仍以 Manager 帳號在行程內跑、同 UID 所以一切都通。
  M2 一旦落地（reviewer 有自己的降權啟動面），verdict 通道會立刻整條斷。

  全套 `python3 -m pytest tests/ -q`：3761 passed，零回歸。
