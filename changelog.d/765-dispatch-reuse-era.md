# 765-dispatch-reuse-era

- **`#765` 補遺：dispatch 的 reuse／retry 判定改走同 era 的 `reusable` 子集——
  第五個（真正的）出口。** `_dispatch_workflow_card` 的 `if matching: return
  matching[-1]` reuse 決策 era-blind，authority restart 後把前代 era 的 terminal
  （verification-38）回傳供 harvest，advance 的 binding 對現 era 必炸——resume 與
  retry-card 兩處過濾後仍炸的最後出口（traceback 實證此路徑）。`matching` 本身
  維持全 era（retry-context／sandbox 清理需要完整歷史）。
