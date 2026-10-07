# #1338 已有 PR 的 repair build 被終止後可用 retry-build 恢復

`retry-build` 在 build phase 接受最新 builder job 為 terminal failure 且沒有已採信 evidence 的狀態。正式 admission、registry reset 與 recovery projection 共用此判準；保留 exact-Candidate CAS 與 Manager-owned receipt，並在 reset 後派出新的 builder job。原有 exit 0 但 evidence 未綁定的恢復路徑維持可用。
