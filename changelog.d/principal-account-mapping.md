# principal-account-mapping

- **`#626`：permgen 為不存在的 principal 產生 `setfacl`，`sh -e` 下中止整份 script 留下
  半套用的權限樹**——`permissions three-way --commands --paths` 會印出
  `setfacl -m u:operator:rX …` 與 `setfacl -m u:cortex-outbox:rX …`。這兩個是
  `registry.Principal` 的**抽象角色名**，不是真實帳號：`SCHEMES` 只把服務帳號那幾個
  principal 對應到真實帳號，`Principal.OPERATOR` 與外部 reader 直接沿用字面值——**對應表
  缺項，不是填錯**。實機上 `setfacl -m u:operator:rX` 回
  `Invalid argument near character 3`，而 runbook 第 2b 步是
  `sudo sh -e /tmp/p2b-permissions.sh`：`sh -e` 遇到第一條就**中止整份 script**，於是權限樹
  處於**半套用**狀態（前段資產已 chown/chmod、後段完全沒動），而錯誤訊息完全看不出是
  「帳號不存在」。最危險的是半套用的樹**看起來像裝好了**——目錄都在、前段權限正確，只有
  後段仍是預設權限。#620 的 traverse ACL 上線後又多產了幾條同樣的 phantom ACL，這條因此更
  嚴重。Phase 2b M1 沒踩到，只是因為 operator 執行前手動 `sed` 把兩個 principal 替換掉了，
  而那個替換不在 runbook 也不在 spec 裡——換句話說**任何人照 runbook 做都會中止**。
- **對應外部化**：`UidScheme` 新增 `operator_account` 與 `external_reader_account`，
  **預設 `None`**，並從兩個 `SCHEMES` 的 `account_of` 移除 `Principal.OPERATOR`
  （`external_reader` 的 `"cortex-outbox"` 預設值一併移除）。「operator 對應到誰」是
  **部署決定**、不是程式能猜的：單人機器上就是那個人的登入帳號，多人／CI 部署可能是
  專用帳號，而外部 outbox reader 在很多部署裡根本還沒有實體。新增
  `PRINCIPAL_ACCOUNT_OPTIONS` 作為**唯一真相**——`UidScheme` 欄位名、CLI 旗標、env 變數名、
  fail-closed 的錯誤訊息全部由它導出，將來多一個部署決定型 principal 只需加一列。
  `UidScheme.__post_init__` 直接拒絕把 `OPERATOR`／`EXTERNAL`／`INSTALLER` 塞回
  `account_of`：那會被靜默忽略而形成第二份真相，正是本 issue 的成因。
- **三態，不是兩態**：`None`＝未指定（fail-closed）／`ABSENT_ACCOUNT`＝**明示**本部署沒有
  這個角色的實體（該 principal 的授權整組略去，是被記錄下來的決定）／真實帳號名＝照常
  產生 ACL。少了中間那態，「本機還沒有 outbox reader」就只能靠亂填一個帳號來繞過，
  等於把不必要的讀取權授出去。
- **fail-closed 在輸出前**：`plan_to_commands()` 先跑 `assert_principals_resolved()`，
  有未對應的 principal 即 raise `UnresolvedPrincipalError`，**一行都不輸出**——CLI 因此
  stdout 全空、回傳碼 2，被重導成 script 的檔案是**空檔**而不是一份跑到一半會中止的半套
  script。訊息可操作：指出是哪個 principal、它是什麼角色、`--operator-account <帳號名>`
  或 `PSC_OPERATOR_ACCOUNT=<帳號名>`、`none` 怎麼用，以及「指定前先 `getent passwd`」。
  `generate_plan()` 刻意**不** raise（計畫是資料，看得見缺項反而有助診斷），未對應的
  principal 改以 `PermissionPlan.unresolved_principals` 隨計畫一起出現，CLI 的 JSON 模式
  把它放進 payload 並在 stderr 提醒一次——診斷模式不擋，但「少了誰的授權」必須看得見。
