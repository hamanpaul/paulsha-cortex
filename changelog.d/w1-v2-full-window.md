### Fixed
- **abandon 尋址窗口放寬至全額認領**：abandon 校驗 run refs 與當前 authority
  全等；舊識別的 openspec exclude 使 refs 永遠 differ。窗口期撤 openspec
  excludes 並暫撤 -v2 的 openspec links（維持無 collision），abandon 後由
  終態 PR 一次還原。
