# 742-reviewer-sandbox-handover

- **`#742` reviewer sandbox 交接——三分部署下 verify／review 卡終於派得出去**
  （#710 的 reviewer lane 版）。`review-sandboxes` 是 verify 首走才被 mkdir 出來的
  pool（`0700 cortex-manager`、零 ACL、不在部署清單），reviewer principal 一個
  inode 都讀不到，`cortex-reviewer-job@` unit 秒死於 shim 的 Permission denied
  （exit 78）。宣告的 `inherited-default-acl` reach 模型對它不成立：Manager unit
  掛 `UMask=0077`，default ACL 的繼承會被 create mode 的 group bits 把 mask 歸零
  （#736 在 gate 快照上的同一個交互）。修法：容器收斂 `0701`（traverse 不可列，
  `dispatch-worktree-pool` 先例）；per-job sandbox 建好之後由 owner（Manager）
  顯式 `setfacl -R u:<reviewer>:rwX`＋default 同值——走 #710 的同一支
  `grant_workspace_acl`（mask 由 setfacl 重算），帳號由
  `resolve_job_account(role=review)` 單一導出（#657）。帳號不在 passwd 時整支
  略過（direct 模式零回歸）。pool 根授權範圍不變、Manager 保留 owner ⇒ 回收
  （`_discard_reviewer_sandbox`）不受影響。
