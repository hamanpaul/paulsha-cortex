### Fixed

- **repair 派工注入 bot review findings**：delivery journal 現在會保留 blocking review threads 的檔案/行號/摘錄，repair builder 的 commit-required prompt 會直接附上 needs-fix findings，避免 fix-round 在缺少 reviewer 上下文時盲修。
