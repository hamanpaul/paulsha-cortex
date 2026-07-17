### Fixed

- 統一 `cortex work` umbrella routing：`show` 保持 read-only Monitor 查詢，其餘 lifecycle action 交給 Manager control queue，且共同 help 不再隱藏 mutation commands。
