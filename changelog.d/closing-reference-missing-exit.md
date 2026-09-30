### Fixed

- **#1220 closing reference 缺席的出口**：merge 授權只剩 `closing-issue-missing`（GitHub 沒替 PR 建立 closing reference）時，不再以例外轉成 `resume-workflow-failed`，改為記錄結構化 needs_human `closing-reference-missing`（PR、缺少的 issue、head）。投影會提供 `resume`，提示在 PR 的 Development 手動連結 issue 後 resume，或在管線外合入後 `retire-delivered`；其他 reason 組合仍 fail-closed（#1220）。
