### Fixed
- **#657 / trust-root Phase 2b：gate（與 reviewer／planner）的 template unit 讀不到自己的
  job spec——spec spool 改為 per-principal，preflight 改驗「那個身分讀得到」**（Closes #657）

  **實機病灶**：#629 的 gate 執行身分落地後，`sudo -u cortex-manager systemctl start
  'cortex-gate-job@<inst>.service'` 一律以 `78/CONFIG` 收場，journal 只留
  `cortex-job-shim: 讀不到 job spec …/job-specs/<inst>.json: [Errno 13] Permission denied`。
  成因：三份模板 unit 共用同一個 `Environment=PSC_JOB_SPEC_SPOOL=<coordinator>/job-specs`，
  而登記表資產 `job-spec-spool` 的 reader 面**只有 builder**。shim 是 systemd 套完
  `User=` **之後**才執行的（`ExecStart=` 就是 shim），它以 job 身分讀 spec ⇒ 必然被拒。
  **reviewer／planner 同型且已查證**：`cortex-reviewer-job@.service` 逐字指向同一個
  spool，而 `cortex-reviewer-planner` 同樣不在 reader 面——#652（M2）落地時沒驗到這一層，
  只是還沒有人在四分部署上派過 reviewer job。

  **裁決＝方向 2（per-principal spool）**：每個降權 principal 一個 spool 根
  （`<coordinator_root>/job-specs/{builder,reviewer,gate}`），各自只授自己
  （`rX` access ＋ default ACL）。容器降為 owner-only、零跨帳號讀權，三個帳號在那一層
  只拿到 `derive_traverse_grants()` 機械導出的 `--x`（走得進自己那格、列不出這台機器上
  還有誰的 job）。理由是**可稽核性**：「哪個身分讀哪個 spool」因此是 root-owned unit 檔
  上可逐字讀懂的一行，而不是一組共用目錄上多條 ACL 的交集；而且不必把「跨 persona 互讀
  spec」這個新性質偷渡進來（方向 1 會）。方向 3（spec 檔 chown 給 job 帳號）不成立：
  spec 是 Manager 寫的，chown 給別的 owner 需要 root，而「cortex 任何元件永不具 root」
  是既有裁決。

  **機械導出，不硬編清單**：`registry.DOWNGRADED_JOB_PRINCIPALS`（由 `permgen` 搬進登記表，
  `permgen.DOWNGRADED_JOB_PRINCIPALS` 成為指向它的別名而非第二份）同時決定三個資產
  （`job-spec-spool-<principal>`）、三條路徑（`config.paths.job_spec_spool_for()`／
  `PathLayout.job_spec_spool_for()`）、六份 unit 的 `Environment=` 與容器上的 traverse ACL。
  新增降權角色只需動那張表一行。`TrustRootAsset` 新增 `path_resolver_args`，讓一支帶參數的
  resolver 服務一族資產（R1 雙向等式與 R3 自檢因此仍逐項解析得到真實路徑）。

  **preflight 改為驗 effective 權限**（本票的第二半）：`prepare_systemd_template()` 原本只
  `os.path.isdir(spool)`——那對 #657 完全無感（目錄存在、Manager 寫得進去，缺的只是那個
  帳號的 ACL）。新增 `job_runner.effective_perms_for_account()`／
  `inherited_perms_for_account()`：以 `os.stat` ＋ POSIX ACL xattr
  （`system.posix_acl_access`／`system.posix_acl_default`）直接算 kernel 用的那條
  POSIX.1e access check，**含 mask 收斂與整條 traverse 鏈**。Manager 不是那個身分、
  `os.access()` 答不了這題，但那條判定本來就是對 inode 中繼資料的純計算。失敗因此變成派工
  **之前**的 `job-runner-job-spec-spool-unreadable`；spec 落地後另有一次就地複驗
  （`job-runner-job-spec-unreadable-by-job`），把 `write_job_spec()` 那段「`chmod 0640`
  是為了讓繼承的 ACL mask 不被關掉」的推導從註解升級成斷言。誠實邊界（mount 選項、
  mount namespace、LSM）寫進 docstring 與 runbook，由 `sudo -u` 實測步驟承接。

  **mask 陷阱（修本票時在實機上量到）**：`/var/lib/cortex/coordinator/job-specs` 當時是
  `mask::---` ＋ `user:cortex-builder:r-x #effective:---`——ACL 還在、`getfacl` 看得到，
  實際權限卻是零（`chmod` 會重寫 ACL mask，因此「先 setfacl 再 chmod」會靜默失效）。
  只看「有沒有那條 ACL」的檢查會說一切正常；新的 effective 判定當場抓到。

  **測試**（`tests/test_per_principal_spec_spool_657.py`，23 條 ＋ 1 條明確 skip）：本族
  （#630／#631／#638／#657）的 bug 全部是「單 UID 環境測不出 ACL 語意」，因此本檔**自己
  建出一棵真實 ACL 樹**（`setfacl` 一個真的存在、uid 與本行程不同的第二個帳號），以
  **effective 權限**斷言，並與系統 `getfacl` 的 `#effective:` 交叉核對；涵蓋「讀得到自己
  的、讀不到別人的」「mask 打掉具名條目」「traverse 斷一層」「Manager 寫的 spec 該帳號讀
  得到」「ACL 指名的是別人時不成立」。**真的以該 uid `open()` 需要第二個 UID／root，
  明確 `pytest.skip` 並寫出理由與替代驗收位置，不靜默通過。** 另補派工路徑（每個角色寫進
  自己那格、preflight 對「讀不到」fail-closed）與 `direct` 模式零回歸。
  `tests/test_trust_root_permgen_traverse_620.py` 的正向路徑改為逐 principal，
  reviewer 的那條鏈因此第一次被涵蓋。

  **部署影響（需 operator 動作）**：spool 落點與六份 unit 的 `Environment=` 同時改變。
  順序是**先權限、後 unit**——反過來會讓每個 job 以 78/CONFIG 收場。runbook 新增
  §5-3a（per-principal spec spool），含落檔驗證、mask 判準，以及**以各 job 身分實測
  讀得到自己的 spec／讀不到別人的**的正反向步驟。
