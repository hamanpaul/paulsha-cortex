---
type: fix
scope: launcher
---
codex launcher 對 `gpt-6-luna` 明示 `model_reasoning_effort="max"`（比照 `gpt-5.6-luna`），避免 run-scoped 指定
`codex/gpt-6-luna` 當 builder 時落回 host `~/.codex/config.toml` 的 ambient effort。
