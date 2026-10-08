---
status: accepted
work_item: readme-dangling-refs
---

# 清除 README 懸空引用規格

## Requirements

對應 [#1368](https://github.com/hamanpaul/paulsha-cortex/issues/1368)。policy_check R-22 在 main（`37dc700c`）回報 159 個早就存在的懸空引用，其中 39 個在 `README.md`。

1. **R1 repo 內檔案改成完整路徑**：README 裡指向 repo 內檔案、但只寫了檔名或部分路徑的引用，改寫成從 repo 根目錄起算的完整路徑。2026-10-08 盤點到的對應如下，實作時以當下的 `git ls-files` 為準：
   - `manager_daemon.py` → `paulsha_cortex/coordinator/manager_daemon.py`
   - `release.yml` → `.github/workflows/release.yml`
   - `persona-scope.yml` → `.github/workflows/persona-scope.yml`
   - `SKILL.md` → `skills/driving-cortex/SKILL.md`
   - `refine-requirements-v1.json` → `docs/superpowers/specs/refine-requirements-v1.json`
   - `service-manager.sh` → `paulsha_cortex/scripts/service-manager.sh`
   - `data/execution-adapters.yaml` → `paulsha_cortex/coordinator/data/execution-adapters.yaml`
   - `deck/schema.py` → `paulsha_cortex/deck/schema.py`
   - `config/runtime.py` → `paulsha_cortex/config/runtime.py`
   - `personas.yaml` → `paulsha_cortex/persona/personas.yaml`
   - `model-identities.yaml`：README 裡有些地方指 repo 內的 packaged 檔 `paulsha_cortex/coordinator/data/model-identities.yaml`，有些指 operator 的 overlay（`~/.agents/config/paulsha/model-identities.yaml`）。依上下文逐處判斷：指 packaged 檔的改成完整路徑，指 overlay 的依 R2 處理。
2. **R2 runtime 檔案標示為非 repo 路徑**：指向 runtime 產生、不在 repo 裡的檔案，保留原本的說明，在該行行尾加上 `<!-- doc-drift-ignore -->`。這是 R-22 承認的唯一行內標記，只承認 HTML 註解形式。目前盤點到的有 `jobs.json`、`project-cortex.yaml`、`project-hippo.yaml`、`paulshaclaw.yaml`、`qualification.json`、`model-eval-roster.yaml`、`verify-attest.json`、`review-attest.json`、`skill_park.json`、`quota-pools.json`，以及帶 `~/` 或 `PSC_PROJECT_CONFIG_ROOT/` 前綴的路徑。不新增 `.doc-drift-allow`。
3. **R3 意義不變**：除了 R1 的路徑改寫與 R2 的行尾標記，README 的文字內容不變。若發現某個引用的檔案已經真的不存在，就修正或刪除那句描述，並在 terminal reason 列出。
4. **R4 驗收方式**：在候選 head 上執行 `env -u PYTHONPATH python3 -m policy_check --repo .`，R-22 的輸出不再包含任何 `README.md ->` 項目。R-22 只列前 20 筆，所以要比對總數：在候選的 base 上與候選 head 上各跑一次，head 的總數要比 base 少了 base 上 README 的那些筆數（2026-10-08 盤點時是 39 筆）。不能只看前 20 筆裡有沒有 README。

## Boundary

- 只改 `README.md`，以及必要的 changelog。不動 `docs/**`、不動 `.project-policy.yml`、不新增 `.doc-drift-allow`。
- spec／design／todo 的文字是 pinned authority，只能勾選 checkbox。

## Evidence

2026-10-08 在 `origin/main`（`37dc700c`）以完整列出的方式執行 R-22：159 筆懸空引用，README 39 筆，分類見 R1 與 R2。
