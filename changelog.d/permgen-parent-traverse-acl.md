# permgen-parent-traverse-acl

- **`#620`：permgen 沒產生父目錄 traverse ACL——三分下 builder／reviewer-planner 走不到
  自己的 spool，正向路徑全斷**——產生器對**葉節點**授的跨帳號 ACL 完全正確
  （`setfacl -m u:cortex-builder:wx <monitor>/event-spool`、
  `setfacl -m u:cortex-reviewer-planner:wx <coordinator>/review-verdicts`），但父目錄是
  `0700 cortex-manager:cortex-manager`。POSIX 要求路徑上**每一層**都帶 `x`（search）位
  才走得到葉節點，於是 Phase 2b 實機兩條 append-only 正向路徑同時 `Permission denied`
  ——而且錯誤訊息指的是**父目錄**，與真正缺的那條授權不在同一層，極難診斷（實機是靠
  operator 手補三條 `setfacl … :--x` 解封的）。
  修法：`permgen` 新增一組純函式，把 traverse 權**與葉節點 ACL 同源機械導出**——
  `derive_traverse_grants()` 對每個授了跨帳號 ACL 的資產沿路徑往上走到管理樹根
  （`managed_roots()`，再往上是發行版標準的 root-owned 0755，不歸本產生器管），
  逐層以 `can_traverse()` 判斷該帳號是否**已經**走得過去（owner 位相符／others 帶 x
  ／既有 ACL 已含 x），只為真正缺的那幾層產生一條 `--x`。中間層的目標狀態由
  `directory_facts()` 從登記表資產的 `PermissionEntry` ＋ `scaffold_directories()`
  兩個既有真相合出來，**沒有第二份手寫清單**；未被任一方描述的中間層（例如 job 自建的
  `<worktree>/<job-id>/.cortex`）保守視為不可 traverse，寧可多產一條也不漏。
- **`--x` 而非 `r-x`，且一律不設 default ACL**——這是安全要求不是風格：`r-x` 會讓 job
  帳號列得出 `coordinator/` 底下還有哪些 Manager 資產；default ACL 則會讓該目錄底下
  **新建的每個物件**都繼承這條授權，等於把一條 traverse 放大成整棵子樹的授權。兩者都有
  測試釘住。三分下實際導出七條（含 issue 手補的那三條，另加 `cortex-outbox` 對
  `coordinator`／`coordinator/digest`、`operator` 對 `coordinator`，以及 per-job 的
  `<worktree>/<job-id>/.cortex`），二分下 reviewer 與 owner 併帳故自動少一條——**同一套
  policy，只換 config**。
- **順序**：traverse 節排在 `plan_to_commands()` 輸出的**最尾端**。`chmod` 在帶 ACL 的
  物件上會把 group 位寫進 ACL **mask**，先 `setfacl` 再 `chmod 0700` 會讓所有具名條目的
  有效權限被 mask 成空——順序反了不會報錯，只會靜默失效。測試釘住「traverse 行全部晚於
  最後一個 `chmod`／`install -d`，且是連續的一節」。
- **可驗收的鏈完整性**：新增 `account_can_reach()`／`unreachable_hops()`——套完產生器輸出
  後，某帳號到某資產的路徑上**是否每一層都可 search**，成為可測的純函式判定（也是
  runbook 之外第二個檢查點）。`plan_to_commands()` 新增選擇性的 `layout`／`scheme` 參數
  （只影響 traverse 節；未給時取 `DEFAULT_LAYOUT` 與 plan 的 scheme），既有呼叫端逐字不變。
- **測試**：新增 `tests/test_trust_root_permgen_traverse_620.py`（26 測試，兩個 scheme 逐一
  參數化）——實機手補的三條逐字出現在可執行行裡；**每一個**跨帳號 ACL 的鏈都完整（不只
  那三條）；反向對照（拿掉導出的授權就重現 issue 的斷法，證明測的不是「本來就通」）；
  `--x` 不含 `r`／`w`、無 default ACL、job 帳號仍列不出 coordinator；已可 traverse 的三種
  情形（root-owned 0755／0701 others-x／既有 `rX` ACL）不重複產生；同一 `(path, account)`
  去重且輸出決定性；換 `PathLayout` 不必改產生器一行程式碼。
- **runbook**：`docs/superpowers/runbooks/trust-root-phase2b-setup.md` 第 2 步補「稽核 5」
  （traverse ACL 存在、不得是 `r-x`、不得有 default、必須留在 script 尾端）與套用後的
  正／負向驗證（builder 寫 event-spool、reviewer-planner 寫 verdict spool 皆成功；
  `ls /var/lib/cortex/coordinator` 對 job 帳號仍 Permission denied）。
