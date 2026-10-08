---
status: accepted
work_item: readme-dangling-refs
domain_breadth: 0
state_consistency: 0
invariant_count: 3
artifact_classes:
  - documentation
---

# 清除 README 懸空引用（#1368）

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1368`；[spec](../../specs/readme-dangling-refs-spec.md)、[design](../../specs/readme-dangling-refs-design.md)。
- 只改 `README.md` 與 changelog；不動 `docs/**`、`.project-policy.yml`，也不新增 `.doc-drift-allow`。
- spec／design／本 todo 的文字是 pinned authority，只能勾選 checkbox；需要澄清時寫進 terminal reason。

## Tasks

- [ ] **T1 盤點**：在候選 worktree 執行 `env -u PYTHONPATH python3 -m policy_check --repo .`，在候選的 base 與目前 head 各記下 R-22 的總數，以及 README 每一個引用屬於 spec R1 或 R2 哪一類。
- [ ] **T2 改寫 repo 內路徑**：依 spec R1 把 README 中 repo 內檔案的引用改成完整路徑；`model-identities.yaml` 逐處判斷。
- [ ] **T3 標示 runtime 檔名**：依 spec R2 在指向 runtime 檔案的行尾加 `<!-- doc-drift-ignore -->`；同一行兩類並存時依 design D3 處理。
- [ ] **T4 驗收**：重跑 policy_check，R-22 輸出沒有 `README.md ->`，總數比 base 少了 base 上 README 的筆數；把兩邊的總數寫進 terminal reason。
- [ ] **T5 文件**：新增 `changelog.d/readme-dangling-refs.md`，並同步 `CHANGELOG.md [Unreleased]`。
