#556：`commit_policy=forbidden` 的 worktree-isolation 卡完成後不再提早綁定 Candidate；第一張允許 commit 的 builder 卡採信後才綁定 exact HEAD，讓 `recover-pre-candidate` 仍能在 candidate 尚未產生時使用。
