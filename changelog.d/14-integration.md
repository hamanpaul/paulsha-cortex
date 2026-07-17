### Fixed

- 統一 `cortex work` umbrella routing：`show` 保持 read-only Monitor 查詢，其餘 lifecycle action 交給 Manager control queue，且共同 help 不再隱藏 mutation commands。
- `jobs.json/workflows` 成為唯一 lifecycle truth；public start/resume 會建立真實 `feature-oneshot` run，delivery 只保存 run-keyed ship journal，並由 canonical foreign review 綁定 exact builder/base、冪等建立 PR、把 `pr_ref` 與 CompletionRecord 寫回同一個 WorkflowRun。
