### Fixed

- **#716 qualification image 的系統層 node**：reference image 不再以 apt 安裝 Ubuntu 24.04 的 nodejs 18.19，改由 contract 釘住版本與 sha256 的官方 Node.js 22 tarball 裝到 toolchain wrapper 寫死的 `/usr/bin/node`。openspec 要求 node >=20.19，舊版下 ship lane 的 `openspec validate` 直接 SyntaxError，archive gate 判 `canonical-specs-invalid`（#716）。
