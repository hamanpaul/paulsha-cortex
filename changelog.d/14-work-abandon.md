feat(work): 新增 Manager-owned `work abandon`，以 exact WorkflowRun CAS 與 immutable reason evidence安全淘汰無delivery side effect的舊run，不冒充done或建立CompletionRecord。
