# fix-read-repo-tier-fail-closed

- **#492：repo policy 的 `tier` 缺失或非法時，在 workflow 與 slice builder dispatch 前以指出 manifest 路徑與允許值的診斷 fail-closed。** 未有 manifest 的 repo 維持 `shareable` 預設；canonical manifest 存在時 `tier` 必填。
