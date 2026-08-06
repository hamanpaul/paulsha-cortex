### Changed
- **W1 canary v2 檔名對齊（#295／#291）**：build 卡 declared inputs 以 `*<work_id>*`
  glob 檔名，v2 重識別僅改 frontmatter 導致 `workflow declared input missing`。
  將 spec／design／plan 檔名與 workstream 目錄補上 `-v2`，並同步 work-items.yaml
  與文內交叉引用。
