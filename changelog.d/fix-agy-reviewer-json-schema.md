# AGY reviewer JSON schema 修正

AGY reviewer 回傳 verification terminal 時，`details` 可能是字串，導致 Manager 直接判定 schema invalid；根因是 reviewer lane 沒有綁定與 Claude 共用的 `--json-schema`，且 Manager 沒有處理這個相容形狀。現在 AGY reviewer 會依 terminal kind 傳入共用 schema，Manager 也只將非空字串正規化為 `{"text": ...}`，空字串仍維持拒收。
