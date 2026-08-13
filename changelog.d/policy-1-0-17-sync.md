---
type: change
scope: policy
---
同步 hamanpaul project policy 1.0.15 → 1.0.17：`.project-policy.yml`、`Policy Check` workflow（`uses:` 與 `policy_engine_ref` 雙重釘選至 `9e7fabbf0b5eea9ad933fa6798764b723934a0b7`，尾註 `# v1.0.17` 供 R-23 對齊）、canonical `CLAUDE.md`（symlink `AGENTS.md`／`GEMINI.md`／`.github/copilot-instructions.md` 自動跟隨）與 `README.md`／`CHANGELOG.md` 內的版本引用全數同步。1.0.16／1.0.17 對下游 repo 未新增或變更任何規則；其中 1.0.16 引入的引擎版本 gate（執行中引擎與 repo 宣告 `policy_version` 不符即 fail-loud）是本次同步的實益。連帶依 R-19（1.0.16 起結構化偵測）移除 `tests.yml` 的條件式測試 gate（`Detect test suite` skeleton），pytest 改為無條件執行，消除 detect 誤判導致測試套件靜默 skip 成綠的風險。
