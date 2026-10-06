### Fixed

- **#1282 adoption review follow-up**：sealed-tree mismatch 現在會讓 capture／rollback wrapper 回傳失敗並阻止後續 root CLI；recovery PATH 補上 `/usr/sbin` 與 `/sbin`；`home-top` 自動 quarantine 限於 plan-managed principal HOME，egress overlay HOME 的未知頂層項目維持 `unclassified`。runbook 說明 RC release 首次安裝透過 installer 建立帳號；服務在 lease 前已停止的解析與還原仍由直接執行 runbook 的回歸測試涵蓋（#1282）。
