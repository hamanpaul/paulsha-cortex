---
status: accepted
work_item: feat-work-gc-v2
---

# feat-work-gc Todo

## Tasks

- [ ] 將 issue #178、active OpenSpec change `2026-08-04-feat-work-gc` 與本 Todo 綁定為同一 confirmed Work Item。（未執行：需 manager/daemon `cortex work link`，本次實作在隔離 worktree 內進行，未觸碰 `~/.agents` 真實狀態）
- [x] 以 TDD 完成 #178：先寫 RED 測試（squash-merge 判 merged、unmerged 絕不進 apply 清單、dirty worktree 保留、closed-unmerged PR 分支保留），再實作到 GREEN。（本次由 Claude/Sonnet 直接實作，非 coordinator 派工 copilot/gpt-5.4；RED 已確認 ImportError 失敗，GREEN 後 14+ 支測試全過）
- [ ] ForeignReview（claude／sonnet）review 通過；operator 驗收核可。
- [x] `changelog.d/feat-work-gc-v2.md` fragment 已新增且已 commit（R-09 硬性 gate）；`CHANGELOG.md [Unreleased]` 有對應 entry。（檔名依分支 slug 用 `-v2`，與本節原述的 `feat-work-gc.md` 不同）
- [x] 帶 PR 上下文執行 `policy_check`（`--pr-title`／`--pr-body`／`--pr-labels`／`--pr-base-ref`／`--pr-head-ref`）確認 fail: 0；全套 pytest 通過。
