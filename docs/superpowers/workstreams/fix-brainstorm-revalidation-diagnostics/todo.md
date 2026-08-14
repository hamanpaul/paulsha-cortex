---
status: accepted
work_item: fix-brainstorm-revalidation-diagnostics
---

# fix-brainstorm-revalidation-diagnostics Todo

`#514`：`_validated_brainstorm_planning_authority()`（`coordinator/manager.py:2695-2696`）對已發佈 artifact
的重新驗證若判定不合格，拋出的例外**不含路徑、不含原因**：

```python
if not assess_planning_artifact(PlanningArtifact(kind=kind, ref=ref, text=text)).accepted:
    raise ValueError("workflow brainstorm artifact is not accepted")
```

operator 只看得到一句 `workflow brainstorm artifact is not accepted`——不知道是迴圈中的哪一個 artifact，
也不知道是 `status-not-accepted`／`required-section-missing`／`blocking-decision` 三種判準中的哪一種。

這是 `#511`（已由 PR `#513` 在 `manager.py:6023` 的首次寫入路徑修正）的同類未修處。差別在觸發時機：
本處是 workflow 續跑時對**已持久化 artifact** 的重驗，命中情境包含 artifact 在磁碟上被改動、或先前以
較寬鬆規則通過的舊 artifact 在規則收緊後不再合格——這類情境對 operator 更難自行推理。

## Tasks

- [ ] 例外訊息帶上 `ref`（指出是哪一個 artifact）與 `assessment.reasons`，`blocking-decision` 時附 marker 行號
- [ ] 沿用 `#513` 建立的 `cortex-planning-artifact-rejection/v1` evidence 落檔（`<coordinator_root>/evidence/planning-artifacts/`），讓重驗失敗同樣留下可查的完整內容
- [ ] 訊息欄位順序比照 `#513`（reasons → markers → evidence path），確保在上游 `planning.py:1165` 的 `str(exc)[:160]` 截斷後關鍵資訊仍存活
- [ ] 測試涵蓋三種 reason 在重驗路徑上的訊息內容，並鎖住「訊息含 ref」
