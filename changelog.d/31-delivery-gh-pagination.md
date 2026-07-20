fix(delivery): 改用 `gh api --paginate --jq '.'` JSONL page stream，相容不支援 `--slurp` 的 gh 版本並維持 malformed page fail-closed。
