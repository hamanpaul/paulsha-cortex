---
type: feat
scope: coordinator
---
#871 maintainer fallback authorization 改用 content-addressed v2 merge authorization：同一 run/head 若已存在 Copilot v1 授權，maintainer fallback 會保留 immutable v1 為 superseded 稽核，另建以 payload digest 定址的 v2 並在 payload 內綁定 v1 ref/hash；replay 會驗證 superseded v1 wrapper 不可變且身分一致，`merge-authorized` 前置檢查與 trusted evidence refs 仍維持既有 fail-closed 邊界。
