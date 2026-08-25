### Fixed

- **#501：分離 slice 的 verification contract hash 與 execution evidence hash。**
  `verification.hash` 現在只由 create/repin pin contract；verification result 與 status
  evidence 改寫入 `current_verification_evidence_hash`，避免下一輪 pinned-input 檢查把
  正常 evidence 誤判成 contract drift。既有缺少新欄位且已被覆寫的 `jobs.json` slice row
  會依 persisted contract 與 legacy digest 在 registry load 時修復，無法判定的值仍
  fail-closed。
