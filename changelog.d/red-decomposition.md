#833：Red run 會派出唯一的拆分 planner，並在計畫通過既有 plan review gate 後，透過標準 work-action intake 接續 child workflow；intake 未受理時 parent 會停在 `needs_human`。
