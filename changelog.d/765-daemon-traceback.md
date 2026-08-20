# 765-daemon-traceback

- **`#765` 補遺（#511 家族）：`_log_error` 於 signature 首次出現時附完整
  traceback。** 「只有 str(exc)」讓每個新失敗面都要靠實機逐層猜（0820 實測
  claim_key 綁定失配的 raiser 路徑追蹤一小時未果）。重複出現維持既有抑制。
