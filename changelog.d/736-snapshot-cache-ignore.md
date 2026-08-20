# 736-snapshot-cache-ignore

- **`#736` gate snapshot 依名跳過可再生快取目錄，寫入卡不再結構性必死**——
  builder job unit 掛 `UMask=0077`，pytest 產生的 `.pytest_cache/` 以 mode 0700 落地，
  POSIX ACL 的 mask 取自 create mode 的 group bits ⇒ `mask::---` ⇒ default ACL 給
  `cortex-gate` 的 `r-x` 繼承了條目、繼承不到權限。`snapshot_worktree()`（#629）是
  裸 `shutil.copytree`，一顆讀不到的快取目錄就 `SnapshotError` ⇒ 依設計不寫 ledger
  ⇒ `gate-spool-empty`、gate unit 以 exit 74 crashloop。tdd-red 實測命中（run
  `workflow-85114100` 第九環）；worktree-isolation 會過純因唯讀卡不產生檔案——
  **任何會在工作樹跑 pytest 的寫入卡在此剖面下必炸**。修法：新增
  `SNAPSHOT_REGENERABLE_CACHE_DIRS`（`__pycache__`／`.pytest_cache`／`.mypy_cache`／
  `.ruff_cache`，皆為 .gitignore 排除、gate 重跑時 pytest 會自行重建的快取）於
  copytree 依名跳過、任意深度；**清單外的不可讀項目維持 fail-closed 不變**——
  「跳過讀不到的東西」是禁手，候選內容讀不到時靜默跳過會讓 gate 在殘缺的樹上判出
  假 verdict。mutation 驗證：清空清單，skip 測試必轉紅。`#723` 環境可攜性一族的
  第三個實例（umask×chmod `#724`、帳號名×tmp 路徑 `#734`、本票 umask×ACL mask）。
