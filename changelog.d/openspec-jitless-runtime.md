### Fixed

- **#716 openspec 在 jitless 下的執行環境**：qualification image 的系統層 node 改為 20.20.2（與 9900X 同版）。node 22 的 ESM facade 會列舉 `node:http` 的 lazy `WebSocket` export 並載入 undici 的 wasm，在 `--jitless` 下 openspec 一 import 就崩。Manager EnvironmentFile 加上 `DO_NOT_TRACK=1`，因為 openspec 的 telemetry 走 fetch→undici→wasm，同樣會在 jitless 下崩。contract 測試釘住 node 版本須 >=20.19 且 <22（#716）。
