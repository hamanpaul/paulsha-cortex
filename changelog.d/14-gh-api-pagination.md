fix(monitor): 改用 `gh api --paginate --jq '.[]'` JSONL entity stream，相容不支援 `--slurp` 的 gh 版本，避免 GitHub provider 永久 degraded。
