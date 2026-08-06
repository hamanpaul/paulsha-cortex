### Fixed
- **-v2 excludes 收窄至 openspec ref**：連 github_issue 一併 exclude 會令舊識別
  的 work authority 全滅，abandon 無從尋址（confirmed work authority missing）；
  issue 的 workflow-metadata＋新 yaml 雙認領本不構成 collision，僅 openspec ref
  需要 exclude。
