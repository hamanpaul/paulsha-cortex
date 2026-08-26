Release publication job 在沒有 checkout 的情況下，明確以
`gh release create --repo "$GITHUB_REPOSITORY"` 指定 remote repository；避免 gh 因找不到
`.git` 而在已通過 qualification gate 後失敗，transactional tag/release rollback 契約不變。
