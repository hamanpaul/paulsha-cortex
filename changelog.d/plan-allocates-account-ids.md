### Fixed

- **#1286 帳號 uid／gid 改在 plan 時決定，不再預設撞 Ubuntu 的 991–995**：release 的 install config
  不再寫死五個 cortex 帳號的 uid／gid（`qualification/write_install_config.py`；config 的 `uid`／`gid`
  改為選填，宣告了照舊採用）。`install trust-root plan` 經 NSS 讀 passwd／group 快照，逐帳號逐欄位決定
  號碼：host overlay 宣告的（`overlay`）或 config 自己宣告的（`config`）優先；主機已有同名帳號／群組就
  沿用現有號碼（`existing`，升級與重跑 plan 走這條，帳號 step 因此不再需要 overlay）；都沒有才自動配號
  （`allocated`：從 989 往下找第一個 uid 與 gid 都空著的號碼、uid＝gid，限 system 範圍 100–989，避開
  發行版慣用的 990–999；每個候選號碼另以 NSS 單點查詢 `getpwuid`／`getgrgid` 確認，sssd／LDAP 這類
  不列舉的來源占用的號碼也會略過）。同一份快照得到同一組號碼，plan sha 穩定；完全宣告號碼的 config
  不讀主機。
- plan 多一個 `account_id_sources` 欄位記錄每個帳號 uid／gid 的來源；`plan` 的輸出與 `verify` evidence
  多一個 `account_ids`（號碼＋來源）供審核。account step 的形狀不變；沒有 `account_id_sources` 的
  v0.1.13 plan／receipt 照舊可讀可套用。
- 升級範圍（owner 裁決）：帳號 step 不再需要 overlay。從未用過 overlay 的主機（例如 v0.1.13 以 991–995
  安裝的）`cortex upgrade` 不需要 overlay，帳號 step 與 prior receipt 相同；曾綁定 overlay 的主機（含
  adoption 主機）必須保留同一份持久 `/var/lib/cortex-installer/host-overlay.yaml`，`cortex upgrade`
  照舊比對它的 digest，檔案不在或內容不同就拒絕（`upgrade.py` 未改）。
- apply preflight 在任何變更前重新確認：`allocated` 的號碼仍沒有被其他帳號或群組占用，`existing` 的
  號碼仍屬同名帳號；不符就 fail closed 並要求重新產生 plan。號碼屬於同名帳號（重跑同一份 plan）仍交給
  既有的 receipt provenance 判斷；overlay 指定的號碼被占用時照舊報錯。legacy adoption 規則不變。
- RC release／deployment-canary profile 在首次 plan 前先以模擬的 Ubuntu 身分（`systemd-resolve`、
  `render`、`kvm`、`sgx`、`input`）占用 uid／gid 991–995，並在配號範圍內只占 uid 989、只占 gid 988；
  首次 plan 的號碼必須全為 `allocated`、不在 990–999、也不等於這些占用的號碼。
  `qualification/validate.py` 不再寫死 991，改以 install verification 的 `account_ids` 對照
  Manager／Monitor／egress 的 service 身分，要求升級後的 plan 全部沿用（`existing`），且號碼不等於 RC
  占用的號碼。新版 validator 要求 install verification 帶 `account_ids`：本版之前產生的 canary
  evidence 要用與 evidence 同版的 checkout 驗證。
- runbook：`trust-root-transactional-install.md` §2 補上號碼來源規則與 overlay 只在要指定號碼時才需要；
  `trust-root-legacy-adoption.md` 同步說明帳號 step 不再依賴 overlay、但 overlay 仍須保留。
