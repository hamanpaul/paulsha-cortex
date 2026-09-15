---
status: accepted
work_item: fix-monitor-fs-event-convoy
---

# fix-monitor-fs-event-convoy Tasks

All tasks listed here are pre-archive work for this card.

- [x] Add the #879 RED regression coverage for bounded filesystem-event refresh work.
- [x] Implement the single monitor refresh worker, take-and-clear pending merge, and
      monotonic thread-warning throttle.
- [x] Run the focused monitor and scan-health regression tests successfully.
- [x] Merge `origin/main` and revalidate the monitor repair against the updated baseline.

OpenSpec archive, pull-request merge, issue closure, and the final done state are
performed by the Manager after this pre-archive card completes.
