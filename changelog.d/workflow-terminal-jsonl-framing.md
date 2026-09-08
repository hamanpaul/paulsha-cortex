# Terminal JSONL framing 實作

- 以 newline-preserving UTF-8 reader 與 literal LF record framing 修正 Manager
  terminal JSONL 對 CRLF 與 Unicode JSON string data 的處理，保留既有 terminal
  carrier、schema 與 fail-closed 邊界。
