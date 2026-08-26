- 修正 Phase 2 generated-vs-installed attestation：shim／toolchain wrapper shebang 漂移
  現在會 fail closed，polkit 的完整獨立 JavaScript 註解只產生 comment-only warning，
  未閉合 block 與 `;` 規則仍 fail closed。Deployment canary 固定 Codex builder，綁定
  Manager-owned job spec，且只接受 `worktree-isolation` 真實 command event 的 hash-only
  observation；同步把 #681/#695 對齊現行 installer/RC authority，#716 在 live success 前
  保持 open，舊手工 Phase 2b runbook 已由 transactional installer runbook 取代。
