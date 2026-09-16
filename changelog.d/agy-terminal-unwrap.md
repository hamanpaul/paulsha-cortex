- **#807 agy headless terminal 改走 JSON envelope 並剝除前導文字**：`build_agy_argv` 對
  planner／reviewer／verifier／builder 所有 agy headless 形態一律附加 `--output-format json`，
  讓 terminal 證據落成單行 JSON envelope，而非 Antigravity 預設多行 pretty-print 的 text
  輸出；Manager `_extract_terminal_json` 與 planning_runtime `_ENVELOPE_KEYS` 新增接受
  agy envelope 的 `response` 鍵。`_parse_terminal_json_text` 容忍尾端 json code fence 後的
  換行、只在 fence 為回應**尾綴**時剝殼（內嵌範例 fence 仍不是 terminal 證據），並在 agy
  前綴進度文字（如「Waiting for tests...」）時只採信回應**尾端**的完整 terminal payload
  （任意內嵌 JSON 仍拒絕）。status enum 別名（`verified`→`passed`）與 rate-limit 時的同
  identity 重試不在本次範圍，仍留在 #807。
