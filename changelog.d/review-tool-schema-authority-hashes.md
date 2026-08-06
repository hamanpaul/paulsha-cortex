### Fixed
- **Issue #315 補遺 3：review StructuredOutput 工具 schema 開放 authority_hashes**：
  manager 驗證器在 input snapshot 含 planning-authority 列時要求
  `authority_hashes`，但 `_claude_review_json_schema` 的
  `additionalProperties:false` 未含此屬性——模型遵循 prompt 也交不出來（工具
  層拒收），review terminal 恆 schema invalid（#219 attestation 佈線缺口）。
  工具 schema 開放屬性（sha256 pattern），必填與精確比對仍由 manager 依
  context 驗證。
