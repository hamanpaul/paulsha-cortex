### Fixed

- **#718：template job 的 canonical log slot 與 unit `%i` 先前分別吃 raw
  `slice_id` 與 `job_segment(slice_id)`，長 id 會把 builder／reviewer 卡在
  `226/NAMESPACE`。** launcher 現在把 `template_plan.instance` 當成 spec
  `instance` 與 job log parent 的唯一 slot 名，而 raw `slice_id` 只留在 explicit
  `control_log_path`／dispatch anchor；因此模板 unit、canonical log 與 completion
  controls 不再彼此漂移。
