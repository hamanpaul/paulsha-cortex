---
type: fix
scope: installer
---

`cortex install service` 對既有 `project-cortex.yaml` 採 append-only workspace
遷移，替換前建立 `.bak-*` 備份；不可載入的既有 project config 或 model identity
registry 會拒絕覆寫，跨 HOME 的 default agents root 需以 `--agents-root` 明確放行。
命中既有 workspace 時逐字保留 operator 條目與其他設定區塊，migration rollback
會清理本次建立的 `.bak-*`；porcelain install 也會保留 installer 正常返回時產生的
stderr 診斷訊息。
