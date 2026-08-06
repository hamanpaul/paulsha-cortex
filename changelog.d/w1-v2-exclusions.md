### Fixed
- **-v2 重識別補 excludes 斷開舊識別的 source 認領**：舊識別的 superseded runs
  經 workflow metadata 仍 confirmed 認領 openspec／issue source，與 -v2 新連結
  形成 confirmed source collision → repo provider degraded、Manager 不得派工。
  比照 dispatch-reliability-batch 先例，舊 work_id 以空 links＋excludes 重登錄。
