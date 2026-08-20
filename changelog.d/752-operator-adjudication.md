# 752-operator-adjudication

- **`#752` verify 階段的人裁通道——`retry-card --reason` 落成 Manager-owned
  evidence、經 retry_context 進 prompt，reviewer 終於讀得到 operator 裁決。**
  實機連四輪：design D3/D8（fail-closed）與 todo／spec req 4/5（shareable
  default）矛盾，reviewer 只能 needs_human——design 被 planning authority 釘死
  不可 mid-run 修訂、builder 寫進 candidate 的註記依 #540/#628 不可採信、
  `review-attest` 只受理 review phase：**operator 已做的裁決沒有任何可信通道**
  （#717 的表親）。修法：`retry-card` 增列選填 `reason`（≤4000、前置驗證、
  無 durable state path 即拒），重置成功後寫 content-addressed immutable
  evidence `cortex-operator-adjudication/v1`（走既有 `_write_supersede_evidence`）；
  dispatch 端 `_operator_adjudications()` 讀回本 run 最近 ≤3 筆（mtime 排序、
  reason 有界）進 retry_context 的 `operator_adjudications` 鍵——builder 與
  reviewer 卡都吃。首派 prompt 逐字不變、採信端零改動、作者歸屬不變（bounded
  CLI＋Manager 落地，非 candidate 內容）。
