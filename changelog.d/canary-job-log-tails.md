### Added

- **#716 canary 診斷帶 workflow job log 尾端**：派工失敗時另印最近 3 顆 workflow job 自己的 log 尾端（有界、遮蔽 credential 形狀）；unit 以 exit 0 結束卻沒交付 terminal JSON 時（run 36652684030 的 agy verification），journal 只有啟停兩行，原因只在 job log 裡（#716）。
