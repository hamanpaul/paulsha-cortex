# #1030 retry-card porcelain selector

新增 `cortex run work retry-card --card`，在 request 前限制 `--card` 僅供 retry-card 使用，並拒絕 `--payload` 以不同 card 靜默覆寫明示 selector。保留 payload-only card workaround；retry-card Manager allowlist 與其他 admission checks 不變。
