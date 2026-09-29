### Added

- **#716 canary job 診斷**：deployment canary 派工以 failed／needs_human 終局或逾時時，driver 先把最近幾個 job unit 的 journal 尾端與 gate.log 尾端印到 stderr（有界、遮蔽 credential 形狀），讓 `gate-spool-empty` 這類只指向 `journalctl -u <unit>` 的失敗在容器銷毀前留下證據（#716）。
