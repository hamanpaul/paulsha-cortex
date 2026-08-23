### Fixed

- **#681：Copilot builder 現在固定走 pinned toolchain wrapper，toolchain plan/probe 會清掉外層 PATH/HOME、拒絕 symlink/traversal，並以部署樹 version metadata 對帳。**
- **#681 repair:** split-UID trust-root ACL integration tests now pick a POSIX-ACL temp root before falling back to `TMPDIR`, so gate environments with noacl temp mounts skip honestly instead of failing with `setfacl ... Invalid argument`.