- **產生器自我檢查（擋未來，不只擋這次）**：`assert_output_accounts_known()` 在輸出組完後
  驗證每一行的 `u:<name>:` 與 `chown <owner>:<group>` 都落在
  `UidScheme.declared_accounts()` 內，否則 raise `UnknownAccountInOutputError`。**註解行
  一併檢查**——per-job 資產是以註解形式輸出的，phantom 躲在 `#   setfacl …` 裡照樣會被
  複製貼上執行。這條擋的是「將來新增一個 principal 而忘了進對應表」：字面角色名一漏進
  命令字串就 raise，不會再靜默產生一行 `sh -e` 下會炸掉的 `setfacl`。
- **帳號名形狀驗證**：帳號名會被**逐字**嵌進 `setfacl`／`chown` 命令字串，因此只接受
  `^[a-z_][a-z0-9_-]*\$?$`。`--operator-account "op; rm -rf /"` 在產生階段就被拒，
  而不是等到 operator `sudo` 執行時。
- **兩條注入管道，CLI 旗標優先於 env**：`--operator-account` / `PSC_OPERATOR_ACCOUNT`、
  `--external-reader-account` / `PSC_EXTERNAL_READER_ACCOUNT`（值 `none`＝明示不存在）。
  兩者並存的理由：runbook 是逐行 review 後手動執行的，旗標讓「這次產生用了哪個帳號」留在
  可稽核的命令列上；env 則讓 CI／自動化不必改命令列。**env 只在 CLI 這一層讀取**——
  `permgen` 維持純函式（不讀 env、不碰 IO），既有的
  `test_permgen_never_touches_the_filesystem`／`test_permgen_module_does_not_import_subprocess`
  兩條靜態不變式不受影響。
- **`PermissionPlan` 帶著方案本體走**：新增 `scheme` 欄位（`compare=False`）。#626 之後
  `SCHEMES[plan.scheme_id]` 反查到的是**未注入部署對應**的模組層方案，只憑 id 反查會把
  注入過的對應整個丟掉、讓自我檢查誤判；`_scheme_for()` 統一「顯式參數 > 計畫自帶 >
  `SCHEMES` 反查」的順序，`plan_to_commands()`／`directory_facts()`／
  `derive_traverse_grants()`／`unreachable_hops()` 一律走它。
- **測試**：新增 `tests/test_trust_root_principal_account_mapping_626.py`（46 測試，兩個
  scheme 逐一參數化）——(a) 未指定時 fail-closed 且訊息含 principal 名／旗標／env／
  `getent passwd`，只補一半也不放行；(b) 指定後每個 `u:<name>:` 都落在宣告帳號集合內，
  且注入的帳號真的被用上；(c) **不變式**：輸出中不得出現任何不在帳號集合內的字面值，
  含「偽造一行未來 principal 的輸出必須被攔下」與「註解行也檢查」與「`menu:x:` 不得被
  誤判成 ACL 條目」；(d) `ABSENT_ACCOUNT` 只略去那個 principal 的 ACL、別人的一條都不少；
  CLI 側涵蓋旗標／`=` 形式／env／旗標勝過 env／非法帳號名／懸空旗標／JSON 模式的
  `unresolved_principals`；另有一條登記表側的不變式——需要部署決定的 principal **恰好**是
  `operator` 與 `external`，多一個就代表對應表又缺了一項。既有 `_p2a`／`_p2b`／`_620`
  三檔的命令輸出測試改用注入後的方案（它們原本驗到的正是帶 phantom 的輸出）。
- **runbook**：`docs/superpowers/runbooks/trust-root-phase2b-setup.md` 第 2a 步——產生命令
  補上兩個旗標與 `getent passwd` 前置確認、`exit=` 期望；新增**稽核 6／6b**（權限 script
  的每個 ACL 帳號、骨架 script 的每個 owner 都必須 `getent passwd` 得到，出現任何 `!!`
  就不要執行第 2b 步）；明說**兩份 script 都是冪等的、中止後直接重跑安全**，不需要先
  回滾。另順修稽核 2 的假陽性：`grep -E ":[^ ]*w"` 會把
  `u:cortex-reviewer-planner:--x` 撈進來（`reviewer` 自己帶一個 `w`），#620 的 traverse
  節上線後這條就一直多報一行；pattern 改錨在 `u:<帳號>:` 之後。
