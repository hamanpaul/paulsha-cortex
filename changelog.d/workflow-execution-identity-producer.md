### Added

- **#828 RED：** 新增 Cortex workflow status execution-identity regression coverage，鎖定 registry job 的 executor/model/card/job_id 綁定、in-flight 優先、retry 後 last execution、planned／unknown／缺值與跨 run/repo 隔離；本卡刻意保留 failing tests，待後續 producer projection 修正轉 GREEN。
