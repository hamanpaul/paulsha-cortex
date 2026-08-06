### Fixed
- **-v2 issue links 暫撤（abandon 尋址窗口）**：issue 被新舊識別 contested 時
  authority 判 ambiguous，舊識別 abandon 無從尋址。暫撤 -v2 的 github_issue
  links 使舊識別 uncontested 可 abandon；隨後 PR 還原 links 並補舊識別 issue
  excludes（正確時序：abandon 先於 exclude）。
