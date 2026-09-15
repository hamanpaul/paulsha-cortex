# AGY reviewer schema 改寫成 Gemini 相容子集

#880 讓 AGY reviewer 帶入與 Claude 共用的 `--json-schema`，但 Antigravity 會把它轉成 Gemini function declaration，而 Gemini 只接受字串 enum、單一 `type`：`schema_version: {"enum": [1]}` 與 `line: {"type": ["integer", "null"]}` 直接被 `INVALID_ARGUMENT` 拒絕，新 pin 下 AGY reviewer 卡零成功。現在 AGY lane 先經 `_gemini_compatible_schema` 改寫（整數單值 enum → `minimum`/`maximum`、含 null 的 type 列表 → `nullable: true`），Claude 契約仍是唯一來源。

另外，agy 的 structured output 會在 canonical payload 外多掛 `toolAction`／`toolSummary` 兩個 CLI 中繼鍵，Manager 解析 terminal JSON 時只剝掉這兩個固定鍵，其餘多餘鍵仍 fail closed。

Gemini 也沒有 `additionalProperties` map 型別（模型會把 `authority_hashes` 的路徑鍵改寫成識別字），字串鍵 map 改以 `[{key, value}]` 陣列表達，Manager 在 review terminalize 前摺回 canonical mapping。
