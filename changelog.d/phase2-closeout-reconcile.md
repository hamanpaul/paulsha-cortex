- 修正 Phase 2 generated-vs-installed attestation：shim／toolchain wrapper shebang 漂移
  現在會 fail closed，polkit 的獨立 JavaScript 註解只產生 comment-only warning；同步把
  #681/#695 對齊現行 installer/RC authority，並將 #716 明確保留為發布後 canary 驗收。
