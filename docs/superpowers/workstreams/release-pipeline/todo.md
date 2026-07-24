---
status: accepted
work_item: release-pipeline
---

# release-pipeline Todo

## Tasks

- [ ] 將 issue #95、active OpenSpec change `release-pipeline` 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] coordinator 派工 copilot（gpt-5.4）完成 tests.yml matrix 擴充、build/smoke-install job、`release.yml` 新增；因檔案落點在 `.github/workflows/**`，派工時需明確告知 persona shadow 模式例外範圍。
- [ ] ForeignReview（claude/sonnet，需知悉 `.github/**` 例外寫入範圍）、自動 push/PR、bot review、merge 與 archive 閉合。
- [ ] 新 workflow 全綠；release dry-run（或等效驗證）產出的 wheel/sdist 內容與版本號正確；smoke-install 於乾淨環境安裝並執行 `cortex --version`/`--help` 通過。
