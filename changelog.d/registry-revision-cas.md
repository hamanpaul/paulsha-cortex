- `jobs.json` 現在以 exact durable-byte SHA-256 revision 與 canonical
  `jobs.json.transaction.lock` sidecar 做 compare-and-persist；stale writer
  會明確回 `RegistryRevisionConflict`、完整重載 durable snapshot，v1 migration /
  verification-hash normalization 也納入同一 CAS 邊界，daemon request queue
  會把這類衝突持久化成 error done 而不是造假成功。
