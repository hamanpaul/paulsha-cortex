### Fixed
- **malformed workflow build 卡改走可重試 recovery**：Manager 現在會把 malformed 的 passed terminal（含 build candidate 缺失）辨識為可重派卡片，避免 operator resume 永久卡死，並在 prompt 明示 build/plan candidate 的回報契約。
