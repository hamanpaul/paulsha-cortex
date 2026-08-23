# trust-root-home-fail-closed

- **#692：downgraded job 的 HOME 契約改為 fail-closed**——launch 前拒絕 missing/blank/relative/symlink/wrong-owner HOME，PATH+HOME 雙缺會一併點名，shim 也不再回退到 unit/daemon HOME，且 HOME `lstat` 診斷不再以 chained traceback 洩漏路徑。
