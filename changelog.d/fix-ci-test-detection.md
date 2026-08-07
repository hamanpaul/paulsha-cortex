### Fixed
- **CI 測試閘門形同虛設（tests.yml 偵測誤判）**：偵測式 `ls tests/test_*.py tests/*_test.py`
  只要任一 glob 沒配到就回傳非零，本 repo 有 156 個 `test_*.py`、0 個 `*_test.py`，
  因而恆判為「無測試套件」，Setup Python／Install／Run test suite 三步全被 skip，
  job 卻仍回報 success——自 repo 初始 commit 起所有 PR 的 pytest 綠燈都是假的。
  改用 `find -print -quit` 偵測，兩種命名慣例皆可辨識且無測試時仍正確跳過。
- **測試套件在 Python 3.10／3.11 無法 parse**：`tests/test_coordinator_manager.py` 與
  `tests/test_coordinator_candidate_verification.py` 的 `_persona_catalog` 在 f-string
  表達式內嵌含反斜線的 f-string，PEP 701（3.12）之前不允許，導致宣稱支援 3.10 的專案
  在該版本連 collect 都失敗。改為先組好字串再內插，輸出等價。
- **openspec 整合測試在缺 CLI 時硬失敗**：`tests/test_openspec_archive_purpose.py`
  依賴 npm 套件 `@fission-ai/openspec`，不在 Python 依賴樹內；改為 `skipif` 明確標示
  並附原因，取代 `assert shutil.which(...)`。
