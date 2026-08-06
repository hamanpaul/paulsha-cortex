### Fixed
- **Issue #315 補遺 2：review 派工 schema 把 authority_hashes 列入 fixed 逐字照抄**：
  sonnet reviewer 對「actually opened」措辭的條件性解讀會整組省略
  `authority_hashes`（實測 2/2），review terminal 恆 schema invalid。expected
  值由 manager 原樣提供並列入 fixed；harvest 端精確比對不變。
