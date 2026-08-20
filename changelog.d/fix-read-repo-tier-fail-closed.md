# fix-read-repo-tier-fail-closed

- **#492：repo policy 的 `tier` 缺失或非法時，在 deck、doctor、readiness、delivery preflight、workflow 與 slice builder dispatch 前以指出 manifest 路徑與允許值的診斷 fail-closed。** canonical 或 legacy manifest 存在時 `tier` 必填；未有任一 manifest 的 repo 維持 `shareable` 預設。production claim 與 direct dispatch 現在實際經過相同 checkpoint，repo-root resolution error 不再誤標為 tier config。
