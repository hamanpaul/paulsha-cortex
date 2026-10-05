# Trust Root 一鍵升級 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 owner 只下一個 root 指令 `sudo /opt/cortex/venv/bin/cortex upgrade <版本>`，工具就依序完成 release ingress → plan → apply → 憑證 handoff → activate → verify → loaded↔installed 核對；任何失敗都自動回到前一版，最後輸出 upgrade report（#1263）。

**Architecture:** 已安裝的 CLI 以新模組 `paulsha_cortex/trust_root/install/upgrade.py` 擔任協調器。它在 process 內完成 ingress 驗證、版本與 receipt chain 判定、以非 root 身分產生 plan 並綁定 plan sha、發布 durable plan，並直接使用 `cli._maintenance_lease`／`_service_snapshot`／`_stop_current_services`／`_restore_snapshot_services`。所有 receipt mutation（apply、`credentials inherit`、activate、verify、rollback）都透過**封存後的 candidate CLI** 子程序執行，並帶上 lease token，與 runbook 手動流程相同（spec §9）。core 新增 prior-receipt 憑證 handoff；`release_ingress.py` 逐項移植 runbook §1／§2；`receipt_chain.py` 依 parent link 定位生效中的 receipt；`loaded_runtime.py` 承接原本放在 qualification driver 的 loaded↔installed 比對。

**Tech Stack:** Python 3.10+ 標準函式庫（urllib、tarfile、hashlib、fcntl、signal、pwd）、PyYAML、pytest、bash（`qualification/run.sh`）、Docker（RC qualification）。

**Spec:** docs/superpowers/specs/2026-10-05-trust-root-one-command-upgrade-design.md

## Global Constraints

- 指令介面：`cortex upgrade <version> [--wait-idle <秒>] [--json]`、`cortex upgrade --recover`、`cortex upgrade --status`；完整路徑名稱是 `cortex install trust-root upgrade`，`cortex upgrade` 是它的別名。
- 執行者必須是 root。不是 root 就立刻失敗，不做任何網路或檔案操作。
- `<version>`：SemVer，例如 `0.1.13`，對應 tag `v<version>`；比較時只接受 `MAJOR.MINOR.PATCH`，不引入 `packaging` 依賴；版本由 plan 的 `candidate.wheel.path` 檔名解析。
- 只能升級：目標版本必須嚴格高於 current receipt 的版本。降版請用 installer 的 `rollback`，或依 runbook 手動處理。
- `--wait-idle`：還有在飛 job 時，最多等待的秒數。預設為 0，也就是有在飛 job 就直接拒絕並說明。
- 正式部署一律從 GitHub 公開 repo `hamanpaul/paulsha-cortex` 抓取（HTTPS、不需 token），expected 值**一律來自 REST metadata**，不由下載後的檔案自己產生。spec §12.5 取代 §3 的 `--repository`：本計畫不提供該參數。
- 僅限測試的 `--release-source <dir>`、`--allow-same-version`、`--prior-receipt <path>` 只有在環境變數 `PSC_UPGRADE_QUALIFICATION=1` 明示時才接受，否則參數直接被拒。
- 下載目錄與每一層 ancestor 都必須 root-owned、不可由 group／other 寫入、不可有 symlink；驗過 qualification manifest 後才導出 bundle hash、檢查 archive topology、解壓，再驗 bundle 列出的每一個檔案；用 wheelhouse 離線建立封存的 candidate venv（`--copies`）。candidate code 只有在 digest 全部驗過、封存之後才會被執行。
- plan 以封存的 candidate CLI 在非 root、空白環境中產生；CLI 回報的 plan sha 與 plan 檔實際的 sha256 不一致就停止；plan 以 sha 命名寫入 `/var/lib/cortex-installer/plans/`。
- host overlay 固定為 `/var/lib/cortex-installer/host-overlay.yaml`，升級不更改 overlay；upgrade 讀 overlay 時去掉 `legacy_adoption` 區塊再產生 plan（digest 不變）。
- 本計畫的設計決策（spec 未明定）：overlay digest 必須等於 current receipt plan 的 `host_overlay_sha256`，只有 `--allow-same-version`（僅 RC）放寬，因為同版同 overlay 的 plan 與 prior 完全相同，installer 會拒絕這種 handoff；plan 的降權身分是 overlay 的 `operator_account`，未設定或為 root 時改用 `nobody`；RC 的「activate 前注入失敗」以 prior 的 builder/codex 落點檔權限偏離 0600 觸發 credential handoff 失敗，不新增 fault-injection 參數；`--recover` 依 spec §12.6 在已安裝的程式內呼叫 `_recover_command`（主流程的 receipt mutation 則一律走封存的 candidate CLI）。
- 新 receipt path 是 canonical receipt path 去掉 `.json` 後加 `.run-<nonce>.json`。
- 憑證只接手 prior receipt **已記錄**的列：`(principal, provider)` 相同、由新 plan 推得的落點與 prior 相同，而且落點檔通過與 `backend.validate_credentials` 相同的檢查（regular file、nlink 1、uid／gid 正確、權限 0600、sha256 相符）。不從任何 HOME 探索憑證，也不讀憑證的值。繼承列加上 `inherited_from: <prior receipt_id>`。
- `verify` 必須 PASS，三個 unit 都必須 active；`cortex service status --system` 必須顯示 loaded runtime 與新 receipt 的 installed artifact 一致（#841）。
- activate 之前失敗（ingress、plan、apply、credential handoff）：自動 rollback，並依 snapshot 恢復原本的服務。activate 之後失敗（verify FAIL、loaded↔installed 不一致）：執行 rollback；若 `restore_safe=false` 就停在原地，report 與終端機列出保留的 unknown state 與下一步，工具**不會**自動啟動 prior 的服務。
- upgrade report 寫到 `/var/lib/cortex-installer/<version>/upgrade-report.json`，內容包含版本、各 asset digest、plan sha、新舊 receipt path、verify evidence path、各步驟耗時與結果。
- maintenance snapshot 的格式不改；`--recover` 由 `plan_sha256` 推出 durable plan `/var/lib/cortex-installer/plans/<sha>.json`，再呼叫現有 `_recover_command` 的邏輯。
- `cortex upgrade` 不提供任何讓 Manager、job 或模型呼叫的入口，也不把 upgrade 註冊成 service。
- 改動 `qualification/` 或 `trust_root/install/` 會觸發 release gate 的 legacy-adoption RC 要求（`release_gate.LEGACY_TRIGGER_PREFIXES`），0.1.13 發版時要兩個 profile 都跑。
- 同 package 的私有函式（`cli._maintenance_lease` 等、`backend._run`、`core._write_all`／`_rename_noreplace_at`）**不改名**：既有測試以這些名稱作 monkeypatch seam；`upgrade.py` 一律以 `from . import cli as install_cli` 後 `install_cli._name` 的屬性存取，monkeypatch 才會生效。
- repo tier 為 shareable：程式、測試與文件不得出現個人絕對路徑、使用者名稱或雇主識別；測試只用 `tmp_path`。
- 同一 PR 必須有已 commit 的 `changelog.d/one-command-upgrade.md` 與 `CHANGELOG.md [Unreleased]` entry。

## Review Focus

- 同一版本重跑（前一次在 activate 前失敗、已回到 prior，修正後再執行 `cortex upgrade 0.1.13`）：不得因 `/var/lib/cortex-installer/0.1.13/` 已存在而失敗，也不得沿用上次未重新驗證的檔案；每次都在新的 `attempt-*` 目錄重新 ingress。→ Task 5、Task 6 加測試。
- apply 把 `/opt/cortex/venv` 這個 symlink 切到新 slot 後，協調器 process 內的任何延遲 import 都會讀到新版程式碼：開始時必須把 `sys.path` 釘在解析後的 slot，模組也不得有函式內 import。→ Task 7 加測試。
- 等待在飛 job 時，durable jobs registry 正被 Manager 改寫而讀不到（`durable_jobs=None`）：必須視為忙碌，不可當成 idle。→ Task 7 加測試。
- INT／TERM 在 apply 子程序已寫出 receipt、協調器卻還沒拿到回傳值時抵達：只要已嘗試 apply 而且 receipt 存在，就 rollback 那一份 receipt，再依 restore_safe 恢復服務。→ Task 9 加測試。
- host overlay 沒有 `operator_account`，或 release config 的 `operator_account` 是 `root`（RC 的 release config 就是如此）：plan 不得以 root 產生，改用 `nobody`；overlay 指名但主機上不存在的帳號則拒絕。→ Task 8 加測試。

---

## 檔案結構

| 路徑 | 動作 | 責任 |
| --- | --- | --- |
| `paulsha_cortex/trust_root/install/core.py` | Modify | `is_inherited_credential`、`inherit_prior_credentials`；receipt load 接受繼承列；rollback 保留繼承列 |
| `paulsha_cortex/trust_root/install/backend.py` | Modify | `rollback_credentials` 跳過繼承列 |
| `paulsha_cortex/trust_root/install/cli.py` | Modify | `_receipt_restore_safe`、`credentials inherit`、`upgrade` subparser、`upgrade_main` |
| `paulsha_cortex/trust_root/install/receipt_chain.py` | Create | `effective_receipt(state_root)` |
| `paulsha_cortex/trust_root/install/loaded_runtime.py` | Create | loaded↔installed 比對、installed runtime env |
| `paulsha_cortex/trust_root/install/release_ingress.py` | Create | REST metadata、fetcher、asset、manifest、archive、bundle、venv、tree sha |
| `paulsha_cortex/trust_root/install/upgrade.py` | Create | 協調器：前置檢查、plan、transaction、`--recover`、`--status` |
| `paulsha_cortex/trust_root/install/__init__.py` | Modify | 匯出新公開函式 |
| `paulsha_cortex/cli.py` | Modify | 頂層別名 `cortex upgrade`、`_HELP` |
| `paulsha_cortex/deploy/installer.py` | Modify | trust-root help 字串 |
| `qualification/release_source.py` | Create | RC 用的 GitHub REST 同形狀本機 release 來源 |
| `qualification/driver.py` | Modify | 改用 `loaded_runtime`；新增 one-command upgrade evidence |
| `qualification/validate.py` | Modify | 新 scenario／artifact／test |
| `qualification/run.sh`、`qualification/Dockerfile` | Modify | 以 `cortex upgrade` 取代升級演練 |
| `docs/superpowers/runbooks/trust-root-transactional-install.md` | Modify | 開頭「一般升級」 |
| `docs/superpowers/runbooks/trust-root-legacy-adoption.md` | Modify | 末尾補一句 |
| `changelog.d/one-command-upgrade.md`、`CHANGELOG.md` | Create／Modify | 變更紀錄 |
| `tests/release_ingress_fixtures.py`、`tests/upgrade_fixtures.py` | Create | 測試共用 fake 與 fixture |

---

### Task 1: 繼承憑證列的 receipt 語意（load、rollback、restore-safe）

**Files:**
- Modify: `paulsha_cortex/trust_root/install/core.py:3003-3012`（`InstallReceipt.load` 的 credentials 檢查）
- Modify: `paulsha_cortex/trust_root/install/core.py:7185-7191`（`rollback_receipt` 清除 credentials 的段落）
- Modify: `paulsha_cortex/trust_root/install/core.py:7429`（在 `credential_destination` 之後新增 `is_inherited_credential`）
- Modify: `paulsha_cortex/trust_root/install/backend.py:27-45`（`from .core import` 清單）、`backend.py:5381-5383`（`rollback_credentials` 迴圈開頭）
- Modify: `paulsha_cortex/trust_root/install/cli.py:36-58`（`from .core import` 清單）、`cli.py:1401-1430`（`_receipt_restore_safe`）
- Modify: `paulsha_cortex/trust_root/install/__init__.py`
- Test: `tests/test_trust_root_install_credentials.py`

**Interfaces:**
- Consumes: 既有 `InstallReceipt.load(path, *, expected_plan=None) -> InstallReceipt`、`rollback_receipt(receipt, *, backend, legacy_host=None) -> RollbackReport`、`LocalInstallBackend.rollback_credentials(receipt) -> Sequence[dict]`、`cli._receipt_restore_safe(document: Mapping) -> bool`。
- Produces: `core.is_inherited_credential(row: object) -> bool`。receipt 的 `credentials` 列可以帶 `inherited_from: str`，值必須等於同一份 receipt 的 `parent_receipt.receipt_id`，且不得同時帶 `created_directories`。`rollback_credentials` 不碰繼承列的落點檔；`rollback_receipt` 在沒有 credential drift 時把 `credentials` 收斂成只剩繼承列；`_receipt_restore_safe` 把「只剩繼承列」視為可安全還原。

- [ ] **Step 1: 寫會失敗的測試**

在 `tests/test_trust_root_install_credentials.py` 的 import 區塊把 `InstallError` 加進 `from paulsha_cortex.trust_root.install import (...)`（放在 `CredentialImportError,` 之後），並在檔案最後加入：

```python
_PRIOR_RECEIPT_PATH = Path("/var/lib/cortex-install-receipts/prior.json")


def _inherited_row(
    digest: str,
    prior_id: str,
    *,
    principal: str = "builder",
    provider: str = "codex",
) -> dict[str, str]:
    return {
        "principal": principal,
        "provider": provider,
        "mode": "0600",
        "sha256": digest,
        "inherited_from": prior_id,
    }


def _write_credential(home: Path, relative: str, content: bytes) -> str:
    destination = home / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    destination.chmod(0o600)
    return hashlib.sha256(content).hexdigest()


def _credential_accounts(
    tmp_path: Path, *, builder_uid: int | None = None
) -> list[dict[str, object]]:
    return [
        {
            "name": name,
            "home": str(tmp_path / name),
            "uid": (
                builder_uid
                if builder_uid is not None and name == "cortex-builder"
                else os.getuid()
            ),
            "gid": os.getgid(),
        }
        for name in ("cortex-builder", "cortex-reviewer-planner", "cortex-manager")
    ]


def _successor_link(prior_id: str) -> dict[str, str]:
    return {
        "path": str(_PRIOR_RECEIPT_PATH),
        "receipt_id": prior_id,
        "plan_sha256": "d" * 64,
    }


def test_receipt_load_accepts_inherited_credential_linked_to_its_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _o, _p: None)
    monkeypatch.setattr(install_core, "_validate_receipt_file", lambda _o, _p: None)
    path = (tmp_path / "receipts" / "successor.json").absolute()
    receipt = new_install_receipt(_plan(), path=path)
    receipt._document["parent_receipt"] = _successor_link("prior-receipt")
    receipt._document["credentials"] = [_inherited_row("e" * 64, "prior-receipt")]
    receipt._persist()

    loaded = InstallReceipt.load(path)

    assert loaded.to_dict()["credentials"] == [
        _inherited_row("e" * 64, "prior-receipt")
    ]


@pytest.mark.parametrize(
    ("parent", "inherited_from"),
    [
        (_successor_link("prior-receipt"), "another-receipt"),
        (_successor_link("prior-receipt"), ""),
        (None, "prior-receipt"),
    ],
)
def test_receipt_load_rejects_inherited_credential_not_linked_to_its_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent: dict[str, str] | None,
    inherited_from: str,
) -> None:
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _o, _p: None)
    monkeypatch.setattr(install_core, "_validate_receipt_file", lambda _o, _p: None)
    path = (tmp_path / "receipts" / "successor.json").absolute()
    receipt = new_install_receipt(_plan(), path=path)
    if parent is not None:
        receipt._document["parent_receipt"] = parent
    receipt._document["credentials"] = [_inherited_row("e" * 64, inherited_from)]
    receipt._persist()

    with pytest.raises(InstallError, match="credential metadata is invalid"):
        InstallReceipt.load(path)


def test_rollback_keeps_the_prior_credential_and_its_inherited_record(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    receipt._document["plan"]["accounts"] = _credential_accounts(tmp_path)  # type: ignore[index]
    receipt._document["parent_receipt"] = _successor_link("prior-receipt")
    inherited = _write_credential(
        tmp_path / "cortex-builder", ".codex/auth.json", b'{"token":"prior"}'
    )
    owned = _write_credential(
        tmp_path / "cortex-manager", ".config/gh/hosts.yml", b"github.com: {}\n"
    )
    receipt._document["credentials"] = [
        _inherited_row(inherited, "prior-receipt"),
        {
            "principal": "manager",
            "provider": "github",
            "mode": "0600",
            "sha256": owned,
        },
    ]

    report = rollback_receipt(
        receipt, backend=LocalInstallBackend(require_root=False)
    )

    assert report.retained_drift == ()
    assert (tmp_path / "cortex-builder/.codex/auth.json").read_bytes() == (
        b'{"token":"prior"}'
    )
    assert not (tmp_path / "cortex-manager/.config/gh/hosts.yml").exists()
    document = receipt.to_dict()
    assert document["state"] == "rolled-back"
    assert document["credentials"] == [_inherited_row(inherited, "prior-receipt")]
    assert install_cli._receipt_restore_safe(document) is True


def test_restore_safe_refuses_an_owned_credential_left_after_rollback() -> None:
    document = {
        "state": "rolled-back",
        "journal": [],
        "activation_journal": [],
        "credential_journal": [],
        "services_started": False,
        "rollback": {"retained_unknown": [], "retained_drift": []},
        "credentials": [
            {"principal": "builder", "provider": "codex", "mode": "0600", "sha256": "e" * 64}
        ],
    }

    assert install_cli._receipt_restore_safe(document) is False
    document["credentials"] = [_inherited_row("e" * 64, "prior-receipt")]
    assert install_cli._receipt_restore_safe(document) is True
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_trust_root_install_credentials.py -q -k "inherited or owned_credential_left"`
Expected: `3 failed, 3 passed`。失敗的是 `test_receipt_load_accepts_inherited_credential_linked_to_its_parent`（`InstallError: receipt credential metadata is invalid`）、`test_rollback_keeps_the_prior_credential_and_its_inherited_record`（繼承列的檔案被刪掉，`FileNotFoundError` 或 `credentials == []`）、`test_restore_safe_refuses_an_owned_credential_left_after_rollback`（第二個 assert 為 `False`）。三個 parametrize 的拒收案例現在就會通過（現行檢查一律拒收額外欄位），實作後仍須通過。

- [ ] **Step 3: 最小實作**

`paulsha_cortex/trust_root/install/core.py`：在 `credential_destination`（結尾 `return (... int(account["gid"]),)`，約 L7429）之後新增：

```python
def is_inherited_credential(row: object) -> bool:
    """True for a receipt credential row handed over from the prior receipt."""

    return (
        isinstance(row, Mapping)
        and isinstance(row.get("inherited_from"), str)
        and bool(row["inherited_from"])
    )
```

把 `InstallReceipt.load` 的 credentials 檢查（L3003-3012）整段換成：

```python
        credentials = payload.get("credentials")
        parent_receipt_id = (
            parent.get("receipt_id") if isinstance(parent, Mapping) else None
        )
        if not isinstance(credentials, list) or any(
            not isinstance(row, Mapping)
            or set(row) - {"created_directories", "inherited_from"}
            != {"principal", "provider", "mode", "sha256"}
            or row.get("mode") != "0600"
            or not isinstance(row.get("sha256"), str)
            or len(str(row.get("sha256"))) != 64
            # An inherited row names the prior receipt's file: it is valid only
            # in a successor linked to exactly that prior receipt, and it never
            # owns credential directories.
            or (
                "inherited_from" in row
                and (
                    not is_inherited_credential(row)
                    or row.get("inherited_from") != parent_receipt_id
                    or "created_directories" in row
                )
            )
            for row in credentials
        ):
            raise InstallError(f"receipt credential metadata is invalid: {path}")
```

把 `rollback_receipt` 的 L7189-7191：

```python
    if not credentials_retained:
        receipt._document["credentials"] = []
        receipt._document["credential_journal"] = []
```

換成：

```python
    if not credentials_retained:
        # Inherited rows name the prior receipt's files.  Rollback never touched
        # them, so they stay as the record of what this receipt relied on.
        current = receipt._document.get("credentials")
        receipt._document["credentials"] = [
            row
            for row in (current if isinstance(current, list) else [])
            if is_inherited_credential(row)
        ]
        receipt._document["credential_journal"] = []
```

`paulsha_cortex/trust_root/install/backend.py`：在 `from .core import (` 清單的 `credential_destination,` 後面加一行 `is_inherited_credential,`。在 `rollback_credentials` 迴圈開頭（L5381-5383）：

```python
        for row in rows:
            if not isinstance(row, Mapping):
                continue
```

之後立刻加：

```python
            if is_inherited_credential(row):
                # The prior receipt owns this file; the successor only relied on it.
                continue
```

`paulsha_cortex/trust_root/install/cli.py`：在 `from .core import (` 清單的 `import_credential,` 後面加 `is_inherited_credential,`。在 `_receipt_restore_safe` 之前新增：

```python
def _only_inherited_credentials(rows: object) -> bool:
    return isinstance(rows, list) and all(is_inherited_credential(row) for row in rows)
```

並把 `_receipt_restore_safe` 結尾判斷中的

```python
        and document.get("credentials", []) == []
```

換成

```python
        and _only_inherited_credentials(document.get("credentials", []))
```

`paulsha_cortex/trust_root/install/__init__.py`：在 `import_credential,` 之後加 `is_inherited_credential,`。

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_trust_root_install_credentials.py tests/test_trust_root_install_plan.py tests/test_trust_root_install_transaction.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add paulsha_cortex/trust_root/install/core.py paulsha_cortex/trust_root/install/backend.py paulsha_cortex/trust_root/install/cli.py paulsha_cortex/trust_root/install/__init__.py tests/test_trust_root_install_credentials.py
git commit -m "feat(trust-root): receipt 接受繼承憑證列，rollback 不刪 prior 憑證（#1263）"
```

---

### Task 2: `inherit_prior_credentials` 與 `credentials inherit` 子命令

**Files:**
- Modify: `paulsha_cortex/trust_root/install/core.py`（在 Task 1 新增的 `is_inherited_credential` 之後新增 `inherit_prior_credentials`）
- Modify: `paulsha_cortex/trust_root/install/__init__.py`
- Modify: `paulsha_cortex/trust_root/install/cli.py:36-58`（import）、`cli.py:713-720`（credentials subparser）、`cli.py:1145-1183`（在 `_credential_command` 之後新增 `_credential_inherit_command`）、`cli.py:1447-1448`（`main` 分派）
- Test: `tests/test_trust_root_install_credentials.py`

**Interfaces:**
- Consumes: `is_inherited_credential(row) -> bool`（Task 1）；既有 `credential_destination(receipt, *, principal, provider) -> tuple[Path, int, int]`、`validate_prior_receipt_handoff(plan, prior_receipt) -> Mapping`、`InstallBackend.validate_credentials(receipt) -> Sequence[str]`（`LocalInstallBackend` 有實作）。
- Produces: `inherit_prior_credentials(receipt: InstallReceipt, prior_receipt: InstallReceipt, *, backend: InstallBackend) -> tuple[dict[str, str], ...]`，失敗一律拋 `CredentialImportError`，成功時把列寫進 `receipt` 並 `_persist()`。CLI：`cortex install trust-root credentials inherit --receipt <新 receipt> --prior-receipt <prior> [--maintenance-token <token>]`，stdout 為 `{"receipt_id": str, "inherited": [{"principal": str, "provider": str, "inherited_from": str}]}`。Task 9 的協調器以封存的 candidate CLI 呼叫這個子命令。

- [ ] **Step 1: 寫會失敗的測試**

在 `tests/test_trust_root_install_credentials.py` 的 import 區塊加 `from copy import deepcopy`，把 `inherit_prior_credentials` 加進 `from paulsha_cortex.trust_root.install import (...)`，並在檔案最後加入：

```python
def _handoff(
    tmp_path: Path,
    *,
    required: tuple[tuple[str, str], ...] = (("builder", "codex"),),
    prior_rows: list[dict[str, str]] | None = None,
    prior_builder_uid: int | None = None,
    new_builder_uid: int | None = None,
) -> tuple[InstallReceipt, InstallReceipt]:
    rows = [{"principal": principal, "provider": provider} for principal, provider in required]
    prior_plan = _plan(required_credentials=rows)
    prior_plan["accounts"] = _credential_accounts(tmp_path, builder_uid=prior_builder_uid)
    prior_plan["candidate"] = {**prior_plan["candidate"], "wheel_sha256": "1" * 64}
    seed = new_install_receipt(prior_plan).to_dict()
    seed.update(state="applied", qualified=True, credentials=deepcopy(prior_rows or []))
    prior = InstallReceipt(seed, path=_PRIOR_RECEIPT_PATH)
    plan = deepcopy(prior_plan)
    plan["accounts"] = _credential_accounts(
        tmp_path,
        builder_uid=new_builder_uid if new_builder_uid is not None else prior_builder_uid,
    )
    plan["candidate"]["wheel_sha256"] = "2" * 64
    receipt = new_install_receipt(plan)
    receipt._document["state"] = "applied"
    receipt._document["parent_receipt"] = {
        "path": str(_PRIOR_RECEIPT_PATH),
        "receipt_id": seed["receipt_id"],
        "plan_sha256": seed["plan_sha256"],
    }
    return prior, receipt


def _prior_row(digest: str, *, principal: str = "builder", provider: str = "codex") -> dict[str, str]:
    return {"principal": principal, "provider": provider, "mode": "0600", "sha256": digest}


def test_inherit_prior_credentials_marks_rows_and_activation_counts_them(
    tmp_path: Path,
) -> None:
    digest = _write_credential(
        tmp_path / "cortex-builder", ".codex/auth.json", b'{"token":"prior"}'
    )
    prior, receipt = _handoff(tmp_path, prior_rows=[_prior_row(digest)])
    prior_id = prior.to_dict()["receipt_id"]

    rows = inherit_prior_credentials(
        receipt, prior, backend=LocalInstallBackend(require_root=False)
    )

    assert rows == (_inherited_row(digest, prior_id),)
    assert receipt.to_dict()["credentials"] == [_inherited_row(digest, prior_id)]

    class LiveValidation(CredentialBackend):
        def validate_credentials(self, current):
            return LocalInstallBackend(require_root=False).validate_credentials(current)

    backend = LiveValidation()
    activate_receipt(receipt, backend=backend)
    assert backend.started == [
        "cortex-egress-proxy.service",
        "cortex-manager.service",
        "cortex-monitor.service",
    ]


def test_inherit_refuses_a_destination_whose_digest_changed(tmp_path: Path) -> None:
    _write_credential(tmp_path / "cortex-builder", ".codex/auth.json", b'{"token":"new"}')
    prior, receipt = _handoff(tmp_path, prior_rows=[_prior_row("0" * 64)])

    with pytest.raises(CredentialImportError, match="builder/codex metadata or hash mismatch"):
        inherit_prior_credentials(
            receipt, prior, backend=LocalInstallBackend(require_root=False)
        )
    assert receipt.to_dict()["credentials"] == []


def test_inherit_refuses_a_destination_the_new_plan_moved(tmp_path: Path) -> None:
    digest = _write_credential(
        tmp_path / "cortex-builder", ".codex/auth.json", b'{"token":"prior"}'
    )
    prior, receipt = _handoff(
        tmp_path, prior_rows=[_prior_row(digest)], new_builder_uid=os.getuid() + 1
    )

    with pytest.raises(CredentialImportError, match="destination changed"):
        inherit_prior_credentials(
            receipt, prior, backend=LocalInstallBackend(require_root=False)
        )


def test_inherit_refuses_a_file_owned_by_another_uid(tmp_path: Path) -> None:
    digest = _write_credential(
        tmp_path / "cortex-builder", ".codex/auth.json", b'{"token":"prior"}'
    )
    prior, receipt = _handoff(
        tmp_path, prior_rows=[_prior_row(digest)], prior_builder_uid=os.getuid() + 1
    )

    with pytest.raises(CredentialImportError, match="builder/codex metadata or hash mismatch"):
        inherit_prior_credentials(
            receipt, prior, backend=LocalInstallBackend(require_root=False)
        )


def test_inherit_refuses_a_hard_linked_destination(tmp_path: Path) -> None:
    digest = _write_credential(
        tmp_path / "cortex-builder", ".codex/auth.json", b'{"token":"prior"}'
    )
    os.link(tmp_path / "cortex-builder/.codex/auth.json", tmp_path / "second-link")
    prior, receipt = _handoff(tmp_path, prior_rows=[_prior_row(digest)])

    with pytest.raises(CredentialImportError, match="builder/codex metadata or hash mismatch"):
        inherit_prior_credentials(
            receipt, prior, backend=LocalInstallBackend(require_root=False)
        )


def test_inherit_refuses_a_provider_the_prior_receipt_never_recorded(
    tmp_path: Path,
) -> None:
    digest = _write_credential(
        tmp_path / "cortex-builder", ".codex/auth.json", b'{"token":"prior"}'
    )
    prior, receipt = _handoff(
        tmp_path,
        required=(("builder", "codex"), ("reviewer-planner", "copilot")),
        prior_rows=[_prior_row(digest)],
    )

    with pytest.raises(CredentialImportError) as exc:
        inherit_prior_credentials(
            receipt, prior, backend=LocalInstallBackend(require_root=False)
        )
    assert "reviewer-planner/copilot" in str(exc.value)
    assert "§4" in str(exc.value)
    assert receipt.to_dict()["credentials"] == []


def test_inherit_refuses_a_receipt_that_is_not_the_prior_successor(tmp_path: Path) -> None:
    digest = _write_credential(
        tmp_path / "cortex-builder", ".codex/auth.json", b'{"token":"prior"}'
    )
    prior, receipt = _handoff(tmp_path, prior_rows=[_prior_row(digest)])
    del receipt._document["parent_receipt"]

    with pytest.raises(CredentialImportError, match="not the upgrade successor"):
        inherit_prior_credentials(
            receipt, prior, backend=LocalInstallBackend(require_root=False)
        )


def test_credentials_inherit_cli_records_rows_in_the_durable_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _o, _p: None)
    monkeypatch.setattr(install_core, "_validate_receipt_file", lambda _o, _p: None)
    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_LOCK_ROOT", tmp_path / "host-locks")
    monkeypatch.setattr(
        install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "maintenance-state"
    )
    monkeypatch.setattr(
        install_cli, "LocalInstallBackend", lambda: LocalInstallBackend(require_root=False)
    )
    digest = _write_credential(
        tmp_path / "cortex-builder", ".codex/auth.json", b'{"token":"prior"}'
    )
    prior_plan = _plan(required_credentials=[{"principal": "builder", "provider": "codex"}])
    prior_plan["accounts"] = _credential_accounts(tmp_path)
    prior_plan["candidate"] = {**prior_plan["candidate"], "wheel_sha256": "1" * 64}
    prior_path = (tmp_path / "receipts" / "prior.json").absolute()
    prior = new_install_receipt(prior_plan, path=prior_path)
    prior._document.update(state="applied", qualified=True, credentials=[_prior_row(digest)])
    prior._persist()
    plan = deepcopy(prior_plan)
    plan["candidate"]["wheel_sha256"] = "2" * 64
    next_path = (tmp_path / "receipts" / "next.json").absolute()
    successor = new_install_receipt(plan, path=next_path)
    successor._document["state"] = "applied"
    prior_document = prior.to_dict()
    successor._document["parent_receipt"] = {
        "path": str(prior_path),
        "receipt_id": prior_document["receipt_id"],
        "plan_sha256": prior_document["plan_sha256"],
    }
    successor._persist()

    assert install_cli.main(
        [
            "credentials",
            "inherit",
            "--receipt",
            str(next_path),
            "--prior-receipt",
            str(prior_path),
        ]
    ) == 0

    emitted = json.loads(capsys.readouterr().out)
    assert emitted["inherited"] == [
        {
            "principal": "builder",
            "provider": "codex",
            "inherited_from": prior_document["receipt_id"],
        }
    ]
    assert InstallReceipt.load(next_path).to_dict()["credentials"] == [
        _inherited_row(digest, prior_document["receipt_id"])
    ]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_trust_root_install_credentials.py -q -k "inherit"`
Expected: collection error，`ImportError: cannot import name 'inherit_prior_credentials' from 'paulsha_cortex.trust_root.install'`。

- [ ] **Step 3: 最小實作**

`paulsha_cortex/trust_root/install/core.py`：在 `is_inherited_credential` 之後新增：

```python
def inherit_prior_credentials(
    receipt: InstallReceipt,
    prior_receipt: InstallReceipt,
    *,
    backend: InstallBackend,
) -> tuple[dict[str, str], ...]:
    """Hand the prior receipt's recorded credentials to its upgrade successor.

    Only rows the prior receipt already records move over, and only when the
    new plan derives the same destination and the file there passes the same
    live check activation runs (regular file, one link, uid/gid, 0600, sha256).
    No HOME is searched and no credential content leaves the file.  A pair the
    new plan requires but the prior never recorded is refused: the operator
    imports it through the runbook instead.
    """

    document = receipt._document
    if document.get("state") != "applied":
        raise CredentialImportError(
            "credentials may only be inherited into an applied receipt"
        )
    if document.get("credentials") or document.get("credential_journal"):
        raise CredentialImportError(
            "receipt already records credential authority; inheritance applies "
            "only to a fresh upgrade receipt"
        )
    plan = document.get("plan")
    if not isinstance(plan, Mapping):
        raise CredentialImportError("receipt plan is invalid")
    try:
        validate_prior_receipt_handoff(plan, prior_receipt)
    except InstallPlanError as exc:
        raise CredentialImportError(
            f"prior receipt cannot hand off credentials: {exc}"
        ) from exc
    prior_document = prior_receipt.to_dict()
    if prior_receipt.path is None or document.get("parent_receipt") != {
        "path": str(prior_receipt.path),
        "receipt_id": prior_document.get("receipt_id"),
        "plan_sha256": prior_document.get("plan_sha256"),
    }:
        raise CredentialImportError(
            "receipt is not the upgrade successor of the prior receipt"
        )
    prior_rows: dict[tuple[str, str], Mapping[str, object]] = {}
    for row in prior_document.get("credentials", []):
        if isinstance(row, Mapping):
            prior_rows[(str(row.get("principal")), str(row.get("provider")))] = row
    required = [
        (str(row.get("principal")), str(row.get("provider")))
        for row in plan.get("required_credentials", [])
        if isinstance(row, Mapping)
    ]
    missing = [pair for pair in required if pair not in prior_rows]
    if missing:
        rendered = ", ".join(f"{principal}/{provider}" for principal, provider in missing)
        raise CredentialImportError(
            f"new plan requires credentials the prior receipt never recorded: {rendered}; "
            "roll back and import them per trust-root-transactional-install.md §4"
        )
    prior_id = str(prior_document.get("receipt_id"))
    inherited: list[dict[str, str]] = []
    for principal, provider in required:
        if credential_destination(
            prior_receipt, principal=principal, provider=provider
        ) != credential_destination(receipt, principal=principal, provider=provider):
            raise CredentialImportError(
                f"credential destination changed since the prior receipt: {principal}/{provider}"
            )
        inherited.append(
            {
                "principal": principal,
                "provider": provider,
                "mode": "0600",
                "sha256": str(prior_rows[(principal, provider)].get("sha256")),
                "inherited_from": prior_id,
            }
        )
    validator = getattr(backend, "validate_credentials", None)
    if not callable(validator):
        raise CredentialImportError("backend cannot validate inherited credentials")
    probe = InstallReceipt({**document, "credentials": deepcopy(inherited)})
    failures = tuple(str(row) for row in validator(probe))
    if failures:
        raise CredentialImportError(
            "prior credential cannot be inherited: " + "; ".join(failures)
        )
    previous = document.get("credentials")
    document["credentials"] = deepcopy(inherited)
    try:
        receipt._persist()
    except BaseException:
        document["credentials"] = previous
        raise
    return tuple(inherited)
```

`activate_receipt`（`core.py:8034-8055`）已經以 `credentials` 列的 `(principal, provider)` 計算「已匯入」，並以 `backend.validate_credentials` 重驗每一列；繼承列放在 `credentials` 內就會被計入並重驗，所以本 task 不改 `activate_receipt`，由 Step 1 的第一個測試釘住這個行為。

`paulsha_cortex/trust_root/install/__init__.py`：在 `import_credential,` 之前加 `inherit_prior_credentials,`。

`paulsha_cortex/trust_root/install/cli.py`：
1. `from .core import (` 清單在 `import_credential,` 之前加 `inherit_prior_credentials,`。
2. 在 `_build_parser` 的 `credential_import.add_argument("--maintenance-token")`（L720）之後加：

```python
    credential_inherit = credential_sub.add_parser(
        "inherit",
        help="hand the prior receipt's recorded credentials to an upgrade receipt",
    )
    credential_inherit.add_argument("--receipt", required=True)
    credential_inherit.add_argument("--prior-receipt", required=True)
    credential_inherit.add_argument("--maintenance-token")
```

3. 在 `_credential_command` 之後新增：

```python
def _credential_inherit_command(args: argparse.Namespace) -> int:
    _require_root()
    prior_path = Path(args.prior_receipt).expanduser()
    with _locked_receipt(
        Path(args.receipt), maintenance_token=args.maintenance_token
    ) as (receipt, _plan):
        prior = InstallReceipt.load(prior_path)
        rows = inherit_prior_credentials(
            receipt, prior, backend=LocalInstallBackend()
        )
    _emit(
        {
            "receipt_id": receipt.to_dict()["receipt_id"],
            "inherited": [
                {
                    "principal": row["principal"],
                    "provider": row["provider"],
                    "inherited_from": row["inherited_from"],
                }
                for row in rows
            ],
        }
    )
    return 0
```

4. `main` 的分派（L1447-1448）：

```python
        if args.trust_root_command == "credentials":
            return _credential_command(args)
```

換成：

```python
        if args.trust_root_command == "credentials":
            if args.credential_command == "inherit":
                return _credential_inherit_command(args)
            return _credential_command(args)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_trust_root_install_credentials.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add paulsha_cortex/trust_root/install/core.py paulsha_cortex/trust_root/install/cli.py paulsha_cortex/trust_root/install/__init__.py tests/test_trust_root_install_credentials.py
git commit -m "feat(trust-root): prior receipt 憑證 handoff 與 credentials inherit 子命令（#1263）"
```

---

### Task 3: `effective_receipt`：依 receipt chain 定位生效中的 receipt

**Files:**
- Create: `paulsha_cortex/trust_root/install/receipt_chain.py`
- Modify: `paulsha_cortex/trust_root/install/__init__.py`
- Test: `tests/test_trust_root_install_receipt_chain.py`

**Interfaces:**
- Consumes: 既有 `InstallReceipt.load(path) -> InstallReceipt`、`InstallError`。
- Produces: `receipt_directory(state_root: Path) -> Path`（`<state 上層>/<state 名稱>-install-receipts`）、`effective_receipt(state_root: Path) -> InstallReceipt`；無法唯一判定時拋 `InstallError("cannot decide the effective receipt: ...")`。Task 7、Task 10 使用。

- [ ] **Step 1: 寫會失敗的測試**

建立 `tests/test_trust_root_install_receipt_chain.py`：

```python
"""#1263：生效中的 receipt 只由 receipt chain 決定，不看檔名、mtime 或掃描順序。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install.core import (
    InstallError,
    InstallReceipt,
    new_install_receipt,
)
from paulsha_cortex.trust_root.install.receipt_chain import (
    effective_receipt,
    receipt_directory,
)


@pytest.fixture(autouse=True)
def _rootless_receipts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _o, _p: None)
    monkeypatch.setattr(install_core, "_validate_receipt_file", lambda _o, _p: None)


def _state_root(tmp_path: Path) -> Path:
    return (tmp_path / "var/lib/cortex").absolute()


def _plan(state_root: Path, *, wheel_digit: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "scheme": "four-way",
        "repo_identity": {
            "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
            "commit": "a" * 40,
        },
        "candidate": {"wheel_sha256": wheel_digit * 64, "bundle_sha256": "c" * 64},
        "accounts": [],
        "roots": {
            "deploy": str(state_root.parent.parent / "opt/cortex"),
            "state": str(state_root),
            "systemd": "/etc/systemd/system",
            "polkit": "/etc/polkit-1/rules.d",
        },
        "apply_order": [],
        "required_credentials": [],
    }


def _receipt(
    state_root: Path,
    name: str,
    *,
    wheel_digit: str,
    state: str = "applied",
    qualified: bool = True,
    parent: InstallReceipt | None = None,
) -> InstallReceipt:
    path = receipt_directory(state_root) / name
    receipt = new_install_receipt(_plan(state_root, wheel_digit=wheel_digit), path=path)
    receipt._document["state"] = state
    receipt._document["qualified"] = qualified
    if parent is not None:
        document = parent.to_dict()
        receipt._document["parent_receipt"] = {
            "path": str(parent.path),
            "receipt_id": document["receipt_id"],
            "plan_sha256": document["plan_sha256"],
        }
    receipt._persist()
    return receipt


def test_receipt_directory_follows_the_state_root_name(tmp_path: Path) -> None:
    assert receipt_directory(Path("/var/lib/cortex")) == Path(
        "/var/lib/cortex-install-receipts"
    )


def test_qualified_successor_is_the_effective_receipt(tmp_path: Path) -> None:
    state_root = _state_root(tmp_path)
    prior = _receipt(state_root, "prior.json", wheel_digit="1")
    successor = _receipt(state_root, "next.run-x.json", wheel_digit="2", parent=prior)

    assert effective_receipt(state_root).path == successor.path


def test_rolled_back_successor_returns_authority_to_the_prior_regardless_of_mtime(
    tmp_path: Path,
) -> None:
    state_root = _state_root(tmp_path)
    prior = _receipt(state_root, "prior.json", wheel_digit="1")
    rolled_back = _receipt(
        state_root,
        "next.run-x.json",
        wheel_digit="2",
        state="rolled-back",
        qualified=False,
        parent=prior,
    )
    os.utime(prior.path, (1, 1))
    os.utime(rolled_back.path, None)

    assert effective_receipt(state_root).path == prior.path


def test_unqualified_successor_does_not_supersede_the_prior(tmp_path: Path) -> None:
    state_root = _state_root(tmp_path)
    prior = _receipt(state_root, "prior.json", wheel_digit="1")
    _receipt(
        state_root, "next.run-x.json", wheel_digit="2", qualified=False, parent=prior
    )

    assert effective_receipt(state_root).path == prior.path


def test_two_unrelated_qualified_receipts_are_refused(tmp_path: Path) -> None:
    state_root = _state_root(tmp_path)
    _receipt(state_root, "a.json", wheel_digit="1")
    _receipt(state_root, "b.json", wheel_digit="2")

    with pytest.raises(InstallError, match="2 applied and qualified receipts"):
        effective_receipt(state_root)


def test_no_qualified_receipt_is_refused(tmp_path: Path) -> None:
    state_root = _state_root(tmp_path)
    _receipt(state_root, "a.json", wheel_digit="1", state="rolled-back", qualified=False)

    with pytest.raises(InstallError, match="0 applied and qualified receipts"):
        effective_receipt(state_root)


def test_an_unreadable_receipt_stops_the_decision(tmp_path: Path) -> None:
    state_root = _state_root(tmp_path)
    _receipt(state_root, "prior.json", wheel_digit="1")
    (receipt_directory(state_root) / "broken.json").write_text("{", encoding="utf-8")

    with pytest.raises(InstallError, match="broken.json is unreadable"):
        effective_receipt(state_root)


def test_missing_receipt_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InstallError, match="receipt directory is unavailable"):
        effective_receipt(_state_root(tmp_path))
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_trust_root_install_receipt_chain.py -q`
Expected: collection error，`ModuleNotFoundError: No module named 'paulsha_cortex.trust_root.install.receipt_chain'`。

- [ ] **Step 3: 最小實作**

建立 `paulsha_cortex/trust_root/install/receipt_chain.py`：

```python
"""Locate the one receipt in force by following receipt-chain links (#1263).

`cortex service status` picks the newest receipt by mtime, which is wrong right
after a rollback.  The chain is authoritative instead: the effective receipt is
the applied and qualified receipt that no applied and qualified successor names
as its ``parent_receipt``.  Links are compared exactly as
``legacy_purge._successor_qualified_at`` does (path, receipt_id and plan_sha256
together).  Anything that prevents a unique answer stops the caller.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

from .core import InstallError, InstallReceipt

_SCAN_LIMIT = 1024


def receipt_directory(state_root: Path) -> Path:
    return state_root.parent / f"{state_root.name}-install-receipts"


def _link(path: Path, document: Mapping[str, object]) -> dict[str, object]:
    return {
        "path": str(path),
        "receipt_id": document.get("receipt_id"),
        "plan_sha256": document.get("plan_sha256"),
    }


def effective_receipt(state_root: Path) -> InstallReceipt:
    directory = receipt_directory(state_root)
    if directory.is_symlink() or not directory.is_dir():
        raise InstallError(f"install receipt directory is unavailable: {directory}")
    paths = sorted(
        path
        for path in directory.iterdir()
        if path.name.endswith(".json") and not path.name.startswith(".")
    )
    if len(paths) > _SCAN_LIMIT:
        raise InstallError(f"install receipt directory exceeds the scan limit: {directory}")
    qualified: list[tuple[Path, InstallReceipt]] = []
    for path in paths:
        try:
            receipt = InstallReceipt.load(path)
        except InstallError as exc:
            raise InstallError(
                f"cannot decide the effective receipt; {path.name} is unreadable: {exc}"
            ) from exc
        document = receipt.to_dict()
        plan = document.get("plan")
        roots = plan.get("roots") if isinstance(plan, Mapping) else None
        if not isinstance(roots, Mapping) or roots.get("state") != str(state_root):
            raise InstallError(
                f"cannot decide the effective receipt; {path.name} belongs to another state root"
            )
        if document.get("state") == "applied" and document.get("qualified") is True:
            qualified.append((path, receipt))
    successor_links = [receipt.to_dict().get("parent_receipt") for _path, receipt in qualified]
    heads = [
        (path, receipt)
        for path, receipt in qualified
        if _link(path, receipt.to_dict()) not in successor_links
    ]
    if len(heads) != 1:
        names = ", ".join(path.name for path, _receipt in heads) or "none"
        raise InstallError(
            "cannot decide the effective receipt: "
            f"{len(heads)} applied and qualified receipts have no qualified successor ({names})"
        )
    return heads[0][1]
```

`paulsha_cortex/trust_root/install/__init__.py`：在 `from .backend import LocalInstallBackend, SystemInstallBackend` 之後加：

```python
from .receipt_chain import effective_receipt, receipt_directory
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_trust_root_install_receipt_chain.py -q`
Expected: 8 passed。

- [ ] **Step 5: Commit**

```bash
git add paulsha_cortex/trust_root/install/receipt_chain.py paulsha_cortex/trust_root/install/__init__.py tests/test_trust_root_install_receipt_chain.py
git commit -m "feat(trust-root): 以 receipt chain 判定唯一生效中的 receipt（#1263）"
```

---

### Task 4: `loaded_runtime`：把 loaded↔installed 比對移進 `paulsha_cortex`

**Files:**
- Create: `paulsha_cortex/trust_root/install/loaded_runtime.py`
- Modify: `qualification/driver.py:49`（import）、刪除 `qualification/driver.py:565-631`（`_rollback_loaded_runtime_mismatch` 與 `_rollback_runtime_expected` 兩個函式本體）
- Test: `tests/test_trust_root_install_loaded_runtime.py`

**Interfaces:**
- Consumes: `InstallError`。
- Produces:
  - `diagnostic_token(value: object) -> str`
  - `runtime_expected(receipt: Mapping[str, object]) -> dict[str, object]`（鍵 `receipt_id`、`wheel_sha256`、`candidate_commit`，值取自 receipt 的 `plan.candidate.wheel_sha256` 與 `plan.repo_identity.commit`）
  - `loaded_runtime_mismatch(payload: object, expected: Mapping[str, object]) -> str`（空字串＝一致；否則是以空白分隔的列舉 token）
  - `installed_runtime_env(deploy_root: Path, state_root: Path, *, owner_uid: int = 0) -> dict[str, str]`（讀 `<deploy>/etc/cortex-manager.env` 的 `PSC_*`；檔案必須是 owner 擁有、無 group／other 寫入的 regular file）
  - driver 以別名保留 `_rollback_loaded_runtime_mismatch`、`_rollback_runtime_expected`，既有 `tests/test_qualification_driver_service_status.py` 不必改。

- [ ] **Step 1: 寫會失敗的測試**

建立 `tests/test_trust_root_install_loaded_runtime.py`：

```python
"""#1263：loaded↔installed 比對由 paulsha_cortex 提供，driver 只沿用。"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from paulsha_cortex.trust_root.install import loaded_runtime
from paulsha_cortex.trust_root.install.core import InstallError


def _driver():
    path = Path(__file__).parents[1] / "qualification" / "driver.py"
    spec = importlib.util.spec_from_file_location("qualification_driver_loaded_runtime", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _payload(*, receipt_id: str = "prior", wheel: str = "b" * 64, commit: str = "c" * 40):
    return {
        "service": {
            "loaded_runtime": {
                name: {
                    "comparison": {
                        "artifact_status": "match",
                        "config_status": "match",
                        "process_status": "match",
                        "loaded_wheel_sha256": wheel,
                    },
                    "trust_root": {
                        "status": "verified",
                        "receipt_id": receipt_id,
                        "wheel_sha256": wheel,
                        "candidate_commit": commit,
                    },
                    "installed_artifact": {"wheel_sha256": wheel},
                }
                for name in ("manager", "monitor")
            }
        }
    }


EXPECTED = {"receipt_id": "prior", "wheel_sha256": "b" * 64, "candidate_commit": "c" * 40}


def test_matching_runtime_reports_no_mismatch() -> None:
    assert loaded_runtime.loaded_runtime_mismatch(_payload(), EXPECTED) == ""


def test_receipt_and_wheel_mismatch_are_named() -> None:
    mismatch = loaded_runtime.loaded_runtime_mismatch(
        _payload(receipt_id="other", wheel="d" * 64), EXPECTED
    )

    assert "manager_receipt=mismatch" in mismatch
    assert "manager_loaded_wheel=mismatch" in mismatch


def test_missing_trust_root_is_unknown() -> None:
    payload = _payload()
    del payload["service"]["loaded_runtime"]["manager"]["trust_root"]

    assert "manager=unknown" in loaded_runtime.loaded_runtime_mismatch(payload, EXPECTED)


def test_unavailable_payload_is_unknown() -> None:
    assert loaded_runtime.loaded_runtime_mismatch(None, EXPECTED) == "loaded_runtime=unknown"


def test_expected_runtime_comes_from_the_receipt_plan() -> None:
    receipt = {
        "receipt_id": "r1",
        "plan": {
            "candidate": {"wheel_sha256": "b" * 64},
            "repo_identity": {"commit": "c" * 40},
        },
    }

    assert loaded_runtime.runtime_expected(receipt) == {
        "receipt_id": "r1",
        "wheel_sha256": "b" * 64,
        "candidate_commit": "c" * 40,
    }


def test_driver_reuses_the_packaged_comparison() -> None:
    driver = _driver()

    assert driver._rollback_loaded_runtime_mismatch is loaded_runtime.loaded_runtime_mismatch
    assert driver._rollback_runtime_expected is loaded_runtime.runtime_expected


def test_installed_runtime_env_reads_only_psc_values(tmp_path: Path) -> None:
    deploy = tmp_path / "opt/cortex"
    (deploy / "etc").mkdir(parents=True)
    env_file = deploy / "etc/cortex-manager.env"
    env_file.write_text(
        'PSC_COORDINATOR_ROOT="/srv/state/coordinator"\nOTHER="ignored"\n',
        encoding="utf-8",
    )
    env_file.chmod(0o644)

    env = loaded_runtime.installed_runtime_env(
        deploy, tmp_path / "var/lib/cortex", owner_uid=os.getuid()
    )

    assert env["PSC_COORDINATOR_ROOT"] == "/srv/state/coordinator"
    assert env["PSC_CONTROL_ROOT"] == str(tmp_path / "var/lib/cortex/control")
    assert "OTHER" not in env
    assert env["PATH"].startswith(f"{deploy}/venv/bin:")


def test_installed_runtime_env_refuses_a_writable_file(tmp_path: Path) -> None:
    deploy = tmp_path / "opt/cortex"
    (deploy / "etc").mkdir(parents=True)
    env_file = deploy / "etc/cortex-manager.env"
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o666)

    with pytest.raises(InstallError, match="not root-controlled"):
        loaded_runtime.installed_runtime_env(
            deploy, tmp_path / "var/lib/cortex", owner_uid=os.getuid()
        )
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_trust_root_install_loaded_runtime.py -q`
Expected: collection error，`ImportError: cannot import name 'loaded_runtime' from 'paulsha_cortex.trust_root.install'`。

- [ ] **Step 3: 最小實作**

建立 `paulsha_cortex/trust_root/install/loaded_runtime.py`：

```python
"""Loaded↔installed runtime comparison shared by `cortex upgrade` and RC (#841, #1263).

The comparison reads only enumerated tokens from ``cortex service status
--system --json``; detail strings never leave it, so the result is safe to put
in upgrade reports and qualification evidence.
"""
from __future__ import annotations

import json
import re
import stat
from pathlib import Path
from typing import Mapping

from .core import InstallError

_DIAGNOSTIC_TOKEN = re.compile(r"[A-Za-z0-9_.:+-]{1,64}")


def diagnostic_token(value: object) -> str:
    """Short enumerated strings pass; anything else becomes its type name."""

    if value is None:
        return "none"
    if isinstance(value, str) and _DIAGNOSTIC_TOKEN.fullmatch(value):
        return value
    return f"<{type(value).__name__}>"


def runtime_expected(receipt: Mapping[str, object]) -> dict[str, object]:
    plan = receipt.get("plan")
    candidate = plan.get("candidate") if isinstance(plan, Mapping) else None
    identity = plan.get("repo_identity") if isinstance(plan, Mapping) else None
    return {
        "receipt_id": receipt.get("receipt_id"),
        "wheel_sha256": candidate.get("wheel_sha256") if isinstance(candidate, Mapping) else None,
        "candidate_commit": identity.get("commit") if isinstance(identity, Mapping) else None,
    }


def loaded_runtime_mismatch(payload: object, expected: Mapping[str, object]) -> str:
    """Compare Manager/Monitor's loaded artifact and receipt with ``expected``."""

    service = payload.get("service") if isinstance(payload, Mapping) else None
    loaded = service.get("loaded_runtime") if isinstance(service, Mapping) else None
    if not isinstance(loaded, Mapping):
        return "loaded_runtime=unknown"
    expected_receipt = expected.get("receipt_id")
    expected_wheel = expected.get("wheel_sha256")
    expected_commit = expected.get("candidate_commit")
    if not all(
        isinstance(value, str) and value
        for value in (expected_receipt, expected_wheel, expected_commit)
    ):
        return "expected_receipt=unknown"
    mismatches: list[str] = []
    for name in ("manager", "monitor"):
        report = loaded.get(name)
        if not isinstance(report, Mapping):
            mismatches.append(f"{name}=unknown")
            continue
        comparison = report.get("comparison")
        trust_root = report.get("trust_root")
        installed = report.get("installed_artifact")
        if not all(
            isinstance(value, Mapping) for value in (comparison, trust_root, installed)
        ):
            mismatches.append(f"{name}=unknown")
            continue
        if any(
            comparison.get(key) != "match"
            for key in ("artifact_status", "config_status", "process_status")
        ):
            mismatches.append(f"{name}_runtime=mismatch")
        if trust_root.get("status") != "verified":
            mismatches.append(f"{name}_trust={diagnostic_token(trust_root.get('status'))}")
        if trust_root.get("receipt_id") != expected_receipt:
            state = "mismatch" if isinstance(trust_root.get("receipt_id"), str) else "unknown"
            mismatches.append(f"{name}_receipt={state}")
        for label, value in (
            ("loaded_wheel", comparison.get("loaded_wheel_sha256")),
            ("installed_wheel", installed.get("wheel_sha256")),
            ("receipt_wheel", trust_root.get("wheel_sha256")),
        ):
            if value != expected_wheel:
                state = "mismatch" if isinstance(value, str) else "unknown"
                mismatches.append(f"{name}_{label}={state}")
        loaded_commit = trust_root.get("candidate_commit")
        if loaded_commit != expected_commit:
            state = "mismatch" if isinstance(loaded_commit, str) else "unknown"
            mismatches.append(f"{name}_commit={state}")
    return " ".join(mismatches)


def installed_runtime_env(
    deploy_root: Path, state_root: Path, *, owner_uid: int = 0
) -> dict[str, str]:
    """The installed, root-owned PSC runtime projection for operator CLI probes."""

    path = deploy_root / "etc" / "cortex-manager.env"
    try:
        observed = path.lstat()
    except FileNotFoundError as exc:
        raise InstallError("installed Manager environment is absent") from exc
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != owner_uid
        or stat.S_IMODE(observed.st_mode) & 0o022
    ):
        raise InstallError("installed Manager environment is not root-controlled")
    env = {
        "HOME": "/root",
        "PATH": f"{deploy_root}/venv/bin:{deploy_root}/toolchain/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    for raw in path.read_text(encoding="utf-8", errors="strict").splitlines():
        key, separator, encoded = raw.partition("=")
        if not separator or not key.startswith("PSC_"):
            continue
        try:
            value = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise InstallError(f"invalid installed runtime value for {key}") from exc
        if not isinstance(value, str) or "\x00" in value:
            raise InstallError(f"invalid installed runtime value for {key}")
        env[key] = value
    env.setdefault("PSC_CONTROL_ROOT", str(state_root / "control"))
    env.setdefault("PSC_COORDINATOR_ROOT", str(state_root / "coordinator"))
    env.setdefault("PSC_SPECS_ROOT", str(state_root / "specs"))
    env.setdefault("PSC_MONITOR_STATE_ROOT", str(state_root / "monitor"))
    return env
```

`qualification/driver.py`：在 L49 `from paulsha_cortex.trust_root.surfaces import writable_surface` 之後加：

```python
from paulsha_cortex.trust_root.install.loaded_runtime import (
    loaded_runtime_mismatch as _rollback_loaded_runtime_mismatch,
    runtime_expected as _rollback_runtime_expected,
)
```

並刪除 L565-631 的 `def _rollback_loaded_runtime_mismatch(...)` 與 `def _rollback_runtime_expected(...)` 兩個函式本體（含其後兩行空白）。`_diagnostic_token` 仍有其他呼叫點，保留不動。

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_trust_root_install_loaded_runtime.py tests/test_qualification_driver_service_status.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add paulsha_cortex/trust_root/install/loaded_runtime.py qualification/driver.py tests/test_trust_root_install_loaded_runtime.py
git commit -m "refactor(qualification): loaded↔installed 比對移入 paulsha_cortex（#1263）"
```

---
### Task 5: release ingress（一）：版本、REST metadata、fetcher、asset 下載與本機 release 來源

**Files:**
- Create: `paulsha_cortex/trust_root/install/release_ingress.py`
- Create: `qualification/release_source.py`
- Create: `tests/release_ingress_fixtures.py`
- Test: `tests/test_trust_root_install_release_ingress.py`

**Interfaces:**
- Consumes: `InstallError`（core）、`_run`（backend，Task 6 才用到）。
- Produces（`release_ingress`）：
  - `OFFICIAL_REPOSITORY = "hamanpaul/paulsha-cortex"`、`class IngressError(InstallError)`
  - `parse_version(value: object) -> tuple[int, int, int]`、`format_version(version: tuple[int, int, int]) -> str`、`wheel_version(wheel_path: object) -> tuple[int, int, int]`、`asset_names(version: str) -> tuple[str, str, str]`（wheel、install-input、qualification）
  - `@dataclass(frozen=True) ReleaseAsset(name: str, sha256: str, size: int, url: str)`、`ReleaseMetadata(version: str, tag: str, commit: str, wheel: ReleaseAsset, install_input: ReleaseAsset, qualification: ReleaseAsset)`
  - `class ReleaseFetcher(Protocol)`：`get_json(api_path: str) -> object`、`copy_asset(asset: ReleaseAsset, destination: BinaryIO) -> None`
  - `GitHubReleaseFetcher(repository: str = OFFICIAL_REPOSITORY, *, urlopen=urllib.request.urlopen, timeout: float = 60.0)`、`DirectoryReleaseFetcher(root: Path)`
  - `resolve_release(fetcher: ReleaseFetcher, version: str, *, repository: str = OFFICIAL_REPOSITORY) -> ReleaseMetadata`
  - `download_asset(fetcher: ReleaseFetcher, asset: ReleaseAsset, release_dir: Path) -> Path`
  - `assert_private_chain(path: Path, *, owner_uid: int, stop: Path = Path("/")) -> None`
  - `make_attempt_dir(installer_root: Path, version: str, *, owner_uid: int, chain_stop: Path = Path("/")) -> Path`（回傳 `<installer_root>/<version>/attempt-<UTC>-<8 hex>`，其下有 0700 的 `release/`）
- Produces（`qualification/release_source.py`）：`build_release_source(*, artifacts: Path, output: Path, version: str, candidate_sha: str, wheel_sha256: str, bundle_sha256: str, repository: str = "hamanpaul/paulsha-cortex") -> dict[str, str]`（asset 名稱 → sha256）。目錄形狀：`api/repos/<owner>/<repo>/git/ref/tags/v<ver>.json`、`api/repos/<owner>/<repo>/git/tags/<tag sha>.json`、`api/repos/<owner>/<repo>/releases/tags/v<ver>.json`、`assets/<name>`。
- Produces（`tests/release_ingress_fixtures.py`）：`VERSION`、`CANDIDATE_SHA`、`InputTree`、`ReleaseFixture`、`write_input_tree`、`write_release_source`、`release_json_path`、`refresh_asset_metadata`、`rewrite_manifest`、`fake_venv_runner`。

- [ ] **Step 1: 寫會失敗的測試**

建立 `tests/release_ingress_fixtures.py`：

```python
"""Release-ingress fixtures: a qualification input tree and its local release source."""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from qualification.release_source import build_release_source

VERSION = "0.1.13"
CANDIDATE_SHA = "a" * 40
TOOL_NAMES = ("agy", "claude", "codex", "copilot", "openspec", "srt")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class InputTree:
    root: Path
    bundle: Path
    wheel_name: str
    wheel_sha256: str
    bundle_sha256: str


@dataclass(frozen=True)
class ReleaseFixture:
    source: Path
    tree: InputTree
    assets: dict[str, str]


def write_input_tree(
    root: Path, *, version: str = VERSION, candidate_sha: str = CANDIDATE_SHA
) -> InputTree:
    root.mkdir(parents=True)
    for directory in ("dist", "wheelhouse", "toolchain", "source"):
        (root / directory).mkdir()
    wheel_name = f"paulsha_cortex-{version}-py3-none-any.whl"
    wheel_bytes = f"candidate wheel {version}\n".encode()
    (root / "dist" / wheel_name).write_bytes(wheel_bytes)
    (root / "wheelhouse" / wheel_name).write_bytes(wheel_bytes)
    (root / "wheelhouse" / "PyYAML-6.0.2-py3-none-any.whl").write_bytes(b"pyyaml wheel\n")
    for name in TOOL_NAMES:
        tool = root / "toolchain" / name
        tool.write_bytes(f"{name} tool\n".encode())
        tool.chmod(0o755)
    (root / "source" / "paulsha-cortex.bundle").write_bytes(b"git bundle\n")
    (root / "install-config.yaml").write_text("schema_version: 1\n", encoding="utf-8")

    def row(relative: str) -> dict[str, str]:
        return {"path": relative, "sha256": _sha256(root / relative)}

    bundle = {
        "schema_version": 1,
        "candidate_sha": candidate_sha,
        "wheel": row(f"dist/{wheel_name}"),
        "wheelhouse": [
            row(f"wheelhouse/{wheel_name}"),
            row("wheelhouse/PyYAML-6.0.2-py3-none-any.whl"),
        ],
        "generated_artifacts": [],
        "toolchain": [
            {"name": name, "version": "1.0.0", "shape": "file", **row(f"toolchain/{name}")}
            for name in TOOL_NAMES
        ],
        "source_repositories": [
            {
                "slug": "paulsha-cortex",
                "commit": candidate_sha,
                "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
                **row("source/paulsha-cortex.bundle"),
            }
        ],
    }
    (root / "bundle.json").write_text(json.dumps(bundle, sort_keys=True), encoding="utf-8")
    return InputTree(
        root=root,
        bundle=root / "bundle.json",
        wheel_name=wheel_name,
        wheel_sha256=hashlib.sha256(wheel_bytes).hexdigest(),
        bundle_sha256=_sha256(root / "bundle.json"),
    )


def write_release_source(tmp_path: Path, *, version: str = VERSION) -> ReleaseFixture:
    tree = write_input_tree(tmp_path / "artifacts", version=version)
    source = tmp_path / "release-source"
    assets = build_release_source(
        artifacts=tree.root,
        output=source,
        version=version,
        candidate_sha=CANDIDATE_SHA,
        wheel_sha256=tree.wheel_sha256,
        bundle_sha256=tree.bundle_sha256,
    )
    return ReleaseFixture(source=source, tree=tree, assets=assets)


def release_json_path(source: Path, version: str = VERSION) -> Path:
    return source / "api/repos/hamanpaul/paulsha-cortex/releases/tags" / f"v{version}.json"


def refresh_asset_metadata(source: Path, name: str, *, version: str = VERSION) -> None:
    release_path = release_json_path(source, version)
    document = json.loads(release_path.read_text(encoding="utf-8"))
    asset = source / "assets" / name
    for row in document["assets"]:
        if row["name"] == name:
            row["digest"] = f"sha256:{_sha256(asset)}"
            row["size"] = asset.stat().st_size
    release_path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")


def rewrite_manifest(release: ReleaseFixture, *, bundle_sha256: str) -> None:
    name = f"paulsha-cortex-{VERSION}-qualification.json"
    path = release.source / "assets" / name
    document = json.loads(path.read_text(encoding="utf-8"))
    document["bundle"]["sha256"] = bundle_sha256
    path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    refresh_asset_metadata(release.source, name)


def fake_venv_runner(
    calls: list[tuple[tuple[str, ...], dict[str, str]]],
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Stand in for `python3 -m venv --copies` and the offline pip install."""

    def run(argv, *, check=False, env=None, **_kwargs):
        argv = tuple(argv)
        calls.append((argv, dict(env or {})))
        if argv[:5] == ("/usr/bin/python3", "-I", "-S", "-m", "venv"):
            venv = Path(argv[-1])
            (venv / "bin").mkdir(parents=True)
            (venv / "lib").mkdir()
            python = venv / "bin" / "python"
            python.write_bytes(b"#!/bin/sh\n")
            python.chmod(0o755)
            (venv / "lib64").symlink_to("lib")
        elif "pip" in argv:
            venv = Path(argv[0]).parent.parent
            cli = venv / "bin" / "cortex"
            cli.write_text("#!/bin/sh\n", encoding="utf-8")
            cli.chmod(0o755)
            (venv / "lib" / "site.py").write_text("# site\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run
```

建立 `tests/test_trust_root_install_release_ingress.py`：

```python
"""#1263：release ingress 逐項移植 runbook §1（REST metadata、asset、目錄與 fetcher）。"""
from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

import pytest

import release_ingress_fixtures as fixtures
from paulsha_cortex.trust_root.install.release_ingress import (
    DirectoryReleaseFetcher,
    GitHubReleaseFetcher,
    IngressError,
    ReleaseAsset,
    asset_names,
    assert_private_chain,
    download_asset,
    format_version,
    make_attempt_dir,
    parse_version,
    resolve_release,
    wheel_version,
)

API = "api/repos/hamanpaul/paulsha-cortex"


@pytest.mark.parametrize("value", ["0.1.13", "1.0.0", "10.20.30"])
def test_parse_version_accepts_major_minor_patch(value: str) -> None:
    assert format_version(parse_version(value)) == value


@pytest.mark.parametrize(
    "value", ["0.1", "0.1.13rc1", "01.2.3", "v0.1.13", "0.1.13.dev0", "", None]
)
def test_parse_version_rejects_everything_else(value: object) -> None:
    with pytest.raises(IngressError, match="MAJOR.MINOR.PATCH"):
        parse_version(value)


def test_versions_compare_numerically_and_come_from_the_wheel_filename() -> None:
    assert parse_version("0.1.13") > parse_version("0.1.9")
    assert wheel_version("dist/paulsha_cortex-0.1.13-py3-none-any.whl") == (0, 1, 13)
    with pytest.raises(IngressError, match="no release version"):
        wheel_version("dist/other-0.1.13-py3-none-any.whl")


def test_release_source_lays_out_github_rest_shapes(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)

    assert set(release.assets) == set(asset_names(fixtures.VERSION))
    metadata = json.loads(fixtures.release_json_path(release.source).read_text())
    assert metadata["draft"] is False
    assert metadata["prerelease"] is False
    assert {row["name"]: row["digest"] for row in metadata["assets"]} == {
        name: f"sha256:{digest}" for name, digest in release.assets.items()
    }


def test_resolve_release_takes_expected_values_from_rest_metadata(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)

    metadata = resolve_release(DirectoryReleaseFetcher(release.source), fixtures.VERSION)

    wheel_name, input_name, qualification_name = asset_names(fixtures.VERSION)
    assert metadata.tag == "v0.1.13"
    assert metadata.commit == fixtures.CANDIDATE_SHA
    assert metadata.wheel.name == wheel_name
    assert metadata.wheel.sha256 == release.tree.wheel_sha256
    assert metadata.install_input.sha256 == release.assets[input_name]
    assert metadata.qualification.sha256 == release.assets[qualification_name]


@pytest.mark.parametrize("field", ["draft", "prerelease"])
def test_resolve_release_refuses_a_release_that_is_not_final(
    tmp_path: Path, field: str
) -> None:
    release = fixtures.write_release_source(tmp_path)
    path = fixtures.release_json_path(release.source)
    document = json.loads(path.read_text())
    document[field] = True
    path.write_text(json.dumps(document))

    with pytest.raises(IngressError, match="not a published final release"):
        resolve_release(DirectoryReleaseFetcher(release.source), fixtures.VERSION)


def test_resolve_release_refuses_a_lightweight_tag(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)
    ref = release.source / API / "git/ref/tags/v0.1.13.json"
    document = json.loads(ref.read_text())
    document["object"]["type"] = "commit"
    ref.write_text(json.dumps(document))

    with pytest.raises(IngressError, match="not an annotated tag"):
        resolve_release(DirectoryReleaseFetcher(release.source), fixtures.VERSION)


def test_resolve_release_refuses_a_missing_asset(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)
    path = fixtures.release_json_path(release.source)
    document = json.loads(path.read_text())
    document["assets"] = [
        row for row in document["assets"] if not row["name"].endswith("-qualification.json")
    ]
    path.write_text(json.dumps(document))

    with pytest.raises(IngressError, match="release lacks assets"):
        resolve_release(DirectoryReleaseFetcher(release.source), fixtures.VERSION)


def test_download_stops_when_bytes_differ_from_the_rest_digest(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)
    fetcher = DirectoryReleaseFetcher(release.source)
    metadata = resolve_release(fetcher, fixtures.VERSION)
    tampered = release.source / "assets" / metadata.qualification.name
    original = tampered.read_bytes()
    tampered.write_bytes(original[:-2] + b"X\n")
    target = tmp_path / "release"
    target.mkdir()

    with pytest.raises(IngressError, match="does not match its REST metadata"):
        download_asset(fetcher, metadata.qualification, target)
    assert list(target.iterdir()) == []


def test_download_refuses_more_bytes_than_the_metadata_size(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "assets").mkdir(parents=True)
    (source / "assets" / "big.bin").write_bytes(b"abcd")
    asset = ReleaseAsset(
        "big.bin",
        "0" * 64,
        3,
        "https://github.com/hamanpaul/paulsha-cortex/releases/download/v0.1.13/big.bin",
    )
    target = tmp_path / "release"
    target.mkdir()

    with pytest.raises(IngressError, match="larger than its metadata"):
        download_asset(DirectoryReleaseFetcher(source), asset, target)
    assert not (target / "big.bin").exists()


class _Response(io.BytesIO):
    def __init__(self, payload: bytes, url: str) -> None:
        super().__init__(payload)
        self._url = url

    def geturl(self) -> str:
        return self._url


def test_github_fetcher_uses_the_public_rest_api_without_a_token() -> None:
    seen = []

    def urlopen(request, timeout):
        seen.append(request)
        return _Response(b'{"ok": true}', request.full_url)

    fetcher = GitHubReleaseFetcher(urlopen=urlopen)

    assert fetcher.get_json("repos/hamanpaul/paulsha-cortex/releases/tags/v0.1.13") == {
        "ok": True
    }
    assert seen[0].full_url == (
        "https://api.github.com/repos/hamanpaul/paulsha-cortex/releases/tags/v0.1.13"
    )
    assert seen[0].get_header("Accept") == "application/vnd.github+json"
    assert seen[0].get_header("Authorization") is None


def test_github_fetcher_refuses_asset_urls_outside_the_release() -> None:
    def urlopen(_request, timeout):
        raise AssertionError("must not download")

    asset = ReleaseAsset("x.whl", "0" * 64, 1, "https://example.invalid/x.whl")

    with pytest.raises(IngressError, match="outside hamanpaul/paulsha-cortex"):
        GitHubReleaseFetcher(urlopen=urlopen).copy_asset(asset, io.BytesIO())


def test_github_fetcher_refuses_a_download_that_leaves_https() -> None:
    url = "https://github.com/hamanpaul/paulsha-cortex/releases/download/v0.1.13/x.whl"
    fetcher = GitHubReleaseFetcher(
        urlopen=lambda request, timeout: _Response(b"x", "http://objects.invalid/x.whl")
    )

    with pytest.raises(IngressError, match="left HTTPS"):
        fetcher.copy_asset(ReleaseAsset("x.whl", "0" * 64, 1, url), io.BytesIO())


def test_private_chain_accepts_owner_only_directories(tmp_path: Path) -> None:
    target = tmp_path / "installer" / "0.1.13"
    target.mkdir(parents=True)
    os.chmod(tmp_path / "installer", 0o755)
    os.chmod(target, 0o700)

    assert_private_chain(target, owner_uid=os.getuid(), stop=tmp_path)


def test_private_chain_refuses_a_group_writable_ancestor(tmp_path: Path) -> None:
    target = tmp_path / "installer" / "0.1.13"
    target.mkdir(parents=True)
    os.chmod(tmp_path / "installer", 0o775)

    with pytest.raises(IngressError, match="group/other writable"):
        assert_private_chain(target, owner_uid=os.getuid(), stop=tmp_path)


def test_private_chain_refuses_a_symlinked_ancestor(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "0.1.13").mkdir(parents=True)
    (tmp_path / "installer").symlink_to(real)

    with pytest.raises(IngressError, match="symlink"):
        assert_private_chain(
            tmp_path / "installer" / "0.1.13", owner_uid=os.getuid(), stop=tmp_path
        )


def test_each_ingress_attempt_gets_a_fresh_private_directory(tmp_path: Path) -> None:
    installer = tmp_path / "installer"

    first = make_attempt_dir(installer, "0.1.13", owner_uid=os.getuid(), chain_stop=tmp_path)
    second = make_attempt_dir(installer, "0.1.13", owner_uid=os.getuid(), chain_stop=tmp_path)

    assert first != second
    assert first.parent == second.parent == installer / "0.1.13"
    assert first.name.startswith("attempt-")
    assert stat.S_IMODE((second / "release").stat().st_mode) == 0o700
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_trust_root_install_release_ingress.py -q`
Expected: collection error，`ModuleNotFoundError: No module named 'qualification.release_source'`（fixtures 先 import 它）。

- [ ] **Step 3: 最小實作**

建立 `qualification/release_source.py`（之後 Task 12 會 COPY 進 RC image）：

```python
#!/usr/bin/env python3
"""Write a GitHub-REST-shaped local release source for RC `cortex upgrade` drills.

The release profile runs with ``--network none`` and qualifies a single
candidate, so the drill cannot fetch a published release.  This helper packs
the mounted qualification input exactly as the release workflow does (one
``qualification-input/`` prefix), writes a passed release qualification
manifest naming the candidate, and lays out the REST documents that
``cortex upgrade --release-source`` reads:

    api/repos/<owner>/<repo>/git/ref/tags/v<version>.json
    api/repos/<owner>/<repo>/git/tags/<tag object sha>.json
    api/repos/<owner>/<repo>/releases/tags/v<version>.json
    assets/<asset name>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tarfile
from pathlib import Path

REPOSITORY = "hamanpaul/paulsha-cortex"
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
SHA40 = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize(member: tarfile.TarInfo) -> tarfile.TarInfo:
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    member.mtime = 0
    return member


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )


def build_release_source(
    *,
    artifacts: Path,
    output: Path,
    version: str,
    candidate_sha: str,
    wheel_sha256: str,
    bundle_sha256: str,
    repository: str = REPOSITORY,
) -> dict[str, str]:
    if VERSION.fullmatch(version) is None:
        raise ValueError("version must be MAJOR.MINOR.PATCH")
    if SHA40.fullmatch(candidate_sha) is None:
        raise ValueError("candidate SHA must be 40 lowercase hex characters")
    for label, value in (("wheel", wheel_sha256), ("bundle", bundle_sha256)):
        if SHA256.fullmatch(value) is None:
            raise ValueError(f"{label} SHA-256 must be 64 lowercase hex characters")
    if output.exists() or output.is_symlink():
        raise ValueError(f"release source output already exists: {output}")
    wheel_name = f"paulsha_cortex-{version}-py3-none-any.whl"
    wheel = artifacts / "dist" / wheel_name
    if wheel.is_symlink() or not wheel.is_file() or _sha256(wheel) != wheel_sha256:
        raise ValueError("the artifacts do not hold the candidate wheel for this version")
    if _sha256(artifacts / "bundle.json") != bundle_sha256:
        raise ValueError("the artifacts bundle.json does not match the candidate bundle")
    assets = output / "assets"
    assets.mkdir(parents=True, mode=0o700)
    shutil.copyfile(wheel, assets / wheel_name)
    archive_name = f"paulsha-cortex-{version}-install-input.tar.gz"
    with tarfile.open(assets / archive_name, mode="w:gz") as archive:
        archive.add(str(artifacts), arcname="qualification-input", filter=_normalize)
    manifest_name = f"paulsha-cortex-{version}-qualification.json"
    _write_json(
        assets / manifest_name,
        {
            "schema_version": 2,
            "profile": "release",
            "status": "passed",
            "candidate_sha": candidate_sha,
            "wheel": {"filename": wheel_name, "sha256": wheel_sha256},
            "bundle": {"sha256": bundle_sha256},
        },
    )
    tag = f"v{version}"
    tag_object = hashlib.sha1(
        f"cortex-release-source:{repository}:{tag}".encode(), usedforsecurity=False
    ).hexdigest()
    api = output / "api" / "repos" / repository
    _write_json(
        api / "git" / "ref" / "tags" / f"{tag}.json",
        {"ref": f"refs/tags/{tag}", "object": {"type": "tag", "sha": tag_object}},
    )
    _write_json(
        api / "git" / "tags" / f"{tag_object}.json",
        {
            "tag": tag,
            "sha": tag_object,
            "message": f"{tag}\n",
            "object": {"type": "commit", "sha": candidate_sha},
        },
    )
    digests = {
        name: _sha256(assets / name) for name in (wheel_name, archive_name, manifest_name)
    }
    _write_json(
        api / "releases" / "tags" / f"{tag}.json",
        {
            "tag_name": tag,
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": name,
                    "size": (assets / name).stat().st_size,
                    "digest": f"sha256:{digest}",
                    "browser_download_url": (
                        f"https://github.com/{repository}/releases/download/{tag}/{name}"
                    ),
                }
                for name, digest in sorted(digests.items())
            ],
        },
    )
    return digests


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--bundle-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        digests = build_release_source(
            artifacts=args.artifacts,
            output=args.output,
            version=args.version,
            candidate_sha=args.candidate_sha,
            wheel_sha256=args.wheel_sha256,
            bundle_sha256=args.bundle_sha256,
        )
    except (OSError, ValueError, tarfile.TarError) as exc:
        print(f"release source failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"output": str(args.output), "assets": digests}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

建立 `paulsha_cortex/trust_root/install/release_ingress.py`（Task 6 會在檔尾續寫）：

```python
"""Release ingress for `cortex upgrade` (#1263): runbook §1 as code.

Every expected value comes from GitHub Releases REST metadata (the annotated
tag's commit target and each asset ``digest``), never from downloaded bytes.
The qualification manifest is verified before the install-input archive is
opened, the archive topology before extraction, every bundle file before any
candidate code runs, and the candidate CLI is then sealed by a tree digest.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Protocol

from .backend import _run
from .core import InstallError

OFFICIAL_REPOSITORY = "hamanpaul/paulsha-cortex"
GITHUB_API_ROOT = "https://api.github.com"
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_WHEEL_FILENAME = re.compile(r"paulsha_cortex-(?P<version>[^-]+)-py3-none-any\.whl")
_SHA40 = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_METADATA_BYTES = 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_OPEN_NEW = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class IngressError(InstallError):
    """Release ingress refused metadata, an asset, a manifest or the input tree."""


def _hex(value: object, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def parse_version(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise IngressError(f"version must be MAJOR.MINOR.PATCH: {value!r}")
    major, minor, patch = (int(part) for part in value.split("."))
    return major, minor, patch


def format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def wheel_version(wheel_path: object) -> tuple[int, int, int]:
    if not isinstance(wheel_path, str) or not wheel_path:
        raise IngressError("plan candidate wheel path is missing")
    match = _WHEEL_FILENAME.fullmatch(PurePosixPath(wheel_path).name)
    if match is None:
        raise IngressError(
            f"candidate wheel filename carries no release version: {wheel_path}"
        )
    return parse_version(match.group("version"))


def asset_names(version: str) -> tuple[str, str, str]:
    """Wheel, install-input archive and qualification manifest of one release."""

    return (
        f"paulsha_cortex-{version}-py3-none-any.whl",
        f"paulsha-cortex-{version}-install-input.tar.gz",
        f"paulsha-cortex-{version}-qualification.json",
    )


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    sha256: str
    size: int
    url: str


@dataclass(frozen=True)
class ReleaseMetadata:
    version: str
    tag: str
    commit: str
    wheel: ReleaseAsset
    install_input: ReleaseAsset
    qualification: ReleaseAsset


class ReleaseFetcher(Protocol):
    def get_json(self, api_path: str) -> object: ...

    def copy_asset(self, asset: ReleaseAsset, destination: BinaryIO) -> None: ...


def _copy_limited(source: BinaryIO, destination: BinaryIO, asset: ReleaseAsset) -> None:
    remaining = asset.size
    while True:
        chunk = source.read(min(_CHUNK_BYTES, remaining + 1))
        if not chunk:
            break
        if len(chunk) > remaining:
            raise IngressError(f"release asset is larger than its metadata: {asset.name}")
        destination.write(chunk)
        remaining -= len(chunk)
    if remaining:
        raise IngressError(f"release asset is shorter than its metadata: {asset.name}")


def _decode_metadata(raw: bytes, api_path: str) -> object:
    if len(raw) > _MAX_METADATA_BYTES:
        raise IngressError(f"GitHub REST metadata is too large: {api_path}")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IngressError(f"GitHub REST metadata is not JSON: {api_path}") from exc


class GitHubReleaseFetcher:
    """Public GitHub REST over HTTPS; no token is ever sent."""

    def __init__(
        self,
        repository: str = OFFICIAL_REPOSITORY,
        *,
        urlopen: Callable[..., object] = urllib.request.urlopen,
        timeout: float = 60.0,
    ) -> None:
        self._repository = repository
        self._urlopen = urlopen
        self._timeout = timeout

    def get_json(self, api_path: str) -> object:
        request = urllib.request.Request(
            f"{GITHUB_API_ROOT}/{api_path}",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "paulsha-cortex-upgrade",
            },
        )
        try:
            with self._urlopen(request, timeout=self._timeout) as response:  # type: ignore[attr-defined]
                raw = response.read(_MAX_METADATA_BYTES + 1)
        except (urllib.error.URLError, OSError) as exc:
            raise IngressError(f"GitHub REST request failed: {api_path}: {exc}") from exc
        return _decode_metadata(raw, api_path)

    def copy_asset(self, asset: ReleaseAsset, destination: BinaryIO) -> None:
        prefix = f"https://github.com/{self._repository}/releases/download/"
        if not asset.url.startswith(prefix) or not asset.url.endswith(f"/{asset.name}"):
            raise IngressError(
                f"release asset URL is outside {self._repository}: {asset.name}"
            )
        request = urllib.request.Request(
            asset.url,
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": "paulsha-cortex-upgrade",
            },
        )
        try:
            with self._urlopen(request, timeout=self._timeout) as response:  # type: ignore[attr-defined]
                if not str(response.geturl()).startswith("https://"):
                    raise IngressError(f"release asset download left HTTPS: {asset.name}")
                _copy_limited(response, destination, asset)
        except (urllib.error.URLError, OSError) as exc:
            raise IngressError(f"release asset download failed: {asset.name}: {exc}") from exc


class DirectoryReleaseFetcher:
    """GitHub-REST-shaped release source on local disk (RC qualification only).

    ``api/<api_path>.json`` holds the REST documents and ``assets/<name>`` the
    asset bytes, as written by ``qualification/release_source.py``.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _file(self, relative: PurePosixPath) -> Path:
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise IngressError(f"release source path is unsafe: {relative}")
        cursor = self._root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise IngressError(f"release source contains a symlink: {relative}")
        if not cursor.is_file():
            raise IngressError(f"release source lacks {relative}")
        return cursor

    def get_json(self, api_path: str) -> object:
        path = self._file(PurePosixPath("api") / f"{api_path}.json")
        return _decode_metadata(path.read_bytes(), api_path)

    def copy_asset(self, asset: ReleaseAsset, destination: BinaryIO) -> None:
        path = self._file(PurePosixPath("assets") / asset.name)
        with path.open("rb") as source:
            _copy_limited(source, destination, asset)


def resolve_release(
    fetcher: ReleaseFetcher, version: str, *, repository: str = OFFICIAL_REPOSITORY
) -> ReleaseMetadata:
    """Expected identity and digests of release ``v<version>``, from REST only."""

    parse_version(version)
    tag = f"v{version}"
    ref = fetcher.get_json(f"repos/{repository}/git/ref/tags/{tag}")
    ref_object = ref.get("object") if isinstance(ref, dict) else None
    if (
        not isinstance(ref_object, dict)
        or ref_object.get("type") != "tag"
        or _hex(ref_object.get("sha"), _SHA40) is None
    ):
        raise IngressError(f"{tag} is not an annotated tag")
    tag_document = fetcher.get_json(f"repos/{repository}/git/tags/{ref_object['sha']}")
    target = tag_document.get("object") if isinstance(tag_document, dict) else None
    if (
        not isinstance(tag_document, dict)
        or tag_document.get("tag") != tag
        or not isinstance(target, dict)
        or target.get("type") != "commit"
        or _hex(target.get("sha"), _SHA40) is None
    ):
        raise IngressError(f"{tag} does not point at a commit")
    release = fetcher.get_json(f"repos/{repository}/releases/tags/{tag}")
    if not isinstance(release, dict) or release.get("tag_name") != tag:
        raise IngressError(f"release {tag} metadata is invalid")
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise IngressError(f"{tag} is not a published final release")
    raw_assets = release.get("assets")
    if not isinstance(raw_assets, list):
        raise IngressError(f"release {tag} lists no assets")
    names = asset_names(version)
    found: dict[str, ReleaseAsset] = {}
    for raw in raw_assets:
        if not isinstance(raw, dict) or raw.get("name") not in names:
            continue
        name = str(raw["name"])
        if name in found:
            raise IngressError(f"release lists asset {name} twice")
        digest = raw.get("digest")
        size = raw.get("size")
        url = raw.get("browser_download_url")
        if (
            not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or _hex(digest[len("sha256:"):], _SHA256) is None
        ):
            raise IngressError(f"release asset {name} has no sha256 digest")
        if type(size) is not int or size <= 0:
            raise IngressError(f"release asset {name} has no size")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise IngressError(f"release asset {name} has no HTTPS download URL")
        found[name] = ReleaseAsset(name, digest[len("sha256:"):], size, url)
    missing = [name for name in names if name not in found]
    if missing:
        raise IngressError("release lacks assets: " + ", ".join(missing))
    return ReleaseMetadata(
        version=version,
        tag=tag,
        commit=str(target["sha"]),
        wheel=found[names[0]],
        install_input=found[names[1]],
        qualification=found[names[2]],
    )


class _HashingWriter:
    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self.digest = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self.digest.update(data)
        return self._stream.write(data)


def download_asset(fetcher: ReleaseFetcher, asset: ReleaseAsset, release_dir: Path) -> Path:
    """Write one asset into a fresh private file and check it against REST."""

    path = release_dir / asset.name
    descriptor = os.open(path, _OPEN_NEW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            writer = _HashingWriter(stream)
            fetcher.copy_asset(asset, writer)  # type: ignore[arg-type]
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if writer.digest.hexdigest() != asset.sha256:
        path.unlink()
        raise IngressError(
            f"release asset digest does not match its REST metadata: {asset.name}"
        )
    return path


def assert_private_chain(path: Path, *, owner_uid: int, stop: Path = Path("/")) -> None:
    """Every directory from ``path`` up to ``stop`` is owner-owned, unwritable by others."""

    cursor = path
    while True:
        observed = cursor.lstat()
        if stat.S_ISLNK(observed.st_mode):
            raise IngressError(f"release ingress path contains a symlink: {cursor}")
        if (
            not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != owner_uid
            or stat.S_IMODE(observed.st_mode) & 0o022
        ):
            raise IngressError(
                f"release ingress directory is not owned by uid {owner_uid} "
                f"or is group/other writable: {cursor}"
            )
        if cursor == stop or cursor.parent == cursor:
            return
        cursor = cursor.parent


def make_attempt_dir(
    installer_root: Path,
    version: str,
    *,
    owner_uid: int,
    chain_stop: Path = Path("/"),
) -> Path:
    """A new attempt directory per run; a rerun of the same version never reuses one."""

    parse_version(version)
    for directory in (installer_root, installer_root / version):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    attempt = installer_root / version / f"attempt-{stamp}-{secrets.token_hex(4)}"
    os.mkdir(attempt, 0o700)
    os.mkdir(attempt / "release", 0o700)
    assert_private_chain(attempt / "release", owner_uid=owner_uid, stop=chain_stop)
    return attempt
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_trust_root_install_release_ingress.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add paulsha_cortex/trust_root/install/release_ingress.py qualification/release_source.py tests/release_ingress_fixtures.py tests/test_trust_root_install_release_ingress.py
git commit -m "feat(trust-root): release ingress 的 REST metadata、fetcher 與 asset 下載（#1263）"
```

---

### Task 6: release ingress（二）：manifest、archive、bundle 逐檔驗證、封存 venv

**Files:**
- Modify: `paulsha_cortex/trust_root/install/release_ingress.py`（檔尾續寫）
- Test: `tests/test_trust_root_install_release_ingress.py`（檔尾續寫）

**Interfaces:**
- Consumes: Task 5 的 `resolve_release`、`download_asset`、`make_attempt_dir`、`ReleaseMetadata`、`ReleaseFetcher`、`IngressError`；`backend._run(argv, *, check=False, env=None, uid=None, gid=None)`。
- Produces:
  - `@dataclass(frozen=True) QualificationAuthority(candidate_sha: str, wheel_filename: str, wheel_sha256: str, bundle_sha256: str)`、`read_qualification_authority(path: Path) -> QualificationAuthority`
  - `check_archive_topology(path: Path) -> None`、`extract_install_input(archive: Path, input_root: Path) -> None`
  - `verify_input_tree(bundle: Path, *, candidate_sha: str, wheel_sha256: str, owner_uid: int) -> None`（runbook 的 owner／mode／symlink／nlink 全樹檢查，加上 `qualification/verify_bundle.py` 的 bundle 逐檔與清單完全一致檢查）
  - `write_bootstrap_requirements(bundle: Path, requirements: Path) -> None`
  - `build_sealed_venv(venv: Path, requirements: Path, *, owner_uid: int, run: Callable[..., object] = _run) -> Path`（回傳 `venv/bin/cortex`）
  - `tree_sha256(root: Path, *, owner_uid: int) -> str`
  - `@dataclass(frozen=True) SealedCandidate(metadata: ReleaseMetadata, attempt_dir: Path, input_root: Path, bundle: Path, install_config: Path, venv: Path, cli: Path, tree_sha256: str, owner_uid: int)`，方法 `assert_unchanged() -> None`
  - `ingest_release(version: str, *, fetcher: ReleaseFetcher, installer_root: Path, repository: str = OFFICIAL_REPOSITORY, owner_uid: int = 0, chain_stop: Path = Path("/"), run: Callable[..., object] = _run) -> SealedCandidate`

- [ ] **Step 1: 寫會失敗的測試**

在 `tests/test_trust_root_install_release_ingress.py` 的 import 區塊加 `import tarfile`，並在 `from paulsha_cortex.trust_root.install.release_ingress import (...)` 加入 `build_sealed_venv`、`check_archive_topology`、`ingest_release`、`read_qualification_authority`、`tree_sha256`、`verify_input_tree`；再加一行 `from qualification.verify_bundle import validate_bundle as qualification_validate_bundle`。檔尾加入：

```python
def _ingest(tmp_path: Path, release, calls=None):
    return ingest_release(
        fixtures.VERSION,
        fetcher=DirectoryReleaseFetcher(release.source),
        installer_root=tmp_path / "installer",
        owner_uid=os.getuid(),
        chain_stop=tmp_path,
        run=fixtures.fake_venv_runner(calls if calls is not None else []),
    )


def test_ingest_seals_the_candidate_cli_after_every_check(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)
    calls: list = []

    sealed = _ingest(tmp_path, release, calls)

    _wheel, input_name, qualification_name = asset_names(fixtures.VERSION)
    assert sorted(path.name for path in (sealed.attempt_dir / "release").iterdir()) == sorted(
        [input_name, qualification_name]
    )
    assert sealed.metadata.commit == fixtures.CANDIDATE_SHA
    assert sealed.cli == sealed.venv / "bin" / "cortex"
    assert sealed.bundle == sealed.input_root / "bundle.json"
    assert sealed.install_config == sealed.input_root / "install-config.yaml"
    assert sealed.tree_sha256 == tree_sha256(sealed.venv, owner_uid=os.getuid())
    requirements = (sealed.attempt_dir / "bootstrap-requirements.txt").read_text().splitlines()
    assert requirements == sorted(requirements)
    assert all(
        line.startswith("file://") and " --hash=sha256:" in line for line in requirements
    )
    assert stat.S_IMODE((sealed.input_root / "toolchain" / "codex").stat().st_mode) == 0o755
    assert stat.S_IMODE((sealed.input_root / "bundle.json").stat().st_mode) == 0o644
    assert calls[0][0][:6] == ("/usr/bin/python3", "-I", "-S", "-m", "venv", "--copies")
    pip = next(argv for argv, _env in calls if "pip" in argv)
    for flag in ("--no-index", "--no-deps", "--only-binary=:all:", "--require-hashes"):
        assert flag in pip


def test_sealed_candidate_detects_a_changed_cli_tree(tmp_path: Path) -> None:
    sealed = _ingest(tmp_path, fixtures.write_release_source(tmp_path))

    sealed.assert_unchanged()
    (sealed.venv / "bin" / "cortex").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    with pytest.raises(IngressError, match="changed since ingress"):
        sealed.assert_unchanged()


def test_rerunning_the_same_version_ingests_into_a_new_attempt(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)

    first = _ingest(tmp_path, release)
    second = _ingest(tmp_path, release)

    assert first.attempt_dir != second.attempt_dir
    assert second.tree_sha256 == tree_sha256(second.venv, owner_uid=os.getuid())


def test_manifest_must_name_the_tag_target_before_extraction(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)
    tag_document = next((release.source / API / "git/tags").iterdir())
    document = json.loads(tag_document.read_text())
    document["object"]["sha"] = "f" * 40
    tag_document.write_text(json.dumps(document))

    with pytest.raises(IngressError, match="manifest candidate does not match the tag target"):
        _ingest(tmp_path, release)
    attempts = list((tmp_path / "installer" / fixtures.VERSION).iterdir())
    assert attempts and all(not (attempt / "input").exists() for attempt in attempts)


def test_bundle_must_match_the_manifest_digest(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)
    fixtures.rewrite_manifest(release, bundle_sha256="0" * 64)

    with pytest.raises(IngressError, match="bundle.json does not match"):
        _ingest(tmp_path, release)


def test_manifest_must_be_a_passed_release_attestation(tmp_path: Path) -> None:
    path = tmp_path / "qualification.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "profile": "deployment-canary",
                "status": "passed",
                "candidate_sha": "a" * 40,
                "wheel": {"filename": "x.whl", "sha256": "b" * 64},
                "bundle": {"sha256": "c" * 64},
            }
        )
    )

    with pytest.raises(IngressError, match="not a passed release attestation"):
        read_qualification_authority(path)


def _tar(path: Path, members: list[tuple[str, bytes | None, str | None]]) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for name, payload, link in members:
            info = tarfile.TarInfo(name)
            if link is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = link
                archive.addfile(info)
            elif payload is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
    return path


@pytest.mark.parametrize(
    "members",
    [
        [("qualification-input", None, None), ("qualification-input/link", None, "/etc/passwd")],
        [("qualification-input", None, None), ("qualification-input/../escape", b"x", None)],
        [("other-root/bundle.json", b"{}", None)],
        [("/qualification-input/bundle.json", b"{}", None)],
        [("qualification-input/a", b"1", None), ("qualification-input/a", b"2", None)],
    ],
)
def test_archive_topology_refuses_unsafe_members(tmp_path: Path, members) -> None:
    with pytest.raises(IngressError, match="topology is unsafe"):
        check_archive_topology(_tar(tmp_path / "input.tar.gz", members))


def _extra_wheelhouse(root: Path) -> None:
    (root / "wheelhouse" / "extra.whl").write_bytes(b"x")


def _missing_tool(root: Path) -> None:
    (root / "toolchain" / "srt").unlink()


def _tampered_wheel(root: Path) -> None:
    (root / "dist" / f"paulsha_cortex-{fixtures.VERSION}-py3-none-any.whl").write_bytes(
        b"tampered\n"
    )


def _extra_root_entry(root: Path) -> None:
    (root / "notes.txt").write_text("x", encoding="utf-8")


def _symlinked_source(root: Path) -> None:
    source = root / "source" / "paulsha-cortex.bundle"
    real = root.parent / "elsewhere.bundle"
    real.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(real)


def test_input_tree_validation_accepts_what_verify_bundle_accepts(tmp_path: Path) -> None:
    tree = fixtures.write_input_tree(tmp_path / "base")

    verify_input_tree(
        tree.bundle,
        candidate_sha=fixtures.CANDIDATE_SHA,
        wheel_sha256=tree.wheel_sha256,
        owner_uid=os.getuid(),
    )
    qualification_validate_bundle(
        tree.bundle, candidate_sha=fixtures.CANDIDATE_SHA, wheel_sha256=tree.wheel_sha256
    )


@pytest.mark.parametrize(
    "mutate",
    [_extra_wheelhouse, _missing_tool, _tampered_wheel, _extra_root_entry, _symlinked_source],
    ids=lambda mutate: mutate.__name__,
)
def test_input_tree_mutations_are_refused_like_verify_bundle(tmp_path: Path, mutate) -> None:
    tree = fixtures.write_input_tree(tmp_path / "case")
    mutate(tree.root)

    with pytest.raises(IngressError):
        verify_input_tree(
            tree.bundle,
            candidate_sha=fixtures.CANDIDATE_SHA,
            wheel_sha256=tree.wheel_sha256,
            owner_uid=os.getuid(),
        )
    with pytest.raises((ValueError, OSError)):
        qualification_validate_bundle(
            tree.bundle, candidate_sha=fixtures.CANDIDATE_SHA, wheel_sha256=tree.wheel_sha256
        )


def test_input_tree_refuses_group_writable_files(tmp_path: Path) -> None:
    tree = fixtures.write_input_tree(tmp_path / "case")
    (tree.root / "install-config.yaml").chmod(0o664)

    with pytest.raises(IngressError, match="ownership/mode is unsafe"):
        verify_input_tree(
            tree.bundle,
            candidate_sha=fixtures.CANDIDATE_SHA,
            wheel_sha256=tree.wheel_sha256,
            owner_uid=os.getuid(),
        )


def test_sealed_venv_is_built_offline_with_copies_and_no_symlinks(tmp_path: Path) -> None:
    calls: list = []
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("", encoding="utf-8")

    cli = build_sealed_venv(
        tmp_path / "venv",
        requirements,
        owner_uid=os.getuid(),
        run=fixtures.fake_venv_runner(calls),
    )

    assert cli == tmp_path / "venv/bin/cortex"
    assert not os.path.lexists(tmp_path / "venv/lib64")
    assert stat.S_IMODE((tmp_path / "venv").stat().st_mode) == 0o755
    assert stat.S_IMODE(cli.stat().st_mode) == 0o755
    assert stat.S_IMODE((tmp_path / "venv/lib/site.py").stat().st_mode) == 0o644
    assert calls[0][1] == {
        "HOME": "/root",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
    }


def test_sealed_venv_refuses_an_existing_path(tmp_path: Path) -> None:
    (tmp_path / "venv").mkdir()

    with pytest.raises(IngressError, match="already exists"):
        build_sealed_venv(
            tmp_path / "venv",
            tmp_path / "requirements.txt",
            owner_uid=os.getuid(),
            run=fixtures.fake_venv_runner([]),
        )


def test_tree_digest_tracks_content_and_refuses_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "venv"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "x").write_text("x", encoding="utf-8")
    digest = tree_sha256(root, owner_uid=os.getuid())

    (root / "bin" / "x").write_text("y", encoding="utf-8")
    assert tree_sha256(root, owner_uid=os.getuid()) != digest
    (root / "bin" / "link").symlink_to("x")
    with pytest.raises(IngressError, match="unsafe candidate CLI"):
        tree_sha256(root, owner_uid=os.getuid())
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_trust_root_install_release_ingress.py -q`
Expected: collection error，`ImportError: cannot import name 'build_sealed_venv' from 'paulsha_cortex.trust_root.install.release_ingress'`。

- [ ] **Step 3: 最小實作**

在 `paulsha_cortex/trust_root/install/release_ingress.py` 檔尾續寫：

```python
# --- qualification manifest, archive and input tree (runbook §1) ---------------

_INPUT_ROOT_ENTRIES = frozenset(
    {"bundle.json", "install-config.yaml", "dist", "wheelhouse", "toolchain", "source"}
)
_BUNDLE_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "candidate_sha",
        "wheel",
        "wheelhouse",
        "generated_artifacts",
        "toolchain",
        "source_repositories",
    }
)
_TOOL_NAMES = frozenset({"codex", "claude", "copilot", "agy", "srt", "openspec"})
_VENV_ENV = {
    "HOME": "/root",
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONNOUSERSITE": "1",
}


@dataclass(frozen=True)
class QualificationAuthority:
    candidate_sha: str
    wheel_filename: str
    wheel_sha256: str
    bundle_sha256: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_qualification_authority(path: Path) -> QualificationAuthority:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IngressError("qualification manifest is not readable JSON") from exc
    wheel = document.get("wheel") if isinstance(document, dict) else None
    bundle = document.get("bundle") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 2
        or document.get("profile") != "release"
        or document.get("status") != "passed"
        or not isinstance(wheel, dict)
        or not isinstance(bundle, dict)
    ):
        raise IngressError("qualification manifest is not a passed release attestation")
    candidate_sha = _hex(document.get("candidate_sha"), _SHA40)
    filename = wheel.get("filename")
    wheel_sha = _hex(wheel.get("sha256"), _SHA256)
    bundle_sha = _hex(bundle.get("sha256"), _SHA256)
    if (
        candidate_sha is None
        or not isinstance(filename, str)
        or not filename
        or "/" in filename
        or wheel_sha is None
        or bundle_sha is None
    ):
        raise IngressError("qualification manifest identity is invalid")
    return QualificationAuthority(candidate_sha, filename, wheel_sha, bundle_sha)


def check_archive_topology(path: Path) -> None:
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            seen: set[str] = set()
            for member in archive.getmembers():
                pure = PurePosixPath(member.name)
                if (
                    pure.is_absolute()
                    or not pure.parts
                    or pure.parts[0] != "qualification-input"
                    or ".." in pure.parts
                    or member.name in seen
                    or not (member.isdir() or member.isfile())
                ):
                    raise IngressError("install-input archive topology is unsafe")
                seen.add(member.name)
    except (OSError, tarfile.TarError) as exc:
        raise IngressError(f"install-input archive is unreadable: {exc}") from exc


def extract_install_input(archive: Path, input_root: Path) -> None:
    """Strip ``qualification-input/``; private modes, only the user-x bit survives."""

    try:
        with tarfile.open(archive, mode="r:gz") as stream:
            for member in stream.getmembers():
                parts = PurePosixPath(member.name).parts[1:]
                if not parts:
                    continue
                target = input_root.joinpath(*parts)
                if member.isdir():
                    os.mkdir(target, 0o700)
                    continue
                source = stream.extractfile(member)
                if source is None:
                    raise IngressError(f"install-input member is unreadable: {member.name}")
                mode = 0o700 if member.mode & 0o100 else 0o600
                descriptor = os.open(target, _OPEN_NEW, mode)
                with os.fdopen(descriptor, "wb") as output:
                    while chunk := source.read(_CHUNK_BYTES):
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
    except (OSError, tarfile.TarError) as exc:
        raise IngressError(f"install-input extraction failed: {exc}") from exc


def _single_link_regular(path: Path, *, label: str) -> None:
    observed = path.lstat()
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise IngressError(f"{label} must be a single-link regular file")


def _plain_directory(path: Path, *, label: str) -> None:
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise IngressError(f"{label} must be a real directory")


def _bundle_entry(raw: object, *, root: Path, label: str) -> str:
    if not isinstance(raw, dict) or set(raw) != {"path", "sha256"}:
        raise IngressError(f"{label} must contain only path and sha256")
    relative = raw["path"]
    if not isinstance(relative, str) or not relative:
        raise IngressError(f"{label}.path must be a non-empty string")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\x00" in relative:
        raise IngressError(f"{label}.path is unsafe")
    expected = _hex(raw["sha256"], _SHA256)
    if expected is None:
        raise IngressError(f"{label}.sha256 is invalid")
    path = root / pure
    _single_link_regular(path, label=f"{label}.path")
    cursor = root
    for part in pure.parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise IngressError(f"{label}.path has a symlink ancestor")
    if _sha256_file(path) != expected:
        raise IngressError(f"{label}.sha256 does not match {relative}")
    return relative


def _directory_files(root: Path, directory: str, *, label: str) -> set[str]:
    found: set[str] = set()
    for path in (root / directory).iterdir():
        _single_link_regular(path, label=label)
        found.add(path.relative_to(root).as_posix())
    return found


def _validate_bundle_inventory(bundle: Path, *, candidate_sha: str, wheel_sha256: str) -> None:
    """Port of ``qualification/verify_bundle.py::validate_bundle`` plus runbook §1."""

    _single_link_regular(bundle, label="bundle")
    root = bundle.parent
    _plain_directory(root, label="qualification input root")
    if {path.name for path in root.iterdir()} != _INPUT_ROOT_ENTRIES:
        raise IngressError("qualification input root inventory is not exact")
    _single_link_regular(root / "install-config.yaml", label="install config")
    for directory in ("dist", "wheelhouse", "toolchain", "source"):
        _plain_directory(root / directory, label=f"{directory} root")
    try:
        payload = json.loads(bundle.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IngressError("bundle is not JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _BUNDLE_ROOT_KEYS:
        raise IngressError("bundle has missing or unknown root fields")
    if payload["schema_version"] != 1 or isinstance(payload["schema_version"], bool):
        raise IngressError("bundle schema_version must be 1")
    if payload["candidate_sha"] != candidate_sha:
        raise IngressError("bundle candidate_sha does not match")
    wheel_path = _bundle_entry(payload["wheel"], root=root, label="wheel")
    if payload["wheel"]["sha256"] != wheel_sha256:
        raise IngressError("bundle wheel sha256 does not match the selected candidate")
    if not wheel_path.startswith("dist/"):
        raise IngressError("bundle wheel must be under dist/")
    if _directory_files(root, "dist", label="dist entry") != {wheel_path}:
        raise IngressError("dist inventory must contain only the declared candidate wheel")
    wheelhouse = payload["wheelhouse"]
    if not isinstance(wheelhouse, list) or not wheelhouse:
        raise IngressError("bundle wheelhouse must be a non-empty array")
    if any(
        not isinstance(raw, dict)
        or not isinstance(raw.get("path"), str)
        or not raw["path"].endswith(".whl")
        or PurePosixPath(raw["path"]).parent != PurePosixPath("wheelhouse")
        for raw in wheelhouse
    ):
        raise IngressError("bundle wheelhouse must list wheels directly under wheelhouse/")
    declared = [
        _bundle_entry(raw, root=root, label=f"wheelhouse[{index}]")
        for index, raw in enumerate(wheelhouse)
    ]
    if len(set(declared)) != len(declared):
        raise IngressError("wheelhouse manifest paths are duplicated")
    if set(declared) != _directory_files(root, "wheelhouse", label="wheelhouse entry"):
        raise IngressError("wheelhouse inventory is incomplete or contains an undeclared file")
    if not any(raw.get("sha256") == wheel_sha256 for raw in wheelhouse):
        raise IngressError("wheelhouse does not contain the exact candidate wheel")
    generated = payload["generated_artifacts"]
    if not isinstance(generated, list):
        raise IngressError("generated_artifacts must be an array")
    generated_paths = [
        _bundle_entry(raw, root=root, label=f"generated_artifacts[{index}]")
        for index, raw in enumerate(generated)
    ]
    if len(set(generated_paths)) != len(generated_paths):
        raise IngressError("bundle contains duplicate generated artifact paths")
    tools = payload["toolchain"]
    if not isinstance(tools, list) or not tools:
        raise IngressError("toolchain must be a non-empty array")
    tool_names: set[str] = set()
    tool_paths: set[str] = set()
    for index, raw in enumerate(tools):
        if not isinstance(raw, dict):
            raise IngressError(f"toolchain[{index}] must be an object")
        required = {"name", "version", "shape", "path", "sha256"}
        if raw.get("shape") == "tree":
            required |= {"entrypoint", "installed_sha256"}
        if set(raw) != required:
            raise IngressError(f"toolchain[{index}] has missing or unknown fields")
        name = raw.get("name")
        version = raw.get("version")
        if (
            not isinstance(name, str)
            or not name
            or "/" in name
            or name in tool_names
            or not isinstance(version, str)
            or not version
            or raw.get("shape") not in {"file", "tree"}
        ):
            raise IngressError(f"toolchain[{index}] identity is invalid")
        if raw.get("shape") == "tree":
            entrypoint = raw.get("entrypoint")
            if (
                not isinstance(entrypoint, str)
                or PurePosixPath(entrypoint).is_absolute()
                or ".." in PurePosixPath(entrypoint).parts
                or _hex(raw.get("installed_sha256"), _SHA256) is None
            ):
                raise IngressError(f"toolchain[{index}] tree metadata is invalid")
        tool_names.add(name)
        tool_paths.add(
            _bundle_entry(
                {"path": raw["path"], "sha256": raw["sha256"]},
                root=root,
                label=f"toolchain[{index}]",
            )
        )
    if tool_names != _TOOL_NAMES:
        raise IngressError("toolchain inventory is incomplete")
    if tool_paths != _directory_files(root, "toolchain", label="toolchain entry"):
        raise IngressError("toolchain directory has undeclared or missing artifacts")
    repositories = payload["source_repositories"]
    if not isinstance(repositories, list) or not repositories:
        raise IngressError("source_repositories must be a non-empty array")
    repository_paths: set[str] = set()
    slugs: set[str] = set()
    for index, raw in enumerate(repositories):
        if not isinstance(raw, dict) or set(raw) != {
            "slug",
            "commit",
            "remote",
            "path",
            "sha256",
        }:
            raise IngressError(f"source_repositories[{index}] fields are invalid")
        slug = raw.get("slug")
        if (
            not isinstance(slug, str)
            or not slug
            or "/" in slug
            or slug in slugs
            or _hex(raw.get("commit"), _SHA40) is None
            or not isinstance(raw.get("remote"), str)
            or not raw["remote"].startswith("https://")
        ):
            raise IngressError(f"source_repositories[{index}] identity is invalid")
        slugs.add(slug)
        repository_paths.add(
            _bundle_entry(
                {"path": raw["path"], "sha256": raw["sha256"]},
                root=root,
                label=f"source_repositories[{index}]",
            )
        )
    if repository_paths != _directory_files(root, "source", label="source entry"):
        raise IngressError("source directory has undeclared or missing artifacts")


def verify_input_tree(
    bundle: Path, *, candidate_sha: str, wheel_sha256: str, owner_uid: int
) -> None:
    root = bundle.parent
    try:
        for path in [root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())]:
            observed = path.lstat()
            if stat.S_ISLNK(observed.st_mode):
                raise IngressError("qualification input contains a symlink")
            if observed.st_uid != owner_uid or stat.S_IMODE(observed.st_mode) & 0o022:
                raise IngressError("qualification input ownership/mode is unsafe")
            if stat.S_ISDIR(observed.st_mode):
                continue
            if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
                raise IngressError("qualification input contains an unsafe object")
        _validate_bundle_inventory(
            bundle, candidate_sha=candidate_sha, wheel_sha256=wheel_sha256
        )
    except OSError as exc:
        raise IngressError(f"qualification input is incomplete: {exc}") from exc


def write_bootstrap_requirements(bundle: Path, requirements: Path) -> None:
    root = bundle.parent
    if os.path.lexists(requirements) or requirements.parent != root.parent:
        raise IngressError("bootstrap requirements path is unsafe")
    document = json.loads(bundle.read_text(encoding="utf-8"))
    rows = sorted(document["wheelhouse"], key=lambda item: item["path"])
    descriptor = os.open(requirements, _OPEN_NEW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        for row in rows:
            uri = (root / PurePosixPath(row["path"])).as_uri()
            stream.write(f"{uri} --hash=sha256:{row['sha256']}\n")


def _open_for_readers(root: Path) -> None:
    """``chmod -R u=rwX,go=rX`` that refuses symlinks instead of following them."""

    for path in [root, *root.rglob("*")]:
        observed = path.lstat()
        if stat.S_ISLNK(observed.st_mode):
            raise IngressError(f"unexpected symlink: {path}")
        executable = stat.S_ISDIR(observed.st_mode) or bool(
            stat.S_IMODE(observed.st_mode) & 0o111
        )
        os.chmod(path, 0o755 if executable else 0o644)


def tree_sha256(root: Path, *, owner_uid: int) -> str:
    """Deterministic digest of relative path, mode and content (runbook §1)."""

    digest = hashlib.sha256()
    paths = [root, *sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())]
    for path in paths:
        relative = "." if path == root else path.relative_to(root).as_posix()
        observed = path.lstat()
        if observed.st_uid != owner_uid or stat.S_IMODE(observed.st_mode) & 0o022:
            raise IngressError("unsafe candidate CLI ownership/mode")
        digest.update(relative.encode() + b"\0")
        digest.update(format(stat.S_IMODE(observed.st_mode), "04o").encode() + b"\0")
        if stat.S_ISDIR(observed.st_mode):
            digest.update(b"D\0")
        elif stat.S_ISREG(observed.st_mode) and observed.st_nlink == 1:
            digest.update(b"F\0" + hashlib.sha256(path.read_bytes()).digest())
        else:
            raise IngressError("unsafe candidate CLI tree object")
    return digest.hexdigest()


def build_sealed_venv(
    venv: Path,
    requirements: Path,
    *,
    owner_uid: int,
    run: Callable[..., object] = _run,
) -> Path:
    if os.path.lexists(venv):
        raise IngressError(f"sealed venv path already exists: {venv}")
    run(
        ("/usr/bin/python3", "-I", "-S", "-m", "venv", "--copies", str(venv)),
        check=True,
        env=dict(_VENV_ENV),
    )
    run(
        (
            str(venv / "bin" / "python"),
            "-I",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--only-binary=:all:",
            "--require-hashes",
            "--requirement",
            str(requirements),
        ),
        check=True,
        env={**_VENV_ENV, "PATH": f"{venv}/bin:/usr/bin:/bin"},
    )
    lib64 = venv / "lib64"
    if lib64.is_symlink():
        if os.readlink(lib64) != "lib":
            raise IngressError("sealed venv lib64 link does not point at lib")
        lib64.unlink()
    _open_for_readers(venv)
    cli = venv / "bin" / "cortex"
    if cli.is_symlink() or not cli.is_file() or not os.access(cli, os.X_OK):
        raise IngressError("sealed venv has no executable cortex CLI")
    tree_sha256(venv, owner_uid=owner_uid)
    return cli


@dataclass(frozen=True)
class SealedCandidate:
    metadata: ReleaseMetadata
    attempt_dir: Path
    input_root: Path
    bundle: Path
    install_config: Path
    venv: Path
    cli: Path
    tree_sha256: str
    owner_uid: int

    def assert_unchanged(self) -> None:
        if tree_sha256(self.venv, owner_uid=self.owner_uid) != self.tree_sha256:
            raise IngressError("sealed candidate CLI changed since ingress")


def ingest_release(
    version: str,
    *,
    fetcher: ReleaseFetcher,
    installer_root: Path,
    repository: str = OFFICIAL_REPOSITORY,
    owner_uid: int = 0,
    chain_stop: Path = Path("/"),
    run: Callable[..., object] = _run,
) -> SealedCandidate:
    """Runbook §1 end to end: nothing from the release runs before this returns."""

    metadata = resolve_release(fetcher, version, repository=repository)
    attempt = make_attempt_dir(
        installer_root, version, owner_uid=owner_uid, chain_stop=chain_stop
    )
    release_dir = attempt / "release"
    archive = download_asset(fetcher, metadata.install_input, release_dir)
    manifest = download_asset(fetcher, metadata.qualification, release_dir)
    for path in (archive, manifest):
        observed = path.lstat()
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or observed.st_uid != owner_uid
            or stat.S_IMODE(observed.st_mode) & 0o022
        ):
            raise IngressError(f"downloaded release asset is not private: {path.name}")
    authority = read_qualification_authority(manifest)
    if authority.candidate_sha != metadata.commit:
        raise IngressError("qualification manifest candidate does not match the tag target")
    if authority.wheel_filename != metadata.wheel.name:
        raise IngressError("qualification manifest wheel is not the release wheel asset")
    if authority.wheel_sha256 != metadata.wheel.sha256:
        raise IngressError("qualification manifest wheel digest is not the release wheel digest")
    check_archive_topology(archive)
    input_root = attempt / "input"
    os.mkdir(input_root, 0o700)
    extract_install_input(archive, input_root)
    bundle = input_root / "bundle.json"
    if _sha256_file(bundle) != authority.bundle_sha256:
        raise IngressError("bundle.json does not match the qualification manifest")
    verify_input_tree(
        bundle,
        candidate_sha=authority.candidate_sha,
        wheel_sha256=authority.wheel_sha256,
        owner_uid=owner_uid,
    )
    requirements = attempt / "bootstrap-requirements.txt"
    write_bootstrap_requirements(bundle, requirements)
    # The input is not a credential surface: once verified, the unprivileged
    # plan identity may traverse and read it (runbook §1).
    for directory in (installer_root, installer_root / version, attempt):
        os.chmod(directory, 0o755)
    _open_for_readers(input_root)
    venv = attempt / "venv"
    cli = build_sealed_venv(venv, requirements, owner_uid=owner_uid, run=run)
    return SealedCandidate(
        metadata=metadata,
        attempt_dir=attempt,
        input_root=input_root,
        bundle=bundle,
        install_config=input_root / "install-config.yaml",
        venv=venv,
        cli=cli,
        tree_sha256=tree_sha256(venv, owner_uid=owner_uid),
        owner_uid=owner_uid,
    )
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_trust_root_install_release_ingress.py tests/test_qualification_candidate_bundle.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add paulsha_cortex/trust_root/install/release_ingress.py tests/test_trust_root_install_release_ingress.py
git commit -m "feat(trust-root): release ingress 驗 manifest、archive 與 bundle 後封存 candidate venv（#1263）"
```

---
### Task 7: 協調器骨架與前置檢查（選項閘門、版本、prior 定位、lock／lease、在飛 job）

**Files:**
- Create: `paulsha_cortex/trust_root/install/upgrade.py`
- Create: `tests/upgrade_fixtures.py`
- Test: `tests/test_trust_root_upgrade_preflight.py`

**Interfaces:**
- Consumes: Task 3 `effective_receipt(state_root) -> InstallReceipt`；Task 4 `installed_runtime_env(deploy_root, state_root, *, owner_uid=0) -> dict[str, str]`、`loaded_runtime_mismatch(payload, expected) -> str`、`runtime_expected(receipt) -> dict`；Task 5／6 的 `parse_version`、`format_version`、`wheel_version`；`backend._run`；`install_cli._host_lock(*, leaf, conflict, shared=False)`、`install_cli._maintenance_lock_payload(lock_fd, *, allow_absent=False)`、`install_cli._maintenance_snapshot_path() -> Path`；`legacy.LocalLegacyHostBackend().in_flight(job_uids, plan) -> {"job_processes": int, "durable_jobs": int | None}`。
- Produces（Task 8–11 使用）：
  - `QUALIFICATION_ENV = "PSC_UPGRADE_QUALIFICATION"`；模組層 seam：`_STATE_ROOT`、`_OWNER_UID`、`_CHAIN_STOP`、`_STATUS_SETTLE_SECONDS`、`_sleep`、`_monotonic`、`_chown`、`_lookup_account`、`_run`
  - `class UpgradeError(InstallError)`
  - `@dataclass(frozen=True) UpgradeOptions(version: str | None = None, wait_idle: int = 0, json_output: bool = False, recover: bool = False, status: bool = False, release_source: Path | None = None, allow_same_version: bool = False, prior_receipt: Path | None = None)`
  - `options_from_args(args: argparse.Namespace, *, environ: Mapping[str, str]) -> UpgradeOptions`（Namespace 欄位：`version`、`wait_idle`、`json_output`、`recover`、`status`、`release_source`、`allow_same_version`、`prior_receipt`）
  - `_pin_import_paths() -> None`
  - `@dataclass(frozen=True) PriorReceipt(path: Path, receipt: InstallReceipt, version: tuple[int, int, int])`，property `document -> dict`、`plan -> Mapping`
  - `locate_prior(options: UpgradeOptions) -> PriorReceipt`、`check_target_version(target: str, current: tuple[int, int, int], *, allow_same_version: bool) -> tuple[int, int, int]`
  - `await_loaded_runtime(plan: Mapping, receipt_path: Path, expected: Mapping, *, settle_seconds: float | None = None) -> tuple[str, object]`
  - `assert_installer_idle() -> None`、`in_flight_counts(plan: Mapping) -> tuple[int, int | None]`、`wait_until_idle(plan: Mapping, *, wait_seconds: int) -> None`
  - `@dataclass(frozen=True) Preflight(prior: PriorReceipt, target: tuple[int, int, int])`、`preflight(options: UpgradeOptions) -> Preflight`
- Produces（`tests/upgrade_fixtures.py`）：`PRIOR_WHEEL`、`PRIOR_COMMIT`、`NEW_WHEEL`、`NEW_COMMIT`、`plan_document(tmp_path, *, version, wheel_sha256, commit, overlay_sha=None) -> dict`、`durable_prior(tmp_path, *, version="0.1.12", qualified=True) -> InstallReceipt`、`make_prior(tmp_path, *, version="0.1.12", overlay_sha=None) -> upgrade.PriorReceipt`

- [ ] **Step 1: 寫會失敗的測試**

建立 `tests/upgrade_fixtures.py`：

```python
"""Shared fakes for `cortex upgrade` tests: no root, no network, no systemd."""
from __future__ import annotations

from pathlib import Path

from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install import upgrade
from paulsha_cortex.trust_root.install.core import (
    InstallReceipt,
    new_install_receipt,
    plan_sha256,
)
from paulsha_cortex.trust_root.install.release_ingress import parse_version

PRIOR_WHEEL = "1" * 64
PRIOR_COMMIT = "b" * 40
NEW_WHEEL = "2" * 64
NEW_COMMIT = "a" * 40


def plan_document(
    tmp_path: Path,
    *,
    version: str,
    wheel_sha256: str,
    commit: str,
    overlay_sha: str | None = None,
) -> dict[str, object]:
    plan: dict[str, object] = {
        "schema_version": 1,
        "scheme": "four-way",
        "repo_identity": {
            "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
            "commit": commit,
        },
        "candidate": {
            "candidate_sha": commit,
            "wheel_sha256": wheel_sha256,
            "bundle_sha256": "c" * 64,
            "wheel": {
                "path": f"dist/paulsha_cortex-{version}-py3-none-any.whl",
                "sha256": wheel_sha256,
            },
        },
        "accounts": [
            {
                "name": "cortex-builder",
                "uid": 993,
                "gid": 993,
                "home": "/var/lib/cortex-builder",
                "shell": "/usr/sbin/nologin",
            }
        ],
        "roots": {
            "deploy": str(tmp_path / "opt/cortex"),
            "state": str(tmp_path / "var/lib/cortex"),
            "systemd": str(tmp_path / "etc/systemd/system"),
            "polkit": str(tmp_path / "etc/polkit-1/rules.d"),
        },
        "apply_order": [],
        "required_credentials": [],
    }
    if overlay_sha is not None:
        plan["host_overlay_sha256"] = overlay_sha
    plan["receipt_path"] = str(install_core.canonical_receipt_path(plan))
    return plan


def durable_prior(
    tmp_path: Path, *, version: str = "0.1.12", qualified: bool = True
) -> InstallReceipt:
    """A real on-disk receipt; callers patch `_validate_receipt_parent/_file`."""

    plan = plan_document(
        tmp_path, version=version, wheel_sha256=PRIOR_WHEEL, commit=PRIOR_COMMIT
    )
    path = (tmp_path / "var/lib/cortex-install-receipts" / "prior.json").absolute()
    receipt = new_install_receipt(plan, path=path)
    receipt._document.update(state="applied", qualified=qualified)
    receipt._persist()
    return receipt


def make_prior(
    tmp_path: Path, *, version: str = "0.1.12", overlay_sha: str | None = None
) -> upgrade.PriorReceipt:
    """An in-memory prior receipt for coordinator tests."""

    plan = plan_document(
        tmp_path,
        version=version,
        wheel_sha256=PRIOR_WHEEL,
        commit=PRIOR_COMMIT,
        overlay_sha=overlay_sha,
    )
    path = tmp_path / "var/lib/cortex-install-receipts" / "prior.json"
    document = {
        "receipt_id": "prior-receipt",
        "plan": plan,
        "plan_sha256": plan_sha256(plan),
        "state": "applied",
        "qualified": True,
        "credentials": [],
    }
    return upgrade.PriorReceipt(
        path=path,
        receipt=InstallReceipt(document, path=path),
        version=parse_version(version),
    )
```

建立 `tests/test_trust_root_upgrade_preflight.py`：

```python
"""#1263：`cortex upgrade` 的前置檢查不改動主機任何東西。"""
from __future__ import annotations

import ast
import sys
from argparse import Namespace
from pathlib import Path

import pytest

import upgrade_fixtures as fx
from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install import release_ingress, upgrade
from paulsha_cortex.trust_root.install.core import InstallError
from paulsha_cortex.trust_root.install.release_ingress import IngressError


def _args(**changes: object) -> Namespace:
    values: dict[str, object] = {
        "version": "0.1.13",
        "wait_idle": 0,
        "json_output": False,
        "recover": False,
        "status": False,
        "release_source": None,
        "allow_same_version": False,
        "prior_receipt": None,
    }
    values.update(changes)
    return Namespace(**values)


@pytest.mark.parametrize(
    "flag",
    [
        {"release_source": "/run/release"},
        {"allow_same_version": True},
        {"prior_receipt": "/run/prior.json"},
    ],
)
def test_test_only_flags_need_the_qualification_environment(flag) -> None:
    with pytest.raises(upgrade.UpgradeError, match="PSC_UPGRADE_QUALIFICATION=1"):
        upgrade.options_from_args(_args(**flag), environ={})

    options = upgrade.options_from_args(
        _args(**flag), environ={"PSC_UPGRADE_QUALIFICATION": "1"}
    )
    assert options.version == "0.1.13"


def test_qualification_paths_must_be_absolute() -> None:
    with pytest.raises(upgrade.UpgradeError, match="absolute"):
        upgrade.options_from_args(
            _args(release_source="relative/dir"),
            environ={"PSC_UPGRADE_QUALIFICATION": "1"},
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"version": None}, "target version is required"),
        ({"recover": True}, "take no version"),
        ({"version": None, "status": True, "wait_idle": 5}, "take no version"),
        ({"wait_idle": -1}, "zero or more"),
    ],
)
def test_option_combinations_are_validated(changes, message) -> None:
    with pytest.raises(upgrade.UpgradeError, match=message):
        upgrade.options_from_args(_args(**changes), environ={})


def test_malformed_version_is_refused_before_any_work() -> None:
    with pytest.raises(IngressError, match="MAJOR.MINOR.PATCH"):
        upgrade.options_from_args(_args(version="0.1.13-rc1"), environ={})


def test_status_takes_no_version() -> None:
    options = upgrade.options_from_args(_args(version=None, status=True), environ={})

    assert options.status is True
    assert options.version is None


@pytest.mark.parametrize(
    ("target", "allow", "ok"),
    [
        ("0.1.13", False, True),
        ("0.1.12", False, False),
        ("0.1.11", False, False),
        ("0.1.12", True, True),
        ("0.1.11", True, False),
    ],
)
def test_upgrade_only_moves_forward(target: str, allow: bool, ok: bool) -> None:
    if ok:
        assert upgrade.check_target_version(
            target, (0, 1, 12), allow_same_version=allow
        ) == release_ingress.parse_version(target)
    else:
        with pytest.raises(upgrade.UpgradeError, match="only moves forward"):
            upgrade.check_target_version(target, (0, 1, 12), allow_same_version=allow)


@pytest.fixture
def rootless(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _o, _p: None)
    monkeypatch.setattr(install_core, "_validate_receipt_file", lambda _o, _p: None)
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "installer")
    monkeypatch.setattr(upgrade, "_STATE_ROOT", (tmp_path / "var/lib/cortex").absolute())
    monkeypatch.setattr(upgrade, "_STATUS_SETTLE_SECONDS", 0)
    return tmp_path


def test_production_locates_the_prior_through_the_receipt_chain(rootless: Path) -> None:
    prior = fx.durable_prior(rootless)

    located = upgrade.locate_prior(upgrade.UpgradeOptions(version="0.1.13"))

    assert located.path == prior.path
    assert located.version == (0, 1, 12)


def test_qualification_prior_receipt_is_loaded_explicitly(rootless: Path) -> None:
    prior = fx.durable_prior(rootless)

    located = upgrade.locate_prior(
        upgrade.UpgradeOptions(version="0.1.13", prior_receipt=prior.path)
    )

    assert located.document["receipt_id"] == prior.to_dict()["receipt_id"]


def test_an_unqualified_prior_is_refused(rootless: Path) -> None:
    prior = fx.durable_prior(rootless, qualified=False)

    with pytest.raises(upgrade.UpgradeError, match="not applied and qualified"):
        upgrade.locate_prior(
            upgrade.UpgradeOptions(version="0.1.13", prior_receipt=prior.path)
        )


def test_preflight_refuses_when_running_services_do_not_match_the_prior(
    rootless: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx.durable_prior(rootless)
    monkeypatch.setattr(
        upgrade,
        "await_loaded_runtime",
        lambda plan, path, expected, **_kwargs: ("manager_receipt=mismatch", None),
    )
    monkeypatch.setattr(
        upgrade,
        "in_flight_counts",
        lambda _plan: pytest.fail("must stop before the idle check"),
    )

    with pytest.raises(upgrade.UpgradeError, match="manager_receipt=mismatch"):
        upgrade.preflight(upgrade.UpgradeOptions(version="0.1.13"))


def test_preflight_passes_on_an_idle_matching_host(
    rootless: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prior = fx.durable_prior(rootless)
    seen: list[tuple[Path, dict[str, object]]] = []

    def matched(plan, path, expected, **_kwargs):
        seen.append((path, dict(expected)))
        return "", {}

    monkeypatch.setattr(upgrade, "await_loaded_runtime", matched)
    monkeypatch.setattr(upgrade, "in_flight_counts", lambda _plan: (0, 0))

    checked = upgrade.preflight(upgrade.UpgradeOptions(version="0.1.13"))

    assert checked.target == (0, 1, 13)
    assert seen[0][0] == prior.path
    assert seen[0][1]["receipt_id"] == prior.to_dict()["receipt_id"]


def test_installer_idle_check_refuses_lease_marker_and_snapshot(rootless: Path) -> None:
    plan = fx.plan_document(
        rootless, version="0.1.12", wheel_sha256=fx.PRIOR_WHEEL, commit=fx.PRIOR_COMMIT
    )
    upgrade.assert_installer_idle()

    with install_cli._maintenance_lease(plan):
        with pytest.raises(InstallError, match="maintenance window is active"):
            upgrade.assert_installer_idle()
    with install_cli._host_lock(leaf="maintenance.lock", conflict="unexpected") as lock_fd:
        install_cli._write_lock_payload(
            lock_fd, {"plan_sha256": "a" * 64, "token_sha256": "b" * 64}
        )
    with pytest.raises(upgrade.UpgradeError, match="stale trust-root maintenance marker"):
        upgrade.assert_installer_idle()
    with install_cli._host_lock(leaf="maintenance.lock", conflict="unexpected") as lock_fd:
        install_cli._write_lock_payload(lock_fd, None)
    install_cli._write_maintenance_snapshot(
        {
            "schema_version": 1,
            "plan_sha256": "a" * 64,
            "receipt_path": str(rootless / "receipt.json"),
            "present_services": [],
            "previously_active": [],
        }
    )
    with pytest.raises(upgrade.UpgradeError, match="unfinished maintenance snapshot"):
        upgrade.assert_installer_idle()


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_wait_idle_polls_until_jobs_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(upgrade, "_monotonic", clock.monotonic)
    monkeypatch.setattr(upgrade, "_sleep", clock.sleep)
    observations = iter([(1, 0), (0, 1), (0, 0)])
    monkeypatch.setattr(upgrade, "in_flight_counts", lambda _plan: next(observations))

    upgrade.wait_until_idle({}, wait_seconds=60)

    assert clock.sleeps == [5.0, 5.0]


def test_wait_idle_zero_refuses_at_once_with_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade, "in_flight_counts", lambda _plan: (2, 1))

    with pytest.raises(
        upgrade.UpgradeError, match="job processes=2, durable in-flight jobs=1"
    ):
        upgrade.wait_until_idle({}, wait_seconds=0)


def test_an_unreadable_durable_registry_counts_as_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(upgrade, "_monotonic", clock.monotonic)
    monkeypatch.setattr(upgrade, "_sleep", clock.sleep)
    monkeypatch.setattr(upgrade, "in_flight_counts", lambda _plan: (0, None))

    with pytest.raises(upgrade.UpgradeError, match="durable in-flight jobs=unknown"):
        upgrade.wait_until_idle({}, wait_seconds=10)
    assert sum(clock.sleeps) == 10.0


def test_in_flight_counts_reads_job_accounts_from_the_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class Host:
        def in_flight(self, job_uids, plan):
            seen["job_uids"] = dict(job_uids)
            return {"job_processes": 0, "durable_jobs": None}

    monkeypatch.setattr(upgrade, "LocalLegacyHostBackend", lambda: Host())
    plan = {
        "accounts": [
            {"name": "cortex-builder", "uid": 993},
            {"name": "cortex-manager", "uid": 991},
            {"name": "cortex-gate", "uid": 994},
        ]
    }

    assert upgrade.in_flight_counts(plan) == (0, None)
    assert seen["job_uids"] == {"cortex-builder": 993, "cortex-gate": 994}


def test_import_paths_are_pinned_to_the_resolved_venv_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path.resolve()
    slot = base / "venvs" / "slot-a"
    (slot / "lib").mkdir(parents=True)
    link = base / "venv"
    link.symlink_to(slot)
    monkeypatch.setattr(sys, "prefix", str(link))
    monkeypatch.setattr(sys, "path", [f"{link}/lib/python3/site-packages", "/usr/lib/python3"])

    upgrade._pin_import_paths()

    assert sys.path == [f"{slot}/lib/python3/site-packages", "/usr/lib/python3"]


def test_import_paths_are_left_alone_without_a_venv_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path.resolve() / "venv"
    real.mkdir()
    monkeypatch.setattr(sys, "prefix", str(real))
    monkeypatch.setattr(sys, "path", [f"{real}/lib/site-packages"])

    upgrade._pin_import_paths()

    assert sys.path == [f"{real}/lib/site-packages"]


def test_upgrade_modules_import_everything_at_module_level() -> None:
    for module in (upgrade, release_ingress):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        nested = [
            node.lineno
            for function in ast.walk(tree)
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
            for node in ast.walk(function)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        assert nested == [], f"{module.__name__} imports inside functions at {nested}"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_trust_root_upgrade_preflight.py -q`
Expected: collection error，`ImportError: cannot import name 'upgrade' from 'paulsha_cortex.trust_root.install'`（`upgrade_fixtures` 先 import 它）。

- [ ] **Step 3: 最小實作**

建立 `paulsha_cortex/trust_root/install/upgrade.py`。下方 import 區塊就是模組最終的完整清單，Task 8–10 只在檔尾續寫函式，不再改 import：

```python
"""`cortex upgrade <version>`: one root command from release ingress to a verified receipt (#1263).

The installed CLI coordinates: it verifies the release itself, decides the
current receipt from the receipt chain, plans as an unprivileged account,
binds the plan sha, and holds the maintenance lease in-process.  Every receipt
mutation -- apply, credential inheritance, activate, verify, rollback -- runs
through the sealed candidate CLI with the lease token, exactly as
trust-root-transactional-install.md does by hand.

`/opt/cortex/venv` is a symlink that apply switches to the candidate slot.  This
module therefore imports everything at module level, and `_pin_import_paths()`
resolves the running slot before anything else happens.
"""
from __future__ import annotations

import fcntl
import hashlib
import importlib
import json
import os
import pwd
import re
import secrets
import signal
import stat
import sys
import time
from argparse import Namespace
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess
from typing import Iterator, Mapping

from . import cli as install_cli
from .backend import _run
from .core import (
    InstallError,
    InstallReceipt,
    _rename_noreplace_at,
    _write_all,
    atomic_write_json,
    plan_sha256,
    validate_prior_receipt_handoff,
)
from .legacy import LocalLegacyHostBackend, host_overlay_record, validate_host_overlay
from .loaded_runtime import installed_runtime_env, loaded_runtime_mismatch, runtime_expected
from .receipt_chain import effective_receipt
from .release_ingress import (
    OFFICIAL_REPOSITORY,
    DirectoryReleaseFetcher,
    GitHubReleaseFetcher,
    ReleaseFetcher,
    SealedCandidate,
    format_version,
    ingest_release,
    parse_version,
    wheel_version,
)

QUALIFICATION_ENV = "PSC_UPGRADE_QUALIFICATION"
_STATE_ROOT = Path("/var/lib/cortex")
_OWNER_UID = 0
_CHAIN_STOP = Path("/")
_IDLE_POLL_SECONDS = 5.0
_STATUS_SETTLE_SECONDS = 60.0
_STATUS_POLL_SECONDS = 2.0
_JOB_ACCOUNTS = ("cortex-builder", "cortex-reviewer-planner", "cortex-gate")
_sleep = time.sleep
_monotonic = time.monotonic
_chown = os.chown
_lookup_account = pwd.getpwnam


class UpgradeError(InstallError):
    """`cortex upgrade` refused or stopped."""


@dataclass(frozen=True)
class UpgradeOptions:
    version: str | None = None
    wait_idle: int = 0
    json_output: bool = False
    recover: bool = False
    status: bool = False
    release_source: Path | None = None
    allow_same_version: bool = False
    prior_receipt: Path | None = None


def _absolute(value: object, *, flag: str) -> Path | None:
    if value is None:
        return None
    path = Path(str(value))
    if not path.is_absolute() or ".." in path.parts:
        raise UpgradeError(f"{flag} must be an absolute path without '..'")
    return path


def options_from_args(args: Namespace, *, environ: Mapping[str, str]) -> UpgradeOptions:
    """Parsed arguments to options; test-only flags need PSC_UPGRADE_QUALIFICATION=1."""

    test_only = [
        flag
        for flag, value in (
            ("--release-source", args.release_source),
            ("--allow-same-version", args.allow_same_version),
            ("--prior-receipt", args.prior_receipt),
        )
        if value
    ]
    if test_only and environ.get(QUALIFICATION_ENV) != "1":
        raise UpgradeError(
            f"{', '.join(test_only)} is accepted only for RC qualification "
            f"({QUALIFICATION_ENV}=1)"
        )
    if args.wait_idle < 0:
        raise UpgradeError("--wait-idle must be zero or more seconds")
    if args.recover or args.status:
        if args.version is not None or test_only or args.wait_idle:
            raise UpgradeError("--recover and --status take no version or upgrade options")
    elif args.version is None:
        raise UpgradeError("a target version is required, e.g. `cortex upgrade 0.1.13`")
    else:
        parse_version(args.version)
    return UpgradeOptions(
        version=args.version,
        wait_idle=args.wait_idle,
        json_output=bool(args.json_output),
        recover=bool(args.recover),
        status=bool(args.status),
        release_source=_absolute(args.release_source, flag="--release-source"),
        allow_same_version=bool(args.allow_same_version),
        prior_receipt=_absolute(args.prior_receipt, flag="--prior-receipt"),
    )


def _pin_import_paths() -> None:
    """Resolve the running venv slot so apply's symlink switch cannot swap our code."""

    prefix = Path(sys.prefix)
    resolved = prefix.resolve()
    if resolved == prefix:
        return
    old = str(prefix)
    pinned: list[str] = []
    for entry in sys.path:
        if entry == old:
            pinned.append(str(resolved))
        elif entry.startswith(old + os.sep):
            pinned.append(str(resolved) + entry[len(old):])
        else:
            pinned.append(entry)
    sys.path[:] = pinned
    importlib.invalidate_caches()


@dataclass(frozen=True)
class PriorReceipt:
    path: Path
    receipt: InstallReceipt
    version: tuple[int, int, int]

    @property
    def document(self) -> dict[str, object]:
        return self.receipt.to_dict()

    @property
    def plan(self) -> Mapping[str, object]:
        plan = self.document.get("plan")
        if not isinstance(plan, Mapping):
            raise UpgradeError("the current receipt has no embedded plan")
        return plan


def locate_prior(options: UpgradeOptions) -> PriorReceipt:
    """The current receipt: the receipt chain in production, explicit only for RC."""

    if options.prior_receipt is not None:
        receipt = InstallReceipt.load(options.prior_receipt)
    else:
        receipt = effective_receipt(_STATE_ROOT)
    document = receipt.to_dict()
    if document.get("state") != "applied" or document.get("qualified") is not True:
        raise UpgradeError(
            "the current receipt is not applied and qualified; finish or recover it first"
        )
    if receipt.path is None:
        raise UpgradeError("the current receipt has no durable path")
    plan = document.get("plan")
    candidate = plan.get("candidate") if isinstance(plan, Mapping) else None
    wheel = candidate.get("wheel") if isinstance(candidate, Mapping) else None
    version = wheel_version(wheel.get("path") if isinstance(wheel, Mapping) else None)
    return PriorReceipt(path=receipt.path, receipt=receipt, version=version)


def check_target_version(
    target: str, current: tuple[int, int, int], *, allow_same_version: bool
) -> tuple[int, int, int]:
    wanted = parse_version(target)
    if wanted > current or (allow_same_version and wanted == current):
        return wanted
    raise UpgradeError(
        f"cortex upgrade only moves forward: current {format_version(current)}, "
        f"requested {target}; use the installer rollback or the runbook to go back"
    )


def _service_status(plan: Mapping[str, object], receipt_path: Path) -> object:
    """`cortex service status --system --json` of the installed CLI, or None."""

    roots = plan.get("roots")
    if not isinstance(roots, Mapping):
        return None
    deploy = Path(str(roots.get("deploy")))
    try:
        env = installed_runtime_env(
            deploy, Path(str(roots.get("state"))), owner_uid=_OWNER_UID
        )
        result = _run(
            (
                str(deploy / "venv" / "bin" / "cortex"),
                "service",
                "status",
                "--system",
                "--json",
                "--install-receipt",
                str(receipt_path),
            ),
            env=env,
        )
    except (InstallError, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def await_loaded_runtime(
    plan: Mapping[str, object],
    receipt_path: Path,
    expected: Mapping[str, object],
    *,
    settle_seconds: float | None = None,
) -> tuple[str, object]:
    """Poll until Manager/Monitor load ``expected`` or the settle window closes."""

    window = _STATUS_SETTLE_SECONDS if settle_seconds is None else settle_seconds
    deadline = _monotonic() + window
    while True:
        payload = _service_status(plan, receipt_path)
        mismatch = (
            "service_status=unavailable"
            if payload is None
            else loaded_runtime_mismatch(payload, expected)
        )
        if not mismatch or _monotonic() >= deadline:
            return mismatch, payload
        _sleep(_STATUS_POLL_SECONDS)


def assert_installer_idle() -> None:
    """No maintenance window, stale marker, snapshot or transaction is in flight."""

    with install_cli._host_lock(
        leaf="maintenance.lock",
        conflict=(
            "a trust-root maintenance window is active; wait for it or run "
            "`cortex upgrade --recover`"
        ),
        shared=True,
    ) as lock_fd:
        if install_cli._maintenance_lock_payload(lock_fd, allow_absent=True) is not None:
            raise UpgradeError(
                "a stale trust-root maintenance marker exists; run `cortex upgrade --recover`"
            )
        if os.path.lexists(install_cli._maintenance_snapshot_path()):
            raise UpgradeError(
                "an unfinished maintenance snapshot exists; run `cortex upgrade --recover`"
            )
        with install_cli._host_lock(
            leaf="transaction.lock",
            conflict="another trust-root transaction is running",
            shared=True,
        ):
            return


def in_flight_counts(plan: Mapping[str, object]) -> tuple[int, int | None]:
    """Job-account processes and durable in-flight jobs (legacy adoption's gate)."""

    accounts = plan.get("accounts")
    job_uids = {
        str(row["name"]): int(row["uid"])
        for row in (accounts if isinstance(accounts, list) else [])
        if isinstance(row, Mapping)
        and row.get("name") in _JOB_ACCOUNTS
        and isinstance(row.get("uid"), int)
    }
    observed = LocalLegacyHostBackend().in_flight(job_uids, plan)
    processes = observed.get("job_processes")
    durable = observed.get("durable_jobs")
    return (
        processes if isinstance(processes, int) else 1,
        durable if isinstance(durable, int) else None,
    )


def wait_until_idle(plan: Mapping[str, object], *, wait_seconds: int) -> None:
    """Refuse in-flight jobs; an unreadable durable registry counts as busy."""

    deadline = _monotonic() + wait_seconds
    while True:
        processes, durable = in_flight_counts(plan)
        if processes == 0 and durable == 0:
            return
        remaining = deadline - _monotonic()
        if remaining <= 0:
            raise UpgradeError(
                f"in-flight jobs block the upgrade: job processes={processes}, "
                f"durable in-flight jobs={'unknown' if durable is None else durable}; "
                "retry when idle or pass --wait-idle <seconds>"
            )
        _sleep(min(_IDLE_POLL_SECONDS, remaining))


@dataclass(frozen=True)
class Preflight:
    prior: PriorReceipt
    target: tuple[int, int, int]


def preflight(options: UpgradeOptions) -> Preflight:
    """Spec §4 step 1: read-only checks; nothing on the host changes."""

    if options.version is None:
        raise UpgradeError("a target version is required")
    prior = locate_prior(options)
    target = check_target_version(
        options.version, prior.version, allow_same_version=options.allow_same_version
    )
    mismatch, _payload = await_loaded_runtime(
        prior.plan, prior.path, runtime_expected(prior.document)
    )
    if mismatch:
        raise UpgradeError(
            f"the running services do not match the current receipt ({mismatch}); "
            "the effective receipt cannot be confirmed"
        )
    assert_installer_idle()
    wait_until_idle(prior.plan, wait_seconds=options.wait_idle)
    return Preflight(prior=prior, target=target)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_trust_root_upgrade_preflight.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add paulsha_cortex/trust_root/install/upgrade.py tests/upgrade_fixtures.py tests/test_trust_root_upgrade_preflight.py
git commit -m "feat(trust-root): cortex upgrade 前置檢查與只限測試參數的閘門（#1263）"
```

---

### Task 8: plan 階段：非 root 產生 plan、綁定 sha、發布 durable plan

**Files:**
- Modify: `paulsha_cortex/trust_root/install/upgrade.py`（檔尾續寫）
- Modify: `tests/upgrade_fixtures.py`（檔尾續寫）
- Test: `tests/test_trust_root_upgrade_plan.py`

**Interfaces:**
- Consumes: Task 6 `SealedCandidate`（欄位 `metadata`、`attempt_dir`、`bundle`、`install_config`、`venv`、`cli`、`assert_unchanged()`）；Task 7 `PriorReceipt`、`UpgradeOptions`、`UpgradeError`、`_run`、`_chown`、`_lookup_account`、`_OWNER_UID`；`install_cli._load_mapping(path, *, label)`、`install_cli._TRUST_ROOT_MAINTENANCE_ROOT`；`legacy.validate_host_overlay`、`legacy.host_overlay_record(overlay) -> {"sha256", "keys"} | None`；`core.validate_prior_receipt_handoff(plan, prior)`、`core._write_all(fd, payload)`、`core._rename_noreplace_at(parent_fd, source, destination)`；candidate CLI `install trust-root plan --config C --bundle B [--host-overlay O] --output P` 的 stdout `{"output", "plan_sha256", ...}`。
- Produces（Task 9、10 使用）：
  - `@dataclass(frozen=True) BoundPlan(plan: dict[str, object], sha256: str, durable_path: Path, receipt_path: Path, overlay_sha256: str | None)`
  - `read_host_overlay(installer_root: Path) -> dict[str, object] | None`、`plan_identity(overlay: Mapping[str, object] | None) -> tuple[int, int]`
  - `publish_durable_plan(payload: bytes, expected_sha256: str, *, plans_root: Path) -> Path`
  - `produce_plan(sealed: SealedCandidate, prior: PriorReceipt, *, options: UpgradeOptions) -> BoundPlan`
  - `_read_regular_bytes(path: Path, *, limit: int = 64 MiB) -> bytes`
- Produces（`tests/upgrade_fixtures.py`）：`make_sealed(tmp_path, *, version="0.1.13", commit=NEW_COMMIT, wheel_sha256=NEW_WHEEL) -> SealedCandidate`、`FakePlanCli(plan, *, reported_sha=None)`、`account(uid, gid) -> SimpleNamespace`

- [ ] **Step 1: 寫會失敗的測試**

在 `tests/upgrade_fixtures.py` 的 import 區塊加入：

```python
import hashlib
import json
import os
import subprocess
from types import SimpleNamespace

from paulsha_cortex.trust_root.install.release_ingress import (
    ReleaseAsset,
    ReleaseMetadata,
    SealedCandidate,
    asset_names,
    tree_sha256,
)
```

並在檔尾加入：

```python
def make_sealed(
    tmp_path: Path,
    *,
    version: str = "0.1.13",
    commit: str = NEW_COMMIT,
    wheel_sha256: str = NEW_WHEEL,
) -> SealedCandidate:
    attempt = tmp_path / "installer" / version / "attempt-test"
    venv = attempt / "venv"
    (venv / "bin").mkdir(parents=True)
    cli = venv / "bin" / "cortex"
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    cli.chmod(0o755)
    input_root = attempt / "input"
    input_root.mkdir()
    (input_root / "bundle.json").write_text("{}\n", encoding="utf-8")
    (input_root / "install-config.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    wheel_name, input_name, qualification_name = asset_names(version)

    def asset(name: str, digest: str = "0" * 64) -> ReleaseAsset:
        return ReleaseAsset(
            name,
            digest,
            1,
            f"https://github.com/hamanpaul/paulsha-cortex/releases/download/v{version}/{name}",
        )

    metadata = ReleaseMetadata(
        version=version,
        tag=f"v{version}",
        commit=commit,
        wheel=asset(wheel_name, wheel_sha256),
        install_input=asset(input_name),
        qualification=asset(qualification_name),
    )
    return SealedCandidate(
        metadata=metadata,
        attempt_dir=attempt,
        input_root=input_root,
        bundle=input_root / "bundle.json",
        install_config=input_root / "install-config.yaml",
        venv=venv,
        cli=cli,
        tree_sha256=tree_sha256(venv, owner_uid=os.getuid()),
        owner_uid=os.getuid(),
    )


class FakePlanCli:
    """Stands in for `<sealed>/bin/cortex install trust-root plan ...` run unprivileged."""

    def __init__(self, plan: dict[str, object], *, reported_sha: str | None = None) -> None:
        self.plan = plan
        self.reported_sha = reported_sha
        self.calls: list[dict[str, object]] = []

    def __call__(self, argv, *, check=False, env=None, uid=None, gid=None, **_kwargs):
        argv = tuple(argv)
        self.calls.append({"argv": argv, "env": dict(env or {}), "uid": uid, "gid": gid})
        output = Path(argv[argv.index("--output") + 1])
        payload = install_core.canonical_plan_bytes(self.plan)
        output.write_bytes(payload)
        sha = self.reported_sha or hashlib.sha256(payload).hexdigest()
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"output": str(output), "plan_sha256": sha}), ""
        )


def account(uid: int, gid: int) -> SimpleNamespace:
    return SimpleNamespace(pw_uid=uid, pw_gid=gid)
```

建立 `tests/test_trust_root_upgrade_plan.py`：

```python
"""#1263：plan 由封存的 candidate CLI 以非 root 產生，工具自己綁定 plan sha。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

import pytest

import upgrade_fixtures as fx
from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install import upgrade
from paulsha_cortex.trust_root.install.core import plan_sha256
from paulsha_cortex.trust_root.install.legacy import host_overlay_record

OVERLAY = {"operator_account": "cortex-ops", "providers": {"builder": ["codex"]}}


@pytest.fixture
def planning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, object]]:
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "installer")
    monkeypatch.setattr(upgrade, "_OWNER_UID", os.getuid())
    monkeypatch.setattr(upgrade, "_chown", lambda *_args: None)
    handoffs: list[tuple[object, object]] = []
    monkeypatch.setattr(
        upgrade,
        "validate_prior_receipt_handoff",
        lambda plan, prior: handoffs.append((plan, prior)),
    )
    return handoffs


def _overlay(tmp_path: Path, document: dict[str, object]) -> Path:
    path = tmp_path / "installer" / "host-overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o644)
    return path


def _new_plan(tmp_path: Path, *, overlay_sha: str | None = None) -> dict[str, object]:
    return fx.plan_document(
        tmp_path,
        version="0.1.13",
        wheel_sha256=fx.NEW_WHEEL,
        commit=fx.NEW_COMMIT,
        overlay_sha=overlay_sha,
    )


def test_plan_runs_the_sealed_cli_unprivileged_and_binds_its_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planning
) -> None:
    _overlay(tmp_path, {**OVERLAY, "legacy_adoption": {"inventory_sha256": "d" * 64}})
    overlay_sha = host_overlay_record(OVERLAY)["sha256"]
    sealed = fx.make_sealed(tmp_path)
    prior = fx.make_prior(tmp_path, overlay_sha=overlay_sha)
    plan = _new_plan(tmp_path, overlay_sha=overlay_sha)
    cli = fx.FakePlanCli(plan)
    monkeypatch.setattr(upgrade, "_run", cli)
    monkeypatch.setattr(
        upgrade, "_lookup_account", lambda name: {"cortex-ops": fx.account(4242, 4243)}[name]
    )

    bound = upgrade.produce_plan(sealed, prior, options=upgrade.UpgradeOptions(version="0.1.13"))

    call = cli.calls[0]
    assert (call["uid"], call["gid"]) == (4242, 4243)
    assert call["argv"][:4] == (str(sealed.cli), "install", "trust-root", "plan")
    assert set(call["env"]) == {"HOME", "PATH", "LANG", "LC_ALL", "PYTHONNOUSERSITE"}
    assert call["env"]["HOME"] == str(sealed.attempt_dir / "plan" / "home")
    overlay_arg = Path(call["argv"][call["argv"].index("--host-overlay") + 1])
    assert "legacy_adoption" not in json.loads(overlay_arg.read_text())
    assert bound.sha256 == plan_sha256(plan)
    assert bound.overlay_sha256 == overlay_sha
    assert bound.durable_path == tmp_path / "installer" / "plans" / f"{bound.sha256}.json"
    assert bound.durable_path.read_bytes() == install_core.canonical_plan_bytes(plan)
    assert stat.S_IMODE(bound.durable_path.stat().st_mode) == 0o600
    canonical = Path(str(plan["receipt_path"]))
    assert bound.receipt_path.parent == canonical.parent
    assert re.fullmatch(rf"{canonical.stem}\.run-[0-9a-f]{{32}}\.json", bound.receipt_path.name)
    assert planning == [(bound.plan, prior.receipt)]


def test_plan_refuses_a_reported_sha_that_is_not_the_file_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planning
) -> None:
    sealed = fx.make_sealed(tmp_path)
    monkeypatch.setattr(upgrade, "_run", fx.FakePlanCli(_new_plan(tmp_path), reported_sha="0" * 64))
    monkeypatch.setattr(upgrade, "_lookup_account", lambda name: fx.account(65534, 65534))

    with pytest.raises(upgrade.UpgradeError, match="does not match the plan file"):
        upgrade.produce_plan(
            sealed, fx.make_prior(tmp_path), options=upgrade.UpgradeOptions(version="0.1.13")
        )
    assert not (tmp_path / "installer" / "plans").exists()


def test_a_changed_host_overlay_is_refused_outside_qualification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planning
) -> None:
    _overlay(tmp_path, OVERLAY)
    overlay_sha = host_overlay_record(OVERLAY)["sha256"]
    sealed = fx.make_sealed(tmp_path)
    prior = fx.make_prior(tmp_path)
    cli = fx.FakePlanCli(_new_plan(tmp_path, overlay_sha=overlay_sha))
    monkeypatch.setattr(upgrade, "_run", cli)
    monkeypatch.setattr(upgrade, "_lookup_account", lambda name: fx.account(4242, 4243))

    with pytest.raises(upgrade.UpgradeError, match="host overlay differs"):
        upgrade.produce_plan(sealed, prior, options=upgrade.UpgradeOptions(version="0.1.13"))
    assert cli.calls == []

    bound = upgrade.produce_plan(
        sealed,
        prior,
        options=upgrade.UpgradeOptions(version="0.1.13", allow_same_version=True),
    )
    assert bound.overlay_sha256 == overlay_sha


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("candidate_sha", "f" * 40, "not the release tag target"),
        ("wheel_sha256", "9" * 64, "not the release wheel"),
    ],
)
def test_plan_must_name_the_release_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planning, field, value, message
) -> None:
    plan = _new_plan(tmp_path)
    if field == "candidate_sha":
        plan["candidate"]["candidate_sha"] = value  # type: ignore[index]
    else:
        plan["candidate"]["wheel"]["sha256"] = value  # type: ignore[index]
    plan["receipt_path"] = str(install_core.canonical_receipt_path(plan))
    monkeypatch.setattr(upgrade, "_run", fx.FakePlanCli(plan))
    monkeypatch.setattr(upgrade, "_lookup_account", lambda name: fx.account(65534, 65534))

    with pytest.raises(upgrade.UpgradeError, match=message):
        upgrade.produce_plan(
            fx.make_sealed(tmp_path),
            fx.make_prior(tmp_path),
            options=upgrade.UpgradeOptions(version="0.1.13"),
        )
    assert not (tmp_path / "installer" / "plans").exists()


@pytest.mark.parametrize("overlay", [None, {"operator_account": "root"}])
def test_plan_never_runs_as_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planning, overlay
) -> None:
    if overlay is not None:
        _overlay(tmp_path, overlay)
    overlay_sha = host_overlay_record(overlay)["sha256"] if overlay is not None else None
    accounts = {"root": fx.account(0, 0), "nobody": fx.account(65534, 65534)}
    monkeypatch.setattr(upgrade, "_lookup_account", lambda name: accounts[name])
    cli = fx.FakePlanCli(_new_plan(tmp_path, overlay_sha=overlay_sha))
    monkeypatch.setattr(upgrade, "_run", cli)

    upgrade.produce_plan(
        fx.make_sealed(tmp_path),
        fx.make_prior(tmp_path, overlay_sha=overlay_sha),
        options=upgrade.UpgradeOptions(version="0.1.13"),
    )

    assert (cli.calls[0]["uid"], cli.calls[0]["gid"]) == (65534, 65534)


def test_a_named_operator_account_missing_on_the_host_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planning
) -> None:
    _overlay(tmp_path, OVERLAY)

    def missing(name: str):
        raise KeyError(name)

    monkeypatch.setattr(upgrade, "_lookup_account", missing)

    with pytest.raises(upgrade.UpgradeError, match="operator_account does not exist"):
        upgrade.produce_plan(
            fx.make_sealed(tmp_path),
            fx.make_prior(tmp_path, overlay_sha=host_overlay_record(OVERLAY)["sha256"]),
            options=upgrade.UpgradeOptions(version="0.1.13"),
        )


def test_host_overlay_must_be_a_private_regular_file(tmp_path: Path, planning) -> None:
    path = _overlay(tmp_path, OVERLAY)
    path.chmod(0o666)
    with pytest.raises(upgrade.UpgradeError, match="without group/other write"):
        upgrade.read_host_overlay(tmp_path / "installer")

    path.unlink()
    real = tmp_path / "elsewhere.yaml"
    real.write_text("{}", encoding="utf-8")
    path.symlink_to(real)
    with pytest.raises(upgrade.UpgradeError, match="regular file"):
        upgrade.read_host_overlay(tmp_path / "installer")


def test_missing_host_overlay_means_no_overlay(tmp_path: Path, planning) -> None:
    assert upgrade.read_host_overlay(tmp_path / "installer") is None


def test_durable_plan_publication_reuses_identical_bytes_and_never_overwrites(
    tmp_path: Path, planning
) -> None:
    plans = tmp_path / "installer" / "plans"
    (tmp_path / "installer").mkdir()
    payload = b'{"plan": 1}\n'
    sha = hashlib.sha256(payload).hexdigest()

    first = upgrade.publish_durable_plan(payload, sha, plans_root=plans)
    assert upgrade.publish_durable_plan(payload, sha, plans_root=plans) == first

    first.write_bytes(b'{"plan": 2}\n')
    with pytest.raises(upgrade.UpgradeError, match="does not match reviewed bytes"):
        upgrade.publish_durable_plan(payload, sha, plans_root=plans)
    with pytest.raises(upgrade.UpgradeError, match="changed before durable publication"):
        upgrade.publish_durable_plan(b"other", sha, plans_root=plans)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_trust_root_upgrade_plan.py -q`
Expected: 多數測試 FAIL，`AttributeError: module 'paulsha_cortex.trust_root.install.upgrade' has no attribute 'produce_plan'`（或 `read_host_overlay`、`publish_durable_plan`）。

- [ ] **Step 3: 最小實作**

在 `paulsha_cortex/trust_root/install/upgrade.py` 檔尾續寫：

```python
# --- plan (spec §4 step 3) -----------------------------------------------------

_HOST_OVERLAY_NAME = "host-overlay.yaml"
_PLAN_ENV = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONNOUSERSITE": "1"}
_MAX_PLAN_BYTES = 64 * 1024 * 1024
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


@dataclass(frozen=True)
class BoundPlan:
    plan: dict[str, object]
    sha256: str
    durable_path: Path
    receipt_path: Path
    overlay_sha256: str | None


def _read_regular_bytes(path: Path, *, limit: int = _MAX_PLAN_BYTES) -> bytes:
    descriptor = os.open(path, _READ_FLAGS)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise UpgradeError(f"expected a single-link regular file: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise UpgradeError(f"file is too large: {path}")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_new_file(path: Path, payload: bytes, *, mode: int) -> None:
    descriptor = os.open(path, _CREATE_FLAGS, 0o600)
    try:
        os.fchmod(descriptor, mode)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_host_overlay(installer_root: Path) -> dict[str, object] | None:
    """The persisted host overlay minus ``legacy_adoption`` (its digest is unchanged)."""

    path = installer_root / _HOST_OVERLAY_NAME
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != _OWNER_UID
        or stat.S_IMODE(observed.st_mode) & 0o022
    ):
        raise UpgradeError(
            f"host overlay must be a regular file owned by uid {_OWNER_UID} "
            f"without group/other write: {path}"
        )
    overlay = install_cli._load_mapping(path, label="host overlay")
    stripped = {key: value for key, value in overlay.items() if key != "legacy_adoption"}
    return validate_host_overlay(stripped) or None


def plan_identity(overlay: Mapping[str, object] | None) -> tuple[int, int]:
    """The overlay's operator account, or ``nobody``; never root."""

    name = overlay.get("operator_account") if overlay is not None else None
    if isinstance(name, str) and name:
        try:
            account = _lookup_account(name)
        except KeyError as exc:
            raise UpgradeError(
                f"host overlay operator_account does not exist on this host: {name}"
            ) from exc
        if account.pw_uid != 0:
            return account.pw_uid, account.pw_gid
    try:
        fallback = _lookup_account("nobody")
    except KeyError as exc:
        raise UpgradeError("no unprivileged account is available to produce the plan") from exc
    if fallback.pw_uid == 0:
        raise UpgradeError("the fallback plan account resolves to root")
    return fallback.pw_uid, fallback.pw_gid


def publish_durable_plan(payload: bytes, expected_sha256: str, *, plans_root: Path) -> Path:
    """Runbook §2 durable publication: never overwrite, reuse only identical bytes."""

    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise UpgradeError("reviewed plan changed before durable publication")
    try:
        os.mkdir(plans_root, 0o700)
    except FileExistsError:
        pass
    root_stat = plans_root.lstat()
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != _OWNER_UID
        or stat.S_IMODE(root_stat.st_mode) != 0o700
    ):
        raise UpgradeError("durable plan root is unsafe")
    target_name = f"{expected_sha256}.json"
    staging_name = f".{target_name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    root_fd = os.open(plans_root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    lock_fd: int | None = None
    staging_fd: int | None = None
    try:
        lock_fd = os.open(
            ".publish.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root_fd,
        )
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        lock_stat = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != _OWNER_UID
            or lock_stat.st_nlink != 1
            or stat.S_IMODE(lock_stat.st_mode) != 0o600
        ):
            raise UpgradeError("durable plan publication lock is unsafe")
        try:
            existing_fd = os.open(target_name, _READ_FLAGS, dir_fd=root_fd)
        except FileNotFoundError:
            existing_fd = None
        if existing_fd is not None:
            try:
                observed = os.fstat(existing_fd)
                existing = b""
                while chunk := os.read(existing_fd, 1024 * 1024):
                    existing += chunk
            finally:
                os.close(existing_fd)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_uid != _OWNER_UID
                or observed.st_nlink != 1
                or stat.S_IMODE(observed.st_mode) != 0o600
                or existing != payload
            ):
                raise UpgradeError("existing durable plan does not match reviewed bytes")
        else:
            staging_fd = os.open(staging_name, _CREATE_FLAGS, 0o600, dir_fd=root_fd)
            os.fchmod(staging_fd, 0o600)
            _write_all(staging_fd, payload)
            os.fsync(staging_fd)
            os.close(staging_fd)
            staging_fd = None
            _rename_noreplace_at(root_fd, staging_name, target_name)
            os.fsync(root_fd)
    finally:
        if staging_fd is not None:
            os.close(staging_fd)
        try:
            os.unlink(staging_name, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(root_fd)
    return plans_root / target_name


def _bind_plan_to_release(
    plan: Mapping[str, object],
    *,
    sealed: SealedCandidate,
    prior: PriorReceipt,
    overlay_sha: str | None,
) -> None:
    candidate = plan.get("candidate")
    wheel = candidate.get("wheel") if isinstance(candidate, Mapping) else None
    if not isinstance(candidate, Mapping) or candidate.get("candidate_sha") != sealed.metadata.commit:
        raise UpgradeError("plan candidate is not the release tag target")
    if not isinstance(wheel, Mapping) or wheel.get("sha256") != sealed.metadata.wheel.sha256:
        raise UpgradeError("plan candidate wheel is not the release wheel")
    if format_version(wheel_version(wheel.get("path"))) != sealed.metadata.version:
        raise UpgradeError("plan candidate wheel version is not the requested version")
    if plan.get("host_overlay_sha256") != overlay_sha:
        raise UpgradeError("plan did not bind the host overlay it was given")
    if "legacy_adoption" in plan:
        raise UpgradeError("an upgrade plan must not carry a legacy_adoption block")
    validate_prior_receipt_handoff(plan, prior.receipt)


def produce_plan(
    sealed: SealedCandidate, prior: PriorReceipt, *, options: UpgradeOptions
) -> BoundPlan:
    """Runbook §2 without the human: plan unprivileged, bind the sha, publish durably."""

    installer_root = install_cli._TRUST_ROOT_MAINTENANCE_ROOT
    overlay = read_host_overlay(installer_root)
    record = host_overlay_record(overlay) if overlay is not None else None
    overlay_sha = str(record["sha256"]) if record is not None else None
    if overlay_sha != prior.plan.get("host_overlay_sha256") and not options.allow_same_version:
        raise UpgradeError(
            "the host overlay differs from the one the current receipt was planned with; "
            "overlay changes go through trust-root-transactional-install.md"
        )
    uid, gid = plan_identity(overlay)
    work = sealed.attempt_dir / "plan"
    os.mkdir(work, 0o700)
    _chown(work, uid, gid)
    home = work / "home"
    os.mkdir(home, 0o700)
    _chown(home, uid, gid)
    overlay_args: tuple[str, ...] = ()
    if overlay is not None:
        overlay_path = sealed.attempt_dir / "host-overlay.json"
        _write_new_file(
            overlay_path,
            (json.dumps(overlay, sort_keys=True) + "\n").encode("utf-8"),
            mode=0o644,
        )
        overlay_args = ("--host-overlay", str(overlay_path))
    output = work / "install-plan.json"
    sealed.assert_unchanged()
    result = _run(
        (
            str(sealed.cli),
            "install",
            "trust-root",
            "plan",
            "--config",
            str(sealed.install_config),
            "--bundle",
            str(sealed.bundle),
            *overlay_args,
            "--output",
            str(output),
        ),
        check=True,
        env={**_PLAN_ENV, "HOME": str(home), "PATH": f"{sealed.venv}/bin:/usr/bin:/bin"},
        uid=uid,
        gid=gid,
    )
    try:
        reported = json.loads(result.stdout)["plan_sha256"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise UpgradeError("candidate plan did not report its plan_sha256") from exc
    payload = _read_regular_bytes(output)
    observed = hashlib.sha256(payload).hexdigest()
    if reported != observed:
        raise UpgradeError("candidate plan sha256 does not match the plan file")
    plan = json.loads(payload.decode("utf-8"))
    if not isinstance(plan, dict) or plan_sha256(plan) != observed:
        raise UpgradeError("plan file is not the canonical plan document")
    _bind_plan_to_release(plan, sealed=sealed, prior=prior, overlay_sha=overlay_sha)
    canonical = Path(str(plan.get("receipt_path")))
    if not canonical.is_absolute() or ".." in canonical.parts:
        raise UpgradeError("plan receipt_path is not absolute")
    durable = publish_durable_plan(payload, observed, plans_root=installer_root / "plans")
    receipt_path = canonical.with_name(f"{canonical.stem}.run-{secrets.token_hex(16)}.json")
    return BoundPlan(
        plan=plan,
        sha256=observed,
        durable_path=durable,
        receipt_path=receipt_path,
        overlay_sha256=overlay_sha,
    )
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_trust_root_upgrade_plan.py tests/test_trust_root_upgrade_preflight.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add paulsha_cortex/trust_root/install/upgrade.py tests/upgrade_fixtures.py tests/test_trust_root_upgrade_plan.py
git commit -m "feat(trust-root): cortex upgrade 以非 root 產生 plan 並綁定 durable plan（#1263）"
```

---
### Task 9: transaction：lease → 停服務 → apply → 憑證 → activate → verify → loaded runtime → report，含失敗分支

**Files:**
- Modify: `paulsha_cortex/trust_root/install/upgrade.py`（檔尾續寫）
- Modify: `tests/upgrade_fixtures.py`（檔尾續寫）
- Test: `tests/test_trust_root_upgrade_transaction.py`

**Interfaces:**
- Consumes: Task 6 `ingest_release(...) -> SealedCandidate`；Task 7 `preflight`、`await_loaded_runtime`、`_pin_import_paths`、`PriorReceipt`、`UpgradeOptions`；Task 8 `produce_plan`、`BoundPlan`；Task 2 的 candidate 子命令 `credentials inherit`；`install_cli._maintenance_lease(plan, *, lifecycle_state)`、`_service_snapshot() -> (present, active)`、`_write_maintenance_snapshot(payload)`、`_stop_current_services()`、`_restore_snapshot_services(services) -> list[str]`、`_clear_maintenance_snapshot(plan, *, receipt_path)`、`_systemctl(*args)`、`_MAINTENANCE_SERVICES`、`_host_lock`；`core.atomic_write_json(path, value, *, mode)`。
- Produces（Task 10、11 使用）：
  - `class UpgradeInterrupted(BaseException)`、`class StepLog`（`rows: list[dict]`、`current: str | None`、`step(name) -> ContextManager`）
  - `run_transaction(sealed: SealedCandidate, bound: BoundPlan, prior: PriorReceipt, report: dict[str, object], steps: StepLog) -> int`（0＝upgraded；1＝rolled-back 或 halted）
  - `perform_upgrade(options: UpgradeOptions) -> int`、`run_upgrade(options: UpgradeOptions) -> int`
  - `_publish_report(report: Mapping[str, object]) -> None`（寫 `<installer_root>/<version>/upgrade-report.json` 與 `<installer_root>/last-upgrade-report.json`）、`_now() -> str`
  - report 欄位：`schema_version`、`version`、`result`（`upgraded`／`rolled-back`／`halted`／`refused`／`in-progress`）、`phase`、`failed_step`、`error`、`started_at`、`finished_at`、`prior_receipt`、`candidate`（含 `assets` digest）、`plan`（`sha256`、`durable_path`、`host_overlay_sha256`）、`receipt`、`inherited_credentials`、`verify_evidence`、`loaded_runtime`、`rollback`（`attempted`、`restore_safe`、`retained_unknown`、`retained_drift`、`services_restored`、`prior_loaded_runtime`）、`services_stopped`、`next_action`、`steps`、`report_path`
- Produces（`tests/upgrade_fixtures.py`）：`SERVICES`、`status_payload(receipt_id, wheel, commit) -> dict`、`FakeSystemd`、`FakeCandidateCli`、`make_bound(tmp_path) -> upgrade.BoundPlan`

- [ ] **Step 1: 寫會失敗的測試**

在 `tests/upgrade_fixtures.py` 的 import 區塊加入 `from dataclasses import dataclass, field` 與 `from paulsha_cortex.trust_root.install import cli as install_cli`，並在檔尾加入：

```python
SERVICES = install_cli._MAINTENANCE_SERVICES


def status_payload(receipt_id: str, wheel: str, commit: str) -> dict[str, object]:
    return {
        "service": {
            "loaded_runtime": {
                name: {
                    "comparison": {
                        "artifact_status": "match",
                        "config_status": "match",
                        "process_status": "match",
                        "loaded_wheel_sha256": wheel,
                    },
                    "trust_root": {
                        "status": "verified",
                        "receipt_id": receipt_id,
                        "wheel_sha256": wheel,
                        "candidate_commit": commit,
                    },
                    "installed_artifact": {"wheel_sha256": wheel},
                }
                for name in ("manager", "monitor")
            }
        }
    }


@dataclass
class FakeSystemd:
    active: set[str] = field(default_factory=lambda: set(SERVICES))
    fail_stop: set[str] = field(default_factory=set)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        verb, service = args[0], args[-1]
        if verb == "show":
            return subprocess.CompletedProcess(args, 0, "loaded\n", "")
        if verb == "is-active":
            return subprocess.CompletedProcess(args, 0 if service in self.active else 3, "", "")
        if verb == "stop":
            if service in self.fail_stop:
                return subprocess.CompletedProcess(args, 1, "", "stop failed")
            self.active.discard(service)
            return subprocess.CompletedProcess(args, 0, "", "")
        if verb == "start":
            self.active.add(service)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected systemctl call: {args}")


class FakeCandidateCli:
    """Stands in for the sealed candidate installer and the installed status probe."""

    def __init__(self, systemd: FakeSystemd) -> None:
        self.systemd = systemd
        self.calls: list[tuple[str, ...]] = []
        self.fail: dict[str, str] = {}
        self.interrupt_after: str | None = None
        self.rollback_result: dict[str, object] = {
            "restore_safe": True,
            "retained_unknown": [],
            "retained_drift": [],
            "systemd_daemon_reload": "not-required",
        }
        self.loaded: dict[str, tuple[str, str, str]] = {}

    @staticmethod
    def _value(argv: tuple[str, ...], flag: str) -> str:
        return argv[argv.index(flag) + 1]

    def commands(self) -> list[str]:
        return [call[0] for call in self.calls]

    def __call__(self, argv, *, check=False, env=None, uid=None, gid=None, **_kwargs):
        argv = tuple(argv)
        if argv[1:4] == ("service", "status", "--system"):
            receipt = self._value(argv, "--install-receipt")
            self.calls.append(("status", receipt))
            identity = self.loaded.get(receipt, ("unknown", "0" * 64, "0" * 40))
            return subprocess.CompletedProcess(argv, 0, json.dumps(status_payload(*identity)), "")
        assert argv[1:3] == ("install", "trust-root"), argv
        command = "credentials inherit" if argv[3] == "credentials" else argv[3]
        self.calls.append((command, *argv[4:]))
        self.systemd.calls.append(("candidate", command))
        if command in self.fail:
            return subprocess.CompletedProcess(
                argv, 1, "", f"trust-root install failed: {self.fail[command]}\n"
            )
        if command == "apply":
            receipt = Path(self._value(argv, "--receipt"))
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text("{}\n", encoding="utf-8")
            if self.interrupt_after == "apply":
                raise upgrade.UpgradeInterrupted("SIGTERM")
            payload: dict[str, object] = {
                "receipt": str(receipt),
                "receipt_id": "new-receipt",
                "state": "applied",
            }
        elif command == "credentials inherit":
            payload = {
                "receipt_id": "new-receipt",
                "inherited": [
                    {"principal": "builder", "provider": "codex", "inherited_from": "prior-receipt"}
                ],
            }
        elif command == "activate":
            self.systemd.active.update(SERVICES)
            payload = {"receipt_id": "new-receipt", "services_started": True, "qualified": False}
        elif command == "verify":
            Path(self._value(argv, "--evidence")).write_text('{"result":"pass"}\n', encoding="utf-8")
            payload = {"ok": True}
        elif command == "rollback":
            self.systemd.active.clear()
            code = 0 if self.rollback_result.get("restore_safe") is True else 1
            return subprocess.CompletedProcess(argv, code, json.dumps(self.rollback_result), "")
        else:
            raise AssertionError(f"unexpected candidate command: {argv}")
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")


def make_bound(tmp_path: Path) -> upgrade.BoundPlan:
    plan = plan_document(tmp_path, version="0.1.13", wheel_sha256=NEW_WHEEL, commit=NEW_COMMIT)
    sha = plan_sha256(plan)
    canonical = Path(str(plan["receipt_path"]))
    return upgrade.BoundPlan(
        plan=plan,
        sha256=sha,
        durable_path=tmp_path / "installer" / "plans" / f"{sha}.json",
        receipt_path=canonical.with_name(f"{canonical.stem}.run-{'0' * 32}.json"),
        overlay_sha256=None,
    )
```

建立 `tests/test_trust_root_upgrade_transaction.py`：

```python
"""#1263：transaction 的步驟順序、sha／token 綁定，以及每一種失敗分支。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import upgrade_fixtures as fx
from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install import upgrade
from paulsha_cortex.trust_root.install.release_ingress import IngressError


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "installer")
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _o, _p: None)
    monkeypatch.setattr(upgrade, "_STATUS_SETTLE_SECONDS", 0)
    monkeypatch.setattr(upgrade, "installed_runtime_env", lambda *_args, **_kwargs: {})
    systemd = fx.FakeSystemd()
    monkeypatch.setattr(install_cli, "_systemctl", systemd)
    cli = fx.FakeCandidateCli(systemd)
    monkeypatch.setattr(upgrade, "_run", cli)
    sealed = fx.make_sealed(tmp_path)
    bound = fx.make_bound(tmp_path)
    prior = fx.make_prior(tmp_path)
    cli.loaded[str(bound.receipt_path)] = ("new-receipt", fx.NEW_WHEEL, fx.NEW_COMMIT)
    cli.loaded[str(prior.path)] = ("prior-receipt", fx.PRIOR_WHEEL, fx.PRIOR_COMMIT)
    return SimpleNamespace(systemd=systemd, cli=cli, sealed=sealed, bound=bound, prior=prior)


def _run_transaction(harness: SimpleNamespace) -> tuple[int, dict[str, object]]:
    report: dict[str, object] = {"version": "0.1.13", "services_stopped": False}
    code = upgrade.run_transaction(
        harness.sealed, harness.bound, harness.prior, report, upgrade.StepLog()
    )
    return code, report


def _marker() -> object:
    with install_cli._host_lock_file(leaf="maintenance.lock") as lock_fd:
        return install_cli._maintenance_lock_payload(lock_fd, allow_absent=True)


def test_upgrade_runs_every_candidate_step_with_the_bound_sha_and_lease_token(harness) -> None:
    code, report = _run_transaction(harness)

    assert code == 0
    assert report["result"] == "upgraded"
    assert [c for c in harness.cli.commands() if c != "status"] == [
        "apply",
        "credentials inherit",
        "activate",
        "verify",
    ]
    apply = harness.cli.calls[0]
    assert apply[apply.index("--plan") + 1] == str(harness.bound.durable_path)
    assert apply[apply.index("--confirm-sha256") + 1] == harness.bound.sha256
    assert apply[apply.index("--receipt") + 1] == str(harness.bound.receipt_path)
    assert apply[apply.index("--prior-receipt") + 1] == str(harness.prior.path)
    tokens = {
        call[call.index("--maintenance-token") + 1]
        for call in harness.cli.calls
        if "--maintenance-token" in call
    }
    assert len(tokens) == 1
    assert re.fullmatch(r"[0-9a-f]{64}", tokens.pop())
    events = harness.systemd.calls
    last_stop = max(index for index, call in enumerate(events) if call[0] == "stop")
    assert last_stop < events.index(("candidate", "apply"))
    assert ("status", str(harness.bound.receipt_path)) in harness.cli.calls
    assert report["inherited_credentials"] == [
        {"principal": "builder", "provider": "codex", "inherited_from": "prior-receipt"}
    ]
    assert report["receipt"] == {
        "path": str(harness.bound.receipt_path),
        "receipt_id": "new-receipt",
    }
    assert install_cli._read_maintenance_snapshot() is None
    assert _marker() is None


def test_credential_handoff_failure_rolls_back_and_restores_services(harness) -> None:
    harness.cli.fail["credentials inherit"] = (
        "new plan requires credentials the prior receipt never recorded: "
        "reviewer-planner/copilot; roll back and import them per "
        "trust-root-transactional-install.md §4"
    )

    code, report = _run_transaction(harness)

    assert code == 1
    assert report["result"] == "rolled-back"
    assert report["failed_step"] == "credentials"
    assert report["phase"] == "pre-activate"
    assert "§4" in report["error"]
    assert "rollback" in harness.cli.commands()
    assert "activate" not in harness.cli.commands()
    assert report["rollback"]["services_restored"] == list(fx.SERVICES)
    assert harness.systemd.active == set(fx.SERVICES)
    assert report["rollback"]["prior_loaded_runtime"]["mismatch"] == ""
    assert install_cli._read_maintenance_snapshot() is None
    assert _marker() is None


def test_post_activate_failure_with_retained_state_stays_put(harness) -> None:
    harness.cli.fail["verify"] = "verify FAIL"
    harness.cli.rollback_result = {
        "restore_safe": False,
        "retained_unknown": ["/var/lib/cortex/coordinator/jobs.json"],
        "retained_drift": [],
        "systemd_daemon_reload": "completed",
    }

    code, report = _run_transaction(harness)

    assert code == 1
    assert report["result"] == "halted"
    assert report["phase"] == "post-activate"
    assert report["failed_step"] == "verify"
    assert report["rollback"]["retained_unknown"] == ["/var/lib/cortex/coordinator/jobs.json"]
    assert "cortex upgrade --recover" in report["next_action"]
    assert not [call for call in harness.systemd.calls if call[0] == "start"]
    snapshot = install_cli._read_maintenance_snapshot()
    assert snapshot is not None
    assert snapshot["receipt_path"] == str(harness.bound.receipt_path)
    assert _marker()["plan_sha256"] == harness.bound.sha256


def test_loaded_runtime_mismatch_after_verify_rolls_back(harness) -> None:
    harness.cli.loaded[str(harness.bound.receipt_path)] = (
        "prior-receipt",
        fx.PRIOR_WHEEL,
        fx.PRIOR_COMMIT,
    )

    code, report = _run_transaction(harness)

    assert code == 1
    assert report["failed_step"] == "loaded-runtime"
    assert "manager_receipt=mismatch" in report["error"]
    assert "rollback" in harness.cli.commands()
    assert report["result"] == "rolled-back"


def test_interrupt_after_the_apply_child_wrote_the_receipt_still_rolls_back(harness) -> None:
    harness.cli.interrupt_after = "apply"

    code, report = _run_transaction(harness)

    assert code == 1
    assert report["failed_step"] == "apply"
    assert report["error"] == "interrupted by SIGTERM"
    rollback = next(call for call in harness.cli.calls if call[0] == "rollback")
    assert rollback[rollback.index("--receipt") + 1] == str(harness.bound.receipt_path)
    assert harness.systemd.active == set(fx.SERVICES)
    assert report["result"] == "rolled-back"


def test_failure_before_apply_restores_services_without_a_rollback(harness) -> None:
    harness.systemd.fail_stop.add("cortex-monitor.service")

    code, report = _run_transaction(harness)

    assert code == 1
    assert report["failed_step"] == "stop-services"
    assert "apply" not in harness.cli.commands()
    assert "rollback" not in harness.cli.commands()
    assert report["rollback"]["attempted"] is False
    assert report["result"] == "rolled-back"
    assert harness.systemd.active == set(fx.SERVICES)


def test_a_changed_sealed_cli_stops_before_apply(harness) -> None:
    (harness.sealed.venv / "bin" / "cortex").write_text("#!/bin/sh\necho changed\n")

    code, report = _run_transaction(harness)

    assert code == 1
    assert report["failed_step"] == "apply"
    assert "changed since ingress" in report["error"]
    assert "apply" not in harness.cli.commands()
    assert report["result"] == "rolled-back"


@pytest.fixture
def composed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "installer")
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _o, _p: None)
    monkeypatch.setattr(upgrade, "_pin_import_paths", lambda: None)
    prior = fx.make_prior(tmp_path)
    sealed = fx.make_sealed(tmp_path)
    bound = fx.make_bound(tmp_path)
    monkeypatch.setattr(
        upgrade, "preflight", lambda options: upgrade.Preflight(prior=prior, target=(0, 1, 13))
    )
    monkeypatch.setattr(upgrade, "ingest_release", lambda version, **_kwargs: sealed)
    monkeypatch.setattr(upgrade, "produce_plan", lambda *_args, **_kwargs: bound)
    return SimpleNamespace(tmp_path=tmp_path, prior=prior, sealed=sealed, bound=bound)


def test_perform_upgrade_publishes_the_report_and_prints_a_summary(
    composed, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    last = composed.tmp_path / "installer" / "last-upgrade-report.json"

    def transaction(_sealed, _bound, _prior, report, _steps):
        assert json.loads(last.read_text())["result"] == "in-progress"
        report["receipt"] = {
            "path": str(composed.bound.receipt_path),
            "receipt_id": "new-receipt",
        }
        report["result"] = "upgraded"
        return 0

    monkeypatch.setattr(upgrade, "run_transaction", transaction)

    assert upgrade.perform_upgrade(upgrade.UpgradeOptions(version="0.1.13")) == 0

    report = json.loads(
        (composed.tmp_path / "installer" / "0.1.13" / "upgrade-report.json").read_text()
    )
    metadata = composed.sealed.metadata
    assert report["result"] == "upgraded"
    assert report["plan"]["sha256"] == composed.bound.sha256
    assert report["candidate"]["assets"] == {
        asset.name: asset.sha256
        for asset in (metadata.wheel, metadata.install_input, metadata.qualification)
    }
    assert report["prior_receipt"] == {
        "path": str(composed.prior.path),
        "receipt_id": "prior-receipt",
        "version": "0.1.12",
    }
    assert [row["name"] for row in report["steps"]] == ["ingress", "plan"]
    assert json.loads(last.read_text()) == report
    out = capsys.readouterr().out
    assert out.startswith("cortex upgrade 0.1.13: upgraded\n")
    assert f"new receipt:   {composed.bound.receipt_path}" in out


def test_an_ingress_failure_is_reported_as_refused(
    composed, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(version, **_kwargs):
        raise IngressError("release asset digest does not match its REST metadata: x")

    monkeypatch.setattr(upgrade, "ingest_release", refuse)
    monkeypatch.setattr(
        upgrade, "run_transaction", lambda *_args: pytest.fail("must not reach the transaction")
    )

    assert upgrade.perform_upgrade(
        upgrade.UpgradeOptions(version="0.1.13", json_output=True)
    ) == 1

    report = json.loads(capsys.readouterr().out)
    assert report["result"] == "refused"
    assert report["failed_step"] == "ingress"
    assert "REST metadata" in report["error"]
    assert (composed.tmp_path / "installer" / "0.1.13" / "upgrade-report.json").exists()
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_trust_root_upgrade_transaction.py -q`
Expected: 全部 FAIL／ERROR，`AttributeError: module 'paulsha_cortex.trust_root.install.upgrade' has no attribute 'StepLog'`（harness 測試）或 `... has no attribute 'run_transaction'`（`composed` 測試的 monkeypatch）。`upgrade_fixtures` 有 `from __future__ import annotations`，import 本身不會失敗。

- [ ] **Step 3: 最小實作**

在 `paulsha_cortex/trust_root/install/upgrade.py` 檔尾續寫：

```python
# --- transaction (spec §4 steps 4–8, §6, §12.4) -----------------------------------

_CANDIDATE_ENV = {"HOME": "/root", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONNOUSERSITE": "1"}
_REPORT_NAME = "upgrade-report.json"
_LAST_REPORT_NAME = "last-upgrade-report.json"
_HALTED_NEXT_ACTION = (
    "services remain stopped and the maintenance snapshot is kept; resolve the "
    "retained state listed above, then run `cortex upgrade --recover` or decide "
    "by hand per trust-root-transactional-install.md §6"
)


class UpgradeInterrupted(BaseException):
    """INT/TERM inside the maintenance window; handled like any step failure."""


def _interrupt(signum: int, _frame: object) -> None:
    raise UpgradeInterrupted(signal.Signals(signum).name)


@contextmanager
def _signals_raise() -> Iterator[None]:
    previous = {sig: signal.signal(sig, _interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _ignore_interrupts() -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal.SIG_IGN)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _describe(error: BaseException) -> str:
    if isinstance(error, UpgradeInterrupted):
        return f"interrupted by {error}"
    return str(error) or type(error).__name__


class StepLog:
    """Per-step timing and status for the upgrade report."""

    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.current: str | None = None

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        self.current = name
        started = _monotonic()
        try:
            yield
        except BaseException:
            self.rows.append(
                {"name": name, "status": "failed", "seconds": round(_monotonic() - started, 3)}
            )
            raise
        self.rows.append(
            {"name": name, "status": "passed", "seconds": round(_monotonic() - started, 3)}
        )


@dataclass
class _TransactionState:
    previously_active: list[str] = field(default_factory=list)
    apply_attempted: bool = False
    activation_attempted: bool = False
    receipt_id: str | None = None


def _candidate(sealed: SealedCandidate, *arguments: str) -> CompletedProcess[str]:
    """One sealed-candidate installer call; the sealed tree is re-attested first."""

    sealed.assert_unchanged()
    return _run(
        (str(sealed.cli), "install", "trust-root", *arguments),
        env={**_CANDIDATE_ENV, "PATH": f"{sealed.venv}/bin:/usr/bin:/bin"},
    )


def _candidate_json(sealed: SealedCandidate, step: str, *arguments: str) -> dict[str, object]:
    result = _candidate(sealed, *arguments)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise UpgradeError(f"{step} failed: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise UpgradeError(f"{step} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise UpgradeError(f"{step} returned invalid JSON")
    return payload


def _advance(
    sealed: SealedCandidate,
    bound: BoundPlan,
    prior: PriorReceipt,
    token: str,
    report: dict[str, object],
    steps: StepLog,
    state: _TransactionState,
) -> None:
    receipt = str(bound.receipt_path)
    with steps.step("stop-services"):
        install_cli._stop_current_services()
    with steps.step("apply"):
        # Set before the child starts: a signal that lands after the child wrote
        # the receipt but before it returned must still roll that receipt back.
        state.apply_attempted = True
        applied = _candidate_json(
            sealed,
            "apply",
            "apply",
            "--plan",
            str(bound.durable_path),
            "--confirm-sha256",
            bound.sha256,
            "--receipt",
            receipt,
            "--prior-receipt",
            str(prior.path),
            "--maintenance-token",
            token,
        )
        state.receipt_id = str(applied.get("receipt_id"))
        report["receipt"] = {"path": receipt, "receipt_id": state.receipt_id}
    with steps.step("credentials"):
        inherited = _candidate_json(
            sealed,
            "credential inheritance",
            "credentials",
            "inherit",
            "--receipt",
            receipt,
            "--prior-receipt",
            str(prior.path),
            "--maintenance-token",
            token,
        )
        report["inherited_credentials"] = inherited.get("inherited", [])
    with steps.step("activate"):
        state.activation_attempted = True
        _candidate_json(
            sealed, "activate", "activate", "--receipt", receipt, "--maintenance-token", token
        )
    with steps.step("verify"):
        evidence = sealed.attempt_dir / "install-verification.json"
        report["verify_evidence"] = str(evidence)
        verified = _candidate(
            sealed,
            "verify",
            "--receipt",
            receipt,
            "--json",
            "--evidence",
            str(evidence),
            "--maintenance-token",
            token,
        )
        if verified.returncode != 0:
            raise UpgradeError("verify did not PASS")
        inactive = [
            service
            for service in install_cli._MAINTENANCE_SERVICES
            if install_cli._systemctl("is-active", "--quiet", service).returncode != 0
        ]
        if inactive:
            raise UpgradeError("services are not active after activation: " + ", ".join(inactive))
    with steps.step("loaded-runtime"):
        expected = runtime_expected({"receipt_id": state.receipt_id, "plan": bound.plan})
        mismatch, _payload = await_loaded_runtime(bound.plan, bound.receipt_path, expected)
        report["loaded_runtime"] = {"expected": expected, "mismatch": mismatch}
        if mismatch:
            raise UpgradeError(f"loaded runtime does not match the new receipt: {mismatch}")


def _abort(
    sealed: SealedCandidate,
    bound: BoundPlan,
    prior: PriorReceipt,
    token: str,
    report: dict[str, object],
    steps: StepLog,
    state: _TransactionState,
    error: BaseException,
    lifecycle: dict[str, bool],
) -> int:
    """Roll the new receipt back; restore services only when that is restore-safe."""

    _ignore_interrupts()
    report["failed_step"] = steps.current
    report["error"] = _describe(error)
    report["phase"] = "post-activate" if state.activation_attempted else "pre-activate"
    rollback: dict[str, object] = {
        "attempted": False,
        "restore_safe": True,
        "retained_unknown": [],
        "retained_drift": [],
    }
    report["rollback"] = rollback
    if state.apply_attempted and os.path.lexists(bound.receipt_path):
        rollback["attempted"] = True
        payload: object = None
        try:
            result = _candidate(
                sealed,
                "rollback",
                "--receipt",
                str(bound.receipt_path),
                "--maintenance-token",
                token,
            )
            payload = json.loads(result.stdout) if result.stdout.strip() else None
        except (InstallError, OSError, json.JSONDecodeError) as exc:
            rollback["error"] = _describe(exc)
        if not isinstance(payload, dict):
            payload = {}
        rollback["restore_safe"] = payload.get("restore_safe") is True
        rollback["retained_unknown"] = list(payload.get("retained_unknown") or [])
        rollback["retained_drift"] = list(payload.get("retained_drift") or [])
    if rollback["restore_safe"] is not True:
        report["result"] = "halted"
        report["next_action"] = _HALTED_NEXT_ACTION
        return 1
    try:
        rollback["services_restored"] = install_cli._restore_snapshot_services(
            state.previously_active
        )
    except InstallError as exc:
        rollback["services_restored"] = []
        rollback["restore_error"] = str(exc)
        report["result"] = "halted"
        report["next_action"] = (
            "restoring the previous services failed; they were stopped again and the "
            "snapshot is kept: run `cortex upgrade --recover`"
        )
        return 1
    if state.previously_active:
        mismatch, status = await_loaded_runtime(
            prior.plan, prior.path, runtime_expected(prior.document)
        )
        rollback["prior_loaded_runtime"] = {"mismatch": mismatch, "service_status": status}
    else:
        mismatch = ""
        rollback["prior_loaded_runtime"] = {
            "mismatch": "",
            "service_status": None,
            "reason": "no-previously-active-services",
        }
    install_cli._clear_maintenance_snapshot(bound.plan, receipt_path=bound.receipt_path)
    lifecycle["complete"] = True
    report["result"] = "rolled-back"
    if mismatch:
        report["next_action"] = (
            "the previous services run again but do not match the current receipt; "
            "inspect `cortex service status --system`"
        )
    return 1


def run_transaction(
    sealed: SealedCandidate,
    bound: BoundPlan,
    prior: PriorReceipt,
    report: dict[str, object],
    steps: StepLog,
) -> int:
    """Lease → snapshot → stop → apply → inherit → activate → verify → loaded runtime."""

    state = _TransactionState()
    lifecycle = {"complete": False}
    with _signals_raise():
        with install_cli._maintenance_lease(bound.plan, lifecycle_state=lifecycle) as token:
            try:
                with steps.step("maintenance-snapshot"):
                    if os.path.lexists(bound.receipt_path):
                        raise UpgradeError(
                            "the new receipt path already exists; run `cortex upgrade --recover`"
                        )
                    present, previously_active = install_cli._service_snapshot()
                    state.previously_active = list(previously_active)
                    install_cli._write_maintenance_snapshot(
                        {
                            "schema_version": 1,
                            "plan_sha256": bound.sha256,
                            "receipt_path": str(bound.receipt_path),
                            "present_services": present,
                            "previously_active": previously_active,
                        }
                    )
            except BaseException:
                # Nothing was stopped yet; the lease marker may go with the lease.
                lifecycle["complete"] = True
                raise
            report["services_stopped"] = True
            try:
                _advance(sealed, bound, prior, token, report, steps, state)
            except BaseException as error:  # noqa: BLE001 — every failure rolls back
                return _abort(
                    sealed, bound, prior, token, report, steps, state, error, lifecycle
                )
            install_cli._clear_maintenance_snapshot(bound.plan, receipt_path=bound.receipt_path)
            lifecycle["complete"] = True
    report["result"] = "upgraded"
    return 0


def _fetcher(options: UpgradeOptions) -> ReleaseFetcher:
    if options.release_source is not None:
        return DirectoryReleaseFetcher(options.release_source)
    return GitHubReleaseFetcher(OFFICIAL_REPOSITORY)


def _report_path(version: str) -> Path:
    return install_cli._TRUST_ROOT_MAINTENANCE_ROOT / version / _REPORT_NAME


def _publish_report(report: Mapping[str, object]) -> None:
    root = install_cli._TRUST_ROOT_MAINTENANCE_ROOT
    atomic_write_json(_report_path(str(report["version"])), report, mode=0o600)
    atomic_write_json(root / _LAST_REPORT_NAME, report, mode=0o600)


def _new_report(version: str, prior: PriorReceipt, steps: StepLog) -> dict[str, object]:
    return {
        "schema_version": 1,
        "version": version,
        "result": None,
        "phase": None,
        "failed_step": None,
        "error": None,
        "started_at": _now(),
        "finished_at": None,
        "prior_receipt": {
            "path": str(prior.path),
            "receipt_id": prior.document.get("receipt_id"),
            "version": format_version(prior.version),
        },
        "candidate": None,
        "plan": None,
        "receipt": None,
        "inherited_credentials": [],
        "verify_evidence": None,
        "loaded_runtime": None,
        "rollback": None,
        "services_stopped": False,
        "next_action": None,
        "steps": steps.rows,
        "report_path": str(_report_path(version)),
    }


def _print_outcome(report: Mapping[str, object], *, json_output: bool) -> None:
    if json_output:
        sys.stdout.write(
            json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        )
        return
    lines = [f"cortex upgrade {report['version']}: {report['result']}"]
    if report.get("failed_step"):
        lines.append(f"  failed step:   {report['failed_step']} ({report.get('error')})")
    prior = report.get("prior_receipt")
    if isinstance(prior, Mapping):
        lines.append(f"  prior receipt: {prior.get('path')} ({prior.get('version')})")
    receipt = report.get("receipt")
    if isinstance(receipt, Mapping):
        lines.append(f"  new receipt:   {receipt.get('path')}")
    plan = report.get("plan")
    if isinstance(plan, Mapping):
        lines.append(f"  plan sha256:   {plan.get('sha256')}")
    if report.get("verify_evidence"):
        lines.append(f"  verify:        {report['verify_evidence']}")
    rollback = report.get("rollback")
    if isinstance(rollback, Mapping):
        lines.append(f"  rollback:      restore_safe={rollback.get('restore_safe')}")
        for key in ("retained_unknown", "retained_drift"):
            for row in rollback.get(key) or []:
                lines.append(f"    {key}: {json.dumps(row, ensure_ascii=False, sort_keys=True)}")
        if "services_restored" in rollback:
            restored = rollback.get("services_restored") or []
            lines.append(
                "  services restored: " + (", ".join(str(name) for name in restored) or "none")
            )
    if report.get("next_action"):
        lines.append(f"  next:          {report['next_action']}")
    lines.append(f"  report:        {report.get('report_path')}")
    sys.stdout.write("\n".join(lines) + "\n")


def perform_upgrade(options: UpgradeOptions) -> int:
    """Spec §4 end to end; every outcome after preflight lands in the report."""

    previous_umask = os.umask(0o077)
    try:
        _pin_import_paths()
        with install_cli._host_lock(
            leaf="upgrade.lock", conflict="another `cortex upgrade` is already running"
        ):
            checked = preflight(options)
            version = format_version(checked.target)
            steps = StepLog()
            report = _new_report(version, checked.prior, steps)
            code = 1
            try:
                with steps.step("ingress"):
                    sealed = ingest_release(
                        version,
                        fetcher=_fetcher(options),
                        installer_root=install_cli._TRUST_ROOT_MAINTENANCE_ROOT,
                        owner_uid=_OWNER_UID,
                        chain_stop=_CHAIN_STOP,
                    )
                report["candidate"] = {
                    "tag": sealed.metadata.tag,
                    "commit": sealed.metadata.commit,
                    "assets": {
                        asset.name: asset.sha256
                        for asset in (
                            sealed.metadata.wheel,
                            sealed.metadata.install_input,
                            sealed.metadata.qualification,
                        )
                    },
                    "attempt_dir": str(sealed.attempt_dir),
                    "cli_tree_sha256": sealed.tree_sha256,
                }
                with steps.step("plan"):
                    bound = produce_plan(sealed, checked.prior, options=options)
                report["plan"] = {
                    "sha256": bound.sha256,
                    "durable_path": str(bound.durable_path),
                    "host_overlay_sha256": bound.overlay_sha256,
                }
                report["result"] = "in-progress"
                _publish_report(report)
                code = run_transaction(sealed, bound, checked.prior, report, steps)
            except BaseException as error:  # noqa: BLE001 — every outcome is reported
                if report["result"] in (None, "in-progress"):
                    stopped = report["services_stopped"] is True
                    report["result"] = "halted" if stopped else "refused"
                    report["failed_step"] = steps.current
                    report["error"] = _describe(error)
                    if stopped:
                        report["next_action"] = (
                            "the upgrade stopped inside the maintenance window; "
                            "run `cortex upgrade --recover`"
                        )
                code = 1
            finally:
                report["finished_at"] = _now()
                _publish_report(report)
            _print_outcome(report, json_output=options.json_output)
            return code
    finally:
        os.umask(previous_umask)


def run_upgrade(options: UpgradeOptions) -> int:
    return perform_upgrade(options)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_trust_root_upgrade_transaction.py tests/test_trust_root_upgrade_plan.py tests/test_trust_root_upgrade_preflight.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add paulsha_cortex/trust_root/install/upgrade.py tests/upgrade_fixtures.py tests/test_trust_root_upgrade_transaction.py
git commit -m "feat(trust-root): cortex upgrade transaction 與自動 rollback 分支（#1263）"
```

---

### Task 10: `--recover` 與 `--status`

**Files:**
- Modify: `paulsha_cortex/trust_root/install/upgrade.py`（檔尾續寫；並把 Task 9 的 `run_upgrade` 換成下方版本）
- Test: `tests/test_trust_root_upgrade_recover_status.py`

**Interfaces:**
- Consumes: Task 3 `effective_receipt`；Task 7 `await_loaded_runtime`、`_pin_import_paths`、`_STATE_ROOT`、`_OWNER_UID`；Task 8 `publish_durable_plan`、`_read_regular_bytes`；Task 9 `_publish_report`、`_now`、`perform_upgrade`；`install_cli._read_maintenance_snapshot() -> dict | None`、`install_cli._host_lock_file(*, leaf, root=None)`、`install_cli._maintenance_lock_payload`、`install_cli._recover_command(args: argparse.Namespace) -> int`（`args.plan`、`args.confirm_sha256`）、`install_cli._emit(payload)`。
- Produces（Task 11 使用）：
  - `verified_durable_plan(plan_sha: str) -> Path`
  - `recover_upgrade() -> int`
  - `upgrade_status() -> dict[str, object]`（鍵：`effective_receipt`、`effective_receipt_error`（失敗時）、`loaded_runtime`、`last_upgrade`、`maintenance_pending`）
  - `run_upgrade(options: UpgradeOptions) -> int`：`status` → 唯讀並以 0／1 表示是否一致；`recover` → `recover_upgrade()`；其餘 → `perform_upgrade(options)`

- [ ] **Step 1: 寫會失敗的測試**

建立 `tests/test_trust_root_upgrade_recover_status.py`：

```python
"""#1263：`--recover` 只讀 snapshot 推出 durable plan；`--status` 唯讀。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import upgrade_fixtures as fx
from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install import upgrade
from paulsha_cortex.trust_root.install.core import InstallError


@pytest.fixture
def recovering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[object]:
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "installer")
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _o, _p: None)
    monkeypatch.setattr(upgrade, "_OWNER_UID", os.getuid())
    monkeypatch.setattr(upgrade, "_pin_import_paths", lambda: None)
    monkeypatch.setattr(
        upgrade,
        "ingest_release",
        lambda *_args, **_kwargs: pytest.fail("recover must not fetch a release"),
    )
    (tmp_path / "installer").mkdir()
    captured: list[object] = []

    def recover_command(args) -> int:
        captured.append(args)
        return 0

    monkeypatch.setattr(install_cli, "_recover_command", recover_command)
    return captured


def _publish(tmp_path: Path, payload: bytes = b'{"plan":"x"}\n') -> str:
    sha = hashlib.sha256(payload).hexdigest()
    upgrade.publish_durable_plan(payload, sha, plans_root=tmp_path / "installer" / "plans")
    return sha


def _snapshot(tmp_path: Path, sha: str) -> None:
    install_cli._write_maintenance_snapshot(
        {
            "schema_version": 1,
            "plan_sha256": sha,
            "receipt_path": str(tmp_path / "receipts" / "next.run-1.json"),
            "present_services": ["cortex-manager.service"],
            "previously_active": ["cortex-manager.service"],
        }
    )


def test_recover_derives_the_durable_plan_from_the_snapshot_only(
    tmp_path: Path, recovering
) -> None:
    sha = _publish(tmp_path)
    _snapshot(tmp_path, sha)

    assert upgrade.recover_upgrade() == 0

    assert recovering[0].plan == str(tmp_path / "installer" / "plans" / f"{sha}.json")
    assert recovering[0].confirm_sha256 == sha


def test_recover_falls_back_to_a_stale_lease_marker(tmp_path: Path, recovering) -> None:
    sha = _publish(tmp_path)
    with install_cli._host_lock(leaf="maintenance.lock", conflict="unexpected") as lock_fd:
        install_cli._write_lock_payload(lock_fd, {"plan_sha256": sha, "token_sha256": "b" * 64})

    assert upgrade.recover_upgrade() == 0
    assert recovering[0].confirm_sha256 == sha


def test_recover_with_nothing_interrupted_does_nothing(
    tmp_path: Path, recovering, capsys: pytest.CaptureFixture[str]
) -> None:
    assert upgrade.recover_upgrade() == 0

    assert recovering == []
    assert json.loads(capsys.readouterr().out) == {
        "maintenance_recovered": False,
        "reason": "nothing-to-recover",
    }


def test_recover_refuses_a_tampered_durable_plan(tmp_path: Path, recovering) -> None:
    sha = _publish(tmp_path)
    _snapshot(tmp_path, sha)
    (tmp_path / "installer" / "plans" / f"{sha}.json").write_bytes(b'{"plan":"y"}\n')

    with pytest.raises(upgrade.UpgradeError, match="durable reviewed plan digest mismatch"):
        upgrade.recover_upgrade()
    assert recovering == []


def test_recover_refuses_while_the_maintenance_window_is_held(
    tmp_path: Path, recovering
) -> None:
    plan = fx.plan_document(
        tmp_path, version="0.1.13", wheel_sha256=fx.NEW_WHEEL, commit=fx.NEW_COMMIT
    )

    with install_cli._maintenance_lease(plan):
        with pytest.raises(upgrade.UpgradeError, match="still held by a live process"):
            upgrade.recover_upgrade()
    assert recovering == []


def test_recover_marks_the_last_report_recovered(tmp_path: Path, recovering) -> None:
    sha = _publish(tmp_path)
    _snapshot(tmp_path, sha)
    upgrade._publish_report({"version": "0.1.13", "result": "in-progress", "plan": {"sha256": sha}})

    assert upgrade.recover_upgrade() == 0

    report = json.loads((tmp_path / "installer" / "last-upgrade-report.json").read_text())
    assert report["result"] == "recovered"


def test_status_is_read_only_and_reports_receipt_runtime_and_last_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "installer")
    (tmp_path / "installer").mkdir()
    upgrade._publish_report(
        {
            "version": "0.1.14",
            "result": "rolled-back",
            "failed_step": "credentials",
            "error": "new plan requires credentials the prior receipt never recorded",
            "finished_at": "2026-10-05T00:00:00Z",
            "report_path": "/var/lib/cortex-installer/0.1.14/upgrade-report.json",
        }
    )
    prior = fx.make_prior(tmp_path)
    monkeypatch.setattr(upgrade, "effective_receipt", lambda _state_root: prior.receipt)
    monkeypatch.setattr(
        upgrade, "await_loaded_runtime", lambda plan, path, expected, **_kwargs: ("", {})
    )
    before = sorted(str(path) for path in tmp_path.rglob("*"))

    status = upgrade.upgrade_status()

    assert sorted(str(path) for path in tmp_path.rglob("*")) == before
    assert status["effective_receipt"] == {
        "path": str(prior.path),
        "receipt_id": "prior-receipt",
        "version": "0.1.12",
    }
    assert status["loaded_runtime"] == "match"
    assert status["last_upgrade"]["result"] == "rolled-back"
    assert status["maintenance_pending"] is False
    assert upgrade.run_upgrade(upgrade.UpgradeOptions(status=True, json_output=True)) == 0
    assert json.loads(capsys.readouterr().out)["effective_receipt"]["version"] == "0.1.12"


def test_status_is_non_zero_when_the_receipt_cannot_be_decided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "installer")

    def undecided(_state_root):
        raise InstallError(
            "cannot decide the effective receipt: 2 applied and qualified receipts "
            "have no qualified successor (a.json, b.json)"
        )

    monkeypatch.setattr(upgrade, "effective_receipt", undecided)

    assert upgrade.run_upgrade(upgrade.UpgradeOptions(status=True)) == 1
    assert "cannot decide the effective receipt" in capsys.readouterr().out
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_trust_root_upgrade_recover_status.py -q`
Expected: FAIL，`AttributeError: module 'paulsha_cortex.trust_root.install.upgrade' has no attribute 'recover_upgrade'`（status 測試則是 `upgrade_status`）。

- [ ] **Step 3: 最小實作**

刪除 Task 9 加在檔尾的：

```python
def run_upgrade(options: UpgradeOptions) -> int:
    return perform_upgrade(options)
```

並在檔尾續寫：

```python
# --- recovery and status (spec §3, §7, §12.6) ------------------------------------

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def _interrupted_plan_sha() -> str | None:
    """Plan sha of an interrupted upgrade: the snapshot first, else a stale marker."""

    snapshot = install_cli._read_maintenance_snapshot()
    if snapshot is not None:
        value = snapshot.get("plan_sha256")
    else:
        with install_cli._host_lock_file(leaf="maintenance.lock") as lock_fd:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise UpgradeError(
                    "the interrupted upgrade's maintenance window is still held by a "
                    "live process; recovery refuses to run"
                ) from exc
            marker = install_cli._maintenance_lock_payload(lock_fd, allow_absent=True)
        if marker is None:
            return None
        value = marker.get("plan_sha256")
    if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
        raise UpgradeError("the maintenance record does not name a plan sha256")
    return value


def verified_durable_plan(plan_sha: str) -> Path:
    """Runbook §6 checks on the root-owned durable plan named by ``plan_sha``."""

    plans_root = install_cli._TRUST_ROOT_MAINTENANCE_ROOT / "plans"
    path = plans_root / f"{plan_sha}.json"
    try:
        observed = path.lstat()
        parent = plans_root.lstat()
    except FileNotFoundError as exc:
        raise UpgradeError(
            f"the durable plan of the interrupted upgrade is missing: {path}"
        ) from exc
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != _OWNER_UID
        or observed.st_nlink != 1
        or stat.S_IMODE(observed.st_mode) != 0o600
    ):
        raise UpgradeError("durable reviewed plan is unsafe")
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != _OWNER_UID
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise UpgradeError("durable plan root is unsafe")
    if hashlib.sha256(_read_regular_bytes(path)).hexdigest() != plan_sha:
        raise UpgradeError("durable reviewed plan digest mismatch")
    return path


def _mark_last_report(plan_sha: str, *, recovered: bool) -> None:
    path = install_cli._TRUST_ROOT_MAINTENANCE_ROOT / _LAST_REPORT_NAME
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    plan = report.get("plan") if isinstance(report, dict) else None
    if not isinstance(plan, dict) or plan.get("sha256") != plan_sha:
        return
    report["result"] = "recovered" if recovered else "halted"
    report["recovered_at"] = _now()
    _publish_report(report)


def recover_upgrade() -> int:
    """Runbook §6 without re-entering the plan sha: it comes from the snapshot."""

    previous_umask = os.umask(0o077)
    try:
        _pin_import_paths()
        with install_cli._host_lock(
            leaf="upgrade.lock", conflict="another `cortex upgrade` is already running"
        ):
            plan_sha = _interrupted_plan_sha()
            if plan_sha is None:
                install_cli._emit(
                    {"maintenance_recovered": False, "reason": "nothing-to-recover"}
                )
                return 0
            durable = verified_durable_plan(plan_sha)
            try:
                code = install_cli._recover_command(
                    Namespace(plan=str(durable), confirm_sha256=plan_sha)
                )
            except InstallError:
                _mark_last_report(plan_sha, recovered=False)
                raise
            _mark_last_report(plan_sha, recovered=code == 0)
            return code
    finally:
        os.umask(previous_umask)


def upgrade_status() -> dict[str, object]:
    """Read-only: effective receipt, loaded runtime, last upgrade, pending recovery."""

    root = install_cli._TRUST_ROOT_MAINTENANCE_ROOT
    status: dict[str, object] = {
        "effective_receipt": None,
        "loaded_runtime": None,
        "last_upgrade": None,
        "maintenance_pending": os.path.lexists(install_cli._maintenance_snapshot_path()),
    }
    try:
        receipt = effective_receipt(_STATE_ROOT)
    except InstallError as exc:
        status["effective_receipt_error"] = str(exc)
    else:
        document = receipt.to_dict()
        plan = document.get("plan")
        candidate = plan.get("candidate") if isinstance(plan, Mapping) else None
        wheel = candidate.get("wheel") if isinstance(candidate, Mapping) else None
        try:
            version: str | None = format_version(
                wheel_version(wheel.get("path") if isinstance(wheel, Mapping) else None)
            )
        except InstallError:
            version = None
        status["effective_receipt"] = {
            "path": str(receipt.path),
            "receipt_id": document.get("receipt_id"),
            "version": version,
        }
        if isinstance(plan, Mapping) and receipt.path is not None:
            mismatch, _payload = await_loaded_runtime(
                plan, receipt.path, runtime_expected(document), settle_seconds=0
            )
            status["loaded_runtime"] = "match" if not mismatch else mismatch
    try:
        report = json.loads((root / _LAST_REPORT_NAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        report = None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        status["last_upgrade_error"] = f"last upgrade report is unreadable: {exc}"
        report = None
    if isinstance(report, dict):
        status["last_upgrade"] = {
            key: report.get(key)
            for key in ("version", "result", "failed_step", "error", "finished_at", "report_path")
        }
    return status


def _status_ok(status: Mapping[str, object]) -> bool:
    return (
        isinstance(status.get("effective_receipt"), Mapping)
        and status.get("loaded_runtime") == "match"
        and not status.get("maintenance_pending")
    )


def _print_status(status: Mapping[str, object], *, json_output: bool) -> None:
    if json_output:
        sys.stdout.write(
            json.dumps(status, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        )
        return
    lines = ["cortex upgrade status"]
    receipt = status.get("effective_receipt")
    if isinstance(receipt, Mapping):
        lines.append(
            f"  effective receipt: {receipt.get('path')} "
            f"({receipt.get('version')}, receipt {receipt.get('receipt_id')})"
        )
    else:
        lines.append(f"  effective receipt: unknown: {status.get('effective_receipt_error')}")
    lines.append(f"  loaded runtime:    {status.get('loaded_runtime') or 'unknown'}")
    last = status.get("last_upgrade")
    if isinstance(last, Mapping):
        detail = f" (failed step: {last.get('failed_step')})" if last.get("failed_step") else ""
        lines.append(
            f"  last upgrade:      {last.get('version')} {last.get('result')} "
            f"at {last.get('finished_at')}{detail}"
        )
    else:
        lines.append("  last upgrade:      none recorded")
    lines.append(
        "  maintenance:       "
        + (
            "unfinished snapshot: run `cortex upgrade --recover`"
            if status.get("maintenance_pending")
            else "idle"
        )
    )
    sys.stdout.write("\n".join(lines) + "\n")


def run_upgrade(options: UpgradeOptions) -> int:
    if options.status:
        status = upgrade_status()
        _print_status(status, json_output=options.json_output)
        return 0 if _status_ok(status) else 1
    if options.recover:
        return recover_upgrade()
    return perform_upgrade(options)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_trust_root_upgrade_recover_status.py tests/test_trust_root_upgrade_transaction.py tests/test_trust_root_upgrade_preflight.py tests/test_trust_root_upgrade_plan.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add paulsha_cortex/trust_root/install/upgrade.py tests/test_trust_root_upgrade_recover_status.py
git commit -m "feat(trust-root): cortex upgrade --recover 由 snapshot 推出 durable plan、--status 唯讀（#1263）"
```

---
### Task 11: CLI：`cortex install trust-root upgrade`、頂層別名 `cortex upgrade`、help 同步

**Files:**
- Modify: `paulsha_cortex/trust_root/install/cli.py:773`（`_build_parser` 的 `return parser` 之前）、`cli.py:1455`（`main` 分派，`legacy` 之前）；新增 `_add_upgrade_arguments`、`_upgrade_command`、`upgrade_main`
- Modify: `paulsha_cortex/cli.py:15-49`（`_HELP`）、`cli.py:146-149`（`install` 分派之後加 `upgrade`）
- Modify: `paulsha_cortex/deploy/installer.py:803-807`（trust-root help 字串）
- Test: `tests/test_trust_root_upgrade_cli.py`、`tests/test_cli_help_alignment.py`

**Interfaces:**
- Consumes: Task 7 `upgrade.options_from_args(args, *, environ) -> UpgradeOptions`；Task 10 `upgrade.run_upgrade(options) -> int`；既有 `cli._require_root()`。
- Produces: `cli._add_upgrade_arguments(parser: argparse.ArgumentParser) -> None`、`cli._upgrade_command(args: argparse.Namespace) -> int`、`cli.upgrade_main(argv: Sequence[str] | None = None) -> int`（prog `cortex upgrade`）；`cortex upgrade ...` 等同 `cortex install trust-root upgrade ...`；`--release-source`／`--allow-same-version`／`--prior-receipt` 在 help 中隱藏，且只有 `PSC_UPGRADE_QUALIFICATION=1` 時被接受。

- [ ] **Step 1: 寫會失敗的測試**

建立 `tests/test_trust_root_upgrade_cli.py`：

```python
"""#1263：`cortex upgrade` 的 CLI 介面與只限測試參數的閘門。"""
from __future__ import annotations

import pytest

from paulsha_cortex import cli as umbrella_cli
from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install import upgrade


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[upgrade.UpgradeOptions]:
    seen: list[upgrade.UpgradeOptions] = []

    def run_upgrade(options: upgrade.UpgradeOptions) -> int:
        seen.append(options)
        return 0

    monkeypatch.setattr(upgrade, "run_upgrade", run_upgrade)
    return seen


def test_non_root_is_refused_before_any_work(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], recorded
) -> None:
    monkeypatch.setattr(install_cli.os, "geteuid", lambda: 1000)

    assert install_cli.main(["upgrade", "0.1.13"]) == 1
    assert install_cli.upgrade_main(["0.1.13"]) == 1

    assert recorded == []
    assert capsys.readouterr().err.count("requires root") == 2


def test_test_only_flags_are_refused_without_the_qualification_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], recorded
) -> None:
    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    monkeypatch.delenv("PSC_UPGRADE_QUALIFICATION", raising=False)

    for flags in (
        ["--release-source", "/run/release"],
        ["--allow-same-version"],
        ["--prior-receipt", "/run/prior.json"],
    ):
        assert install_cli.upgrade_main(["0.1.13", *flags]) == 1

    assert recorded == []
    assert capsys.readouterr().err.count("PSC_UPGRADE_QUALIFICATION=1") == 3


def test_qualification_options_reach_the_coordinator(
    monkeypatch: pytest.MonkeyPatch, recorded
) -> None:
    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    monkeypatch.setenv("PSC_UPGRADE_QUALIFICATION", "1")

    assert install_cli.main(
        [
            "upgrade",
            "0.1.13",
            "--release-source",
            "/run/release",
            "--allow-same-version",
            "--prior-receipt",
            "/run/prior.json",
            "--wait-idle",
            "30",
            "--json",
        ]
    ) == 0

    options = recorded[0]
    assert options.version == "0.1.13"
    assert options.wait_idle == 30
    assert options.json_output is True
    assert str(options.release_source) == "/run/release"
    assert options.allow_same_version is True
    assert str(options.prior_receipt) == "/run/prior.json"


def test_top_level_cortex_upgrade_is_an_alias(monkeypatch: pytest.MonkeyPatch, recorded) -> None:
    monkeypatch.setattr(install_cli, "_require_root", lambda: None)

    assert umbrella_cli.main(["upgrade", "--status"]) == 0
    assert recorded[0].status is True


def test_recover_and_status_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as exc:
        install_cli.upgrade_main(["--recover", "--status"])
    assert exc.value.code == 2
```

在 `tests/test_cli_help_alignment.py` 檔尾加入：

```python
def test_umbrella_help_lists_one_command_upgrade(capsys) -> None:
    assert umbrella_cli.main(["--help"]) == 0
    assert "  upgrade          以 root 一鍵升級" in capsys.readouterr().out


def test_upgrade_alias_help_uses_cortex_upgrade_and_hides_test_only_flags(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        umbrella_cli.main(["upgrade", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "usage: cortex upgrade" in output
    for flag in ("--wait-idle", "--recover", "--status", "--json"):
        assert flag in output
    for hidden in ("--release-source", "--allow-same-version", "--prior-receipt"):
        assert hidden not in output


def test_trust_root_help_lists_the_upgrade_subcommand(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        installer.main(["trust-root", "--help"])
    assert exc.value.code == 0
    assert "upgrade" in capsys.readouterr().out


def test_install_help_mentions_trust_root_upgrade(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        installer.main(["--help"])
    assert exc.value.code == 0
    # argparse wraps the help column, so compare without whitespace.
    assert "rollback/upgrade" in "".join(capsys.readouterr().out.split())
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_trust_root_upgrade_cli.py tests/test_cli_help_alignment.py -q`
Expected: FAIL。`install_cli.main(["upgrade", ...])` 以 `SystemExit(2)`（`invalid choice: 'upgrade'`）結束；`AttributeError: module ... has no attribute 'upgrade_main'`；`_HELP` 與 installer help 的斷言不成立。

- [ ] **Step 3: 最小實作**

`paulsha_cortex/trust_root/install/cli.py`：在 `_build_parser` 之前新增：

```python
def _add_upgrade_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "version", nargs="?", help="target release MAJOR.MINOR.PATCH (tag v<version>)"
    )
    parser.add_argument(
        "--wait-idle",
        type=int,
        default=0,
        metavar="SECONDS",
        help="wait up to SECONDS for in-flight jobs to finish (default 0: refuse at once)",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="print the upgrade report or status as JSON",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--recover",
        action="store_true",
        help="recover an upgrade interrupted by SIGKILL, OOM or power loss (runbook §6)",
    )
    mode.add_argument(
        "--status",
        action="store_true",
        help="read-only: effective receipt, last upgrade result, loaded runtime match",
    )
    # RC qualification only: accepted when PSC_UPGRADE_QUALIFICATION=1, hidden from help.
    parser.add_argument("--release-source", help=argparse.SUPPRESS)
    parser.add_argument("--allow-same-version", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--prior-receipt", help=argparse.SUPPRESS)
```

在 `_build_parser` 的 `return parser`（L773）之前加：

```python
    upgrade_parser = sub.add_parser(
        "upgrade", help="one-command upgrade to a published release (root only)"
    )
    _add_upgrade_arguments(upgrade_parser)
```

在 `_rollback_command` 之後新增：

```python
def _upgrade_command(args: argparse.Namespace) -> int:
    _require_root()
    # upgrade.py imports this module, so it is imported here -- still before any
    # host mutation.  upgrade.py itself imports everything at module level.
    from . import upgrade

    options = upgrade.options_from_args(args, environ=os.environ)
    return upgrade.run_upgrade(options)


def upgrade_main(argv: Sequence[str] | None = None) -> int:
    """`cortex upgrade`: alias of `cortex install trust-root upgrade`."""

    parser = argparse.ArgumentParser(
        prog="cortex upgrade",
        description=(
            "Upgrade this host to a published release in one root command "
            "(alias of `cortex install trust-root upgrade`)."
        ),
    )
    _add_upgrade_arguments(parser)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return _upgrade_command(args)
    except (InstallError, PermissionError, OSError, ValueError) as exc:
        sys.stderr.write(f"cortex upgrade failed: {exc}\n")
        return 1
```

在 `main` 的 `if args.trust_root_command == "legacy":`（L1455）之前加：

```python
        if args.trust_root_command == "upgrade":
            return _upgrade_command(args)
```

`paulsha_cortex/cli.py`：`_HELP` 的 `  install service  安裝 manager service/timer 與 monitor 的 systemd --user units` 下一行加：

```text
  upgrade          以 root 一鍵升級到指定 release（--status 唯讀、--recover 收拾中斷）
```

並在 `main` 的 `install` 分派（L146-149）之後加：

```python
    if args[0] == "upgrade":
        from paulsha_cortex.trust_root.install.cli import upgrade_main

        return int(upgrade_main(args[1:]) or 0)
```

`paulsha_cortex/deploy/installer.py`：把

```python
        help="Phase 2: plan/apply/credentials/activate/verify/rollback",
```

換成

```python
        help="Phase 2: plan/apply/credentials/activate/verify/rollback/upgrade",
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_trust_root_upgrade_cli.py tests/test_cli_help_alignment.py tests/test_trust_root_install_plan.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add paulsha_cortex/trust_root/install/cli.py paulsha_cortex/cli.py paulsha_cortex/deploy/installer.py tests/test_trust_root_upgrade_cli.py tests/test_cli_help_alignment.py
git commit -m "feat(cli): 新增 cortex upgrade 與 cortex install trust-root upgrade（#1263）"
```

---

### Task 12: RC qualification：以 `cortex upgrade` 取代升級演練，新增 one-command-upgrade evidence

**Files:**
- Modify: `qualification/run.sh:132-134`（刪除 `rollback_*_path` 三個變數）、`run.sh:395-427`（升級演練整段換掉）、`run.sh:435-438`（driver profile 參數）、`run.sh:457-459`（driver 的 `--receipt`／`--install-evidence`）
- Modify: `qualification/Dockerfile`（COPY 與 chmod）
- Modify: `qualification/driver.py`（argparse、`main` 的 evidence 段、tests 清單；刪除 `_capture_rollback_loaded_runtime`；新增 `_capture_one_command_upgrade`）
- Modify: `qualification/validate.py:73-79`（`REQUIRED_RELEASE_ARTIFACTS`）、`validate.py:85-103`（`REQUIRED_RELEASE_TESTS`）、`validate.py:197-205` 之後（新 helper）、`validate.py:1338-1370`（rollback 比對改用 helper 並驗新 evidence）
- Test: `tests/test_phase2_qualification.py:219`（`_valid_full_qualification`）、`:244`、`:428`、`:698-716`；`tests/test_qualification_driver_service_status.py`；`tests/test_requirement_delivery.py:1407`、`:1541`；`tests/test_requirement_delivery_cli.py:381`、`:515`

**Interfaces:**
- Consumes: Task 4 的 driver 別名 `_rollback_loaded_runtime_mismatch`、`_rollback_runtime_expected`；Task 5 `qualification/release_source.py`；Task 9／10 的 upgrade report（`result`、`failed_step`、`receipt.path`、`receipt.receipt_id`、`plan.sha256`、`verify_evidence`、`rollback.restore_safe`、`rollback.prior_loaded_runtime.mismatch`／`service_status`）；Task 11 的 `/opt/cortex/venv/bin/cortex upgrade`。
- Produces: driver 參數 `--prior-receipt`、`--upgrade-drill-report`、`--upgrade-report`（三者同時出現），取代 `--rollback-receipt`；`_capture_one_command_upgrade(*, prior_receipt, upgrade_receipt, receipt_path, drill_report, upgrade_report, evidence_dir) -> None` 寫出 `evidence/rollback-loaded-runtime-status.json`（schema 不變）與 `evidence/one-command-upgrade.json`（`schema_version: 1`、`scenario: "one-command-upgrade-same-artifact"`、`drill`、`upgrade`、`expected`、`service_status`）；validate 的 release／canary 必要 test 多 `one-command-upgrade`、必要 artifact 多 `evidence/one-command-upgrade.json`。

- [ ] **Step 1: 寫會失敗的測試**

`tests/test_phase2_qualification.py`：

1. 把 `_valid_full_qualification` 的簽名（L219）改成：

```python
def _valid_full_qualification(
    tmp_path: Path,
    *,
    rollback_expected_wheel: str = "b" * 64,
    upgrade_document: dict | None = None,
) -> dict:
```

2. 在它的 tests tuple 中 `"rollback-loaded-runtime",`（L244）之後加一行 `"one-command-upgrade",`。
3. 在 `documents = {...}` 最後一個 entry `"rollback-loaded-runtime-status.json": {...},` 之後、`}` 之前加：

```python
        "one-command-upgrade.json": (
            upgrade_document
            if upgrade_document is not None
            else _one_command_upgrade_document()
        ),
```

4. 在 `_valid_full_qualification` 之前新增：

```python
def _one_command_upgrade_document(
    *,
    drill_result: str = "rolled-back",
    inherited_from: str = "prior",
    loaded_receipt_id: str = "upgraded",
) -> dict:
    wheel, commit = "b" * 64, "a" * 40
    return {
        "schema_version": 1,
        "scenario": "one-command-upgrade-same-artifact",
        "drill": {
            "result": drill_result,
            "failed_step": "credentials",
            "restore_safe": True,
            "receipt_id": "rollback",
            "parent_receipt_id": "prior",
        },
        "upgrade": {
            "result": "upgraded",
            "receipt_id": "upgraded",
            "parent_receipt_id": "prior",
            "plan_sha256": "e" * 64,
            "inherited_credentials": [
                {"principal": "builder", "provider": "codex", "inherited_from": inherited_from}
            ],
        },
        "expected": {"receipt_id": "upgraded", "wheel_sha256": wheel, "candidate_commit": commit},
        "service_status": {
            "service": {
                "loaded_runtime": {
                    name: {
                        "comparison": {
                            "artifact_status": "match",
                            "config_status": "match",
                            "process_status": "match",
                            "loaded_wheel_sha256": wheel,
                        },
                        "trust_root": {
                            "status": "verified",
                            "receipt_id": loaded_receipt_id,
                            "wheel_sha256": wheel,
                            "candidate_commit": commit,
                        },
                        "installed_artifact": {"wheel_sha256": wheel},
                    }
                    for name in ("manager", "monitor")
                }
            }
        },
    }
```

5. 把 L709-716 的 `test_release_harness_checks_loaded_runtime_after_qualified_upgrade_rollback` 整個換成：

```python
def test_release_harness_drives_one_command_upgrade_drill_then_full_upgrade() -> None:
    runner = _required_text(RUNNER)
    assert "cortex install trust-root rollback \\" not in runner
    assert "rollback_overlay_path" not in runner
    source = runner.index("/usr/local/libexec/cortex-qualification-release-source")
    overlay = runner.index("/var/lib/cortex-installer/host-overlay.yaml")
    drift = runner.index('chmod 0640 "$builder_codex_credential"')
    drill = runner.index('sh "$upgrade_drill_report" "${upgrade_cli[@]}"')
    restore = runner.index('chmod 0600 "$builder_codex_credential"')
    full = runner.index('sh "$upgrade_report" "${upgrade_cli[@]}"')
    driver = runner.index("driver_profile_args+=(\n    --prior-receipt")
    assert source < overlay < drift < drill < restore < full < driver
    assert runner.count("--env PSC_UPGRADE_QUALIFICATION=1") == 2
    for fragment in (
        '/opt/cortex/venv/bin/cortex upgrade "$upgrade_version"',
        '--release-source "$upgrade_release_source"',
        "--allow-same-version",
        '--prior-receipt "$receipt_path"',
        '--upgrade-drill-report "$upgrade_drill_report"',
        '--upgrade-report "$upgrade_report"',
        '--receipt "$upgrade_receipt_path"',
        '--install-evidence "$upgrade_evidence_path"',
    ):
        assert fragment in runner
    assert '"--install-receipt"' in _required_text(DRIVER)


def test_reference_image_ships_the_release_source_helper() -> None:
    raw = _required_text(DOCKERFILE)
    assert (
        "COPY release_source.py /usr/local/libexec/cortex-qualification-release-source"
        in raw
    )
    assert "/usr/local/libexec/cortex-qualification-release-source" in raw.split(
        "RUN chmod", 1
    )[1]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"drill_result": "halted"}, "drill did not return to the qualified prior receipt"),
        ({"inherited_from": "another"}, "did not inherit the prior receipt's credentials"),
        (
            {"loaded_receipt_id": "prior"},
            "one-command upgrade loaded-runtime artifact or receipt does not match",
        ),
    ],
)
def test_full_suite_validator_rejects_broken_one_command_upgrade_evidence(
    tmp_path: Path, changes, message
) -> None:
    payload = _valid_full_qualification(
        tmp_path, upgrade_document=_one_command_upgrade_document(**changes)
    )

    completed = _run_full_validator(tmp_path, payload)

    assert completed.returncode != 0, completed.stdout + completed.stderr
    assert message in completed.stderr
```

`tests/test_requirement_delivery.py` 的 `_full_canary_qualification`（L1355 起）與 `tests/test_requirement_delivery_cli.py` 的 `_cli_full_canary_qualification`（L335 起）各做兩處相同修改：tests tuple 的 `"rollback-loaded-runtime",`（分別在 L1407、L381）之後加 `"one-command-upgrade",`；`documents` 最後一個 entry `"rollback-loaded-runtime-status.json": {...},`（分別在 L1541、L515 起）之後加：

```python
        # #1263：`cortex upgrade` 的 activate 前失敗演練與完整升級，都綁回同一個 prior。
        "one-command-upgrade.json": {
            "schema_version": 1,
            "scenario": "one-command-upgrade-same-artifact",
            "drill": {
                "result": "rolled-back", "failed_step": "credentials", "restore_safe": True,
                "receipt_id": "rollback", "parent_receipt_id": "prior",
            },
            "upgrade": {
                "result": "upgraded", "receipt_id": "upgraded", "parent_receipt_id": "prior",
                "plan_sha256": "e" * 64,
                "inherited_credentials": [
                    {"principal": "builder", "provider": "codex", "inherited_from": "prior"},
                ],
            },
            "expected": {
                "receipt_id": "upgraded", "wheel_sha256": wheel_sha256,
                "candidate_commit": candidate_sha,
            },
            "service_status": {"service": {"loaded_runtime": {
                name: {
                    "comparison": {
                        "artifact_status": "match", "config_status": "match",
                        "process_status": "match", "loaded_wheel_sha256": wheel_sha256,
                    },
                    "trust_root": {
                        "status": "verified", "receipt_id": "upgraded",
                        "wheel_sha256": wheel_sha256, "candidate_commit": candidate_sha,
                    },
                    "installed_artifact": {"wheel_sha256": wheel_sha256},
                }
                for name in ("manager", "monitor")
            }}},
        },
```

`tests/test_qualification_driver_service_status.py` 檔尾加入：

```python
def _upgrade_inputs(
    tmp_path: Path, *, drill_result: str = "rolled-back", inherited_from: str = "prior"
):
    drill_receipt = tmp_path / "drill-receipt.json"
    parent = {
        "path": "/run/cortex-install/install-receipt.json",
        "receipt_id": "prior",
        "plan_sha256": "e" * 64,
    }
    drill_receipt.write_text(
        json.dumps({"receipt_id": "drill", "state": "rolled-back", "parent_receipt": parent}),
        encoding="utf-8",
    )
    plan = {"candidate": {"wheel_sha256": "b" * 64}, "repo_identity": {"commit": "c" * 40}}
    prior = {"receipt_id": "prior", "plan": plan}
    upgraded = {
        "receipt_id": "upgraded",
        "state": "applied",
        "qualified": True,
        "parent_receipt": parent,
        "plan": plan,
        "credentials": [
            {
                "principal": "builder",
                "provider": "codex",
                "mode": "0600",
                "sha256": "f" * 64,
                "inherited_from": inherited_from,
            }
        ],
    }
    drill = {
        "result": drill_result,
        "failed_step": "credentials",
        "receipt": {"path": str(drill_receipt), "receipt_id": "drill"},
        "rollback": {
            "restore_safe": True,
            "prior_loaded_runtime": {"mismatch": "", "service_status": _rollback_status_payload()},
        },
    }
    report = {
        "result": "upgraded",
        "receipt": {"path": "/var/lib/cortex-install-receipts/next.json", "receipt_id": "upgraded"},
        "plan": {"sha256": "d" * 64},
    }
    return prior, upgraded, drill, report


def _upgraded_status_payload():
    payload = _rollback_status_payload()
    for report in payload["service"]["loaded_runtime"].values():
        report["trust_root"]["receipt_id"] = "upgraded"
    return payload


def _capture(driver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **changes):
    prior, upgraded, drill, report = _upgrade_inputs(tmp_path, **changes)
    commands: list[tuple[str, ...]] = []

    def run(argv, **_kwargs):
        commands.append(tuple(argv))
        return driver.CommandResult(tuple(argv), 0, json.dumps(_upgraded_status_payload()), "")

    monkeypatch.setattr(driver, "_run", run)
    monkeypatch.setattr(driver, "_installed_runtime_env", dict)
    monkeypatch.setattr(driver, "SYSTEM_STATUS_SETTLE_SECONDS", 0)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    driver._capture_one_command_upgrade(
        prior_receipt=prior,
        upgrade_receipt=upgraded,
        receipt_path=Path(report["receipt"]["path"]),
        drill_report=drill,
        upgrade_report=report,
        evidence_dir=evidence,
    )
    return evidence, commands


def test_one_command_upgrade_evidence_binds_the_drill_and_the_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver()

    evidence, commands = _capture(driver, tmp_path, monkeypatch)

    rollback = json.loads((evidence / "rollback-loaded-runtime-status.json").read_text())
    assert rollback["scenario"] == "same-artifact-qualified-prior-to-candidate-rollback"
    assert rollback["rollback_receipt"] == {
        "receipt_id": "drill",
        "state": "rolled-back",
        "parent_receipt_id": "prior",
    }
    upgrade = json.loads((evidence / "one-command-upgrade.json").read_text())
    assert upgrade["scenario"] == "one-command-upgrade-same-artifact"
    assert upgrade["upgrade"]["inherited_credentials"] == [
        {"principal": "builder", "provider": "codex", "inherited_from": "prior"}
    ]
    assert upgrade["expected"]["receipt_id"] == "upgraded"
    assert commands[-1][-2:] == ("--install-receipt", "/var/lib/cortex-install-receipts/next.json")


def test_one_command_upgrade_evidence_refuses_a_drill_that_did_not_roll_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver()

    with pytest.raises(driver.QualificationFailure, match="did not roll back"):
        _capture(driver, tmp_path, monkeypatch, drill_result="halted")


def test_one_command_upgrade_evidence_refuses_credentials_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver()

    with pytest.raises(driver.QualificationFailure, match="inherited the prior credentials"):
        _capture(driver, tmp_path, monkeypatch, inherited_from="another")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_phase2_qualification.py tests/test_qualification_driver_service_status.py tests/test_requirement_delivery.py tests/test_requirement_delivery_cli.py -q -k "one_command or harness_drives or release_source_helper or full_suite or canary_qualification or delivery_gaps"`
Expected: FAIL。`test_release_harness_drives_one_command_upgrade_drill_then_full_upgrade` 與 `test_reference_image_ships_the_release_source_helper` 找不到字串（`ValueError: substring not found` 或 assert 失敗）；driver 測試 `AttributeError: module ... has no attribute '_capture_one_command_upgrade'`；三個 `rejects_broken_one_command_upgrade_evidence` 案例因 validator 尚未檢查而 `returncode == 0`。

- [ ] **Step 3: 最小實作**

`qualification/Dockerfile`：在 `COPY verify_bundle.py /usr/local/libexec/cortex-qualification-verify-bundle` 之後加：

```dockerfile
COPY release_source.py /usr/local/libexec/cortex-qualification-release-source
```

並在 `RUN chmod 0755 \` 清單的 `/usr/local/libexec/cortex-qualification-verify-bundle \` 之後加一行 `        /usr/local/libexec/cortex-qualification-release-source \`。

`qualification/run.sh`：

1. 刪除 L132-134：

```bash
rollback_receipt_path=/run/cortex-install/rollback-install-receipt.json
rollback_plan_path=/run/cortex-install/rollback-install-plan.json
rollback_overlay_path=/run/cortex-install/rollback-host-overlay.json
```

2. 把 L395-427（從 `# 上方 fresh-install rollback 會刻意回到沒有服務的主機。` 到 `docker exec "$container_name" systemctl start "${rollback_services[@]}"`）整段換成：

```bash
# 一鍵升級演練（#1263）：上方已 qualified 的 receipt 是 prior，同一容器以已安裝的
# `/opt/cortex/venv/bin/cortex upgrade` 走完整流程。release profile 沒有網路，release
# 來源改用容器內與 GitHub REST 同形狀的本機目錄；同一 candidate 的 plan 只能靠
# host overlay digest 不同才成為新的 transaction，因此 overlay 重述 release 設定既有
# 的 `providers.builder`（有效設定不變）。三個僅限測試的參數只有在
# PSC_UPGRADE_QUALIFICATION=1 時才被接受。
upgrade_version=$(basename "$wheel_path")
upgrade_version=${upgrade_version#paulsha_cortex-}
upgrade_version=${upgrade_version%-py3-none-any.whl}
[[ "$upgrade_version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || \
    die "candidate wheel carries no MAJOR.MINOR.PATCH release version"
upgrade_release_source=/run/cortex-upgrade-release
upgrade_drill_report=/run/cortex-install/upgrade-drill-report.json
upgrade_report=/run/cortex-install/upgrade-report.json
builder_codex_credential=/var/lib/cortex-builder/.codex/auth.json
docker exec "$container_name" /usr/local/libexec/cortex-qualification-release-source \
    --artifacts /artifacts \
    --output "$upgrade_release_source" \
    --version "$upgrade_version" \
    --candidate-sha "$candidate_sha" \
    --wheel-sha256 "$expected_wheel_sha" \
    --bundle-sha256 "$expected_bundle_sha"
docker exec "$container_name" python3 -c '
import json, sys, yaml
config = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
overlay = {"providers": {"builder": list(config["providers"]["builder"])}}
open(sys.argv[2], "w", encoding="utf-8").write(json.dumps(overlay) + "\n")
' /artifacts/install-config.yaml /run/cortex-install/upgrade-host-overlay.json
docker exec "$container_name" install -o root -g root -m 0644 \
    /run/cortex-install/upgrade-host-overlay.json /var/lib/cortex-installer/host-overlay.yaml
upgrade_cli=(
    /opt/cortex/venv/bin/cortex upgrade "$upgrade_version"
    --release-source "$upgrade_release_source"
    --allow-same-version
    --prior-receipt "$receipt_path"
    --wait-idle 120
    --json
)
# 剛恢復的 Manager 可能正在改寫 durable jobs registry；`--wait-idle` 讓前置檢查重試而不是
# 立刻拒絕（讀不到 registry 一律視為忙碌）。
# activate 前注入失敗：prior 已記錄的 builder/codex 落點檔權限偏離 0600，credential
# handoff 拒絕繼承 → 工具自動 rollback 新 receipt，並把原本 active 的服務啟動回來。
docker exec "$container_name" chmod 0640 "$builder_codex_credential"
if docker exec --env PSC_UPGRADE_QUALIFICATION=1 "$container_name" \
    sh -eu -c 'out=$1; shift; "$@" >"$out"' sh "$upgrade_drill_report" "${upgrade_cli[@]}"; then
    die "one-command upgrade accepted a drifted inherited credential"
fi
docker exec "$container_name" chmod 0600 "$builder_codex_credential"
docker exec "$container_name" cat "$upgrade_drill_report"
docker exec "$container_name" jq -e \
    '.result == "rolled-back" and .failed_step == "credentials"
     and .rollback.restore_safe == true
     and .rollback.prior_loaded_runtime.mismatch == ""' \
    "$upgrade_drill_report" >/dev/null || \
    die "pre-activate upgrade failure did not return to the prior receipt"
for upgrade_service in cortex-egress-proxy.service cortex-manager.service cortex-monitor.service; do
    docker exec "$container_name" systemctl is-active --quiet "$upgrade_service" || \
        die "upgrade drill did not restore $upgrade_service"
done
# 完整升級：apply → 繼承 prior 憑證 → activate → verify → loaded↔installed 一致。
docker exec --env PSC_UPGRADE_QUALIFICATION=1 "$container_name" \
    sh -eu -c 'out=$1; shift; "$@" >"$out"' sh "$upgrade_report" "${upgrade_cli[@]}"
docker exec "$container_name" jq -e '.result == "upgraded"' "$upgrade_report" >/dev/null || \
    die "one-command upgrade did not complete"
upgrade_receipt_path=$(docker exec "$container_name" jq -r '.receipt.path' "$upgrade_report")
upgrade_evidence_path=$(docker exec "$container_name" jq -r '.verify_evidence' "$upgrade_report")
[[ "$upgrade_receipt_path" == /* && "$upgrade_evidence_path" == /* ]] || \
    die "one-command upgrade report lacks the new receipt or verify evidence"
```

3. 把 L435-438：

```bash
driver_profile_args+=(
    --rollback-receipt "$rollback_receipt_path"
    --prior-receipt "$receipt_path"
)
```

換成：

```bash
driver_profile_args+=(
    --prior-receipt "$receipt_path"
    --upgrade-drill-report "$upgrade_drill_report"
    --upgrade-report "$upgrade_report"
)
```

4. 把 driver 呼叫（L457-459）中的：

```bash
    --receipt "$receipt_path" \
    --install-evidence "$install_evidence_path" \
```

換成：

```bash
    --receipt "$upgrade_receipt_path" \
    --install-evidence "$upgrade_evidence_path" \
```

`qualification/driver.py`：

1. argparse：把

```python
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--rollback-receipt", type=Path)
    parser.add_argument("--prior-receipt", type=Path)
```

換成

```python
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--prior-receipt", type=Path)
    parser.add_argument("--upgrade-drill-report", type=Path)
    parser.add_argument("--upgrade-report", type=Path)
```

2. 把

```python
    if (args.rollback_receipt is None) != (args.prior_receipt is None):
        parser.error("rollback loaded-runtime evidence requires both receipt paths")
```

換成

```python
    upgrade_evidence = (args.prior_receipt, args.upgrade_drill_report, args.upgrade_report)
    if any(value is not None for value in upgrade_evidence) and not all(
        value is not None for value in upgrade_evidence
    ):
        parser.error(
            "one-command upgrade evidence requires --prior-receipt, "
            "--upgrade-drill-report and --upgrade-report together"
        )
```

3. 把 `main` 的 try 區塊中

```python
        if args.rollback_receipt is not None and args.prior_receipt is not None:
            prior_receipt = _load_json(args.prior_receipt, "prior install receipt")
            rollback_receipt = _load_json(args.rollback_receipt, "rollback install receipt")
            _capture_rollback_loaded_runtime(
                rollback_receipt=rollback_receipt,
                prior_receipt=prior_receipt,
                receipt_path=args.prior_receipt,
                evidence_dir=args.evidence_dir,
            )
```

換成

```python
        if args.upgrade_report is not None:
            assert args.prior_receipt is not None and args.upgrade_drill_report is not None
            _capture_one_command_upgrade(
                prior_receipt=_load_json(args.prior_receipt, "prior install receipt"),
                upgrade_receipt=receipt,
                receipt_path=args.receipt,
                drill_report=_load_json(args.upgrade_drill_report, "upgrade drill report"),
                upgrade_report=_load_json(args.upgrade_report, "upgrade report"),
                evidence_dir=args.evidence_dir,
            )
```

4. 把

```python
            if args.rollback_receipt is not None:
                tests.append({"name": "rollback-loaded-runtime", "status": "passed"})
```

換成

```python
            if args.upgrade_report is not None:
                tests += [
                    {"name": "rollback-loaded-runtime", "status": "passed"},
                    {"name": "one-command-upgrade", "status": "passed"},
                ]
```

5. 刪除整個 `def _capture_rollback_loaded_runtime(...)`（Task 4 之後位於 `SYSTEM_STATUS_SETTLE_SECONDS` 與 `_system_status_mismatch` 之後、`OWNER_RECLAIM_MANAGER` 註解之前），在原處新增：

```python
def _capture_one_command_upgrade(
    *,
    prior_receipt: Mapping[str, object],
    upgrade_receipt: Mapping[str, object],
    receipt_path: Path,
    drill_report: Mapping[str, object],
    upgrade_report: Mapping[str, object],
    evidence_dir: Path,
) -> None:
    """#1263：`cortex upgrade` 的 activate 前失敗演練與完整升級都綁回同一個 prior。"""

    prior_id = prior_receipt.get("receipt_id")
    drill_rollback = drill_report.get("rollback")
    drill_receipt = drill_report.get("receipt")
    if (
        drill_report.get("result") != "rolled-back"
        or drill_report.get("failed_step") != "credentials"
        or not isinstance(drill_rollback, Mapping)
        or drill_rollback.get("restore_safe") is not True
        or not isinstance(drill_receipt, Mapping)
        or not isinstance(drill_receipt.get("path"), str)
    ):
        raise QualificationFailure(
            "one-command upgrade drill did not roll back to the prior receipt: "
            f"result={_diagnostic_token(drill_report.get('result'))} "
            f"failed_step={_diagnostic_token(drill_report.get('failed_step'))}"
        )
    rolled_back = _load_json(Path(str(drill_receipt["path"])), "upgrade drill receipt")
    parent = rolled_back.get("parent_receipt")
    if (
        rolled_back.get("state") != "rolled-back"
        or not isinstance(parent, Mapping)
        or parent.get("receipt_id") != prior_id
    ):
        raise QualificationFailure(
            "upgrade drill receipt is not a rolled-back successor of the prior receipt"
        )
    prior_runtime = drill_rollback.get("prior_loaded_runtime")
    drill_status = (
        prior_runtime.get("service_status") if isinstance(prior_runtime, Mapping) else None
    )
    expected_prior = _rollback_runtime_expected(prior_receipt)
    _write_json(
        evidence_dir / "rollback-loaded-runtime-status.json",
        {
            "schema_version": 1,
            "scenario": "same-artifact-qualified-prior-to-candidate-rollback",
            "rollback_receipt": {
                "receipt_id": rolled_back.get("receipt_id"),
                "state": rolled_back.get("state"),
                "parent_receipt_id": parent.get("receipt_id"),
            },
            "expected": expected_prior,
            "service_status": drill_status,
        },
    )
    mismatch = _rollback_loaded_runtime_mismatch(drill_status, expected_prior)
    if mismatch:
        raise QualificationFailure(
            "upgrade drill did not restore the prior loaded runtime: " + mismatch
        )
    upgraded_parent = upgrade_receipt.get("parent_receipt")
    credentials = upgrade_receipt.get("credentials")
    report_receipt = upgrade_report.get("receipt")
    if (
        upgrade_report.get("result") != "upgraded"
        or not isinstance(report_receipt, Mapping)
        or report_receipt.get("path") != str(receipt_path)
        or upgrade_receipt.get("state") != "applied"
        or upgrade_receipt.get("qualified") is not True
        or not isinstance(upgraded_parent, Mapping)
        or upgraded_parent.get("receipt_id") != prior_id
        or not isinstance(credentials, list)
        or not credentials
        or any(
            not isinstance(row, Mapping) or row.get("inherited_from") != prior_id
            for row in credentials
        )
    ):
        raise QualificationFailure(
            "one-command upgrade receipt is not a qualified successor that "
            "inherited the prior credentials"
        )
    expected = _rollback_runtime_expected(upgrade_receipt)
    # activate 之後 loaded receipt 可能晚幾秒寫入：與 `_installed_checks` 相同，
    # 輪詢到比對一致或逾時，逾時以最後一次結果判定。
    deadline = time.monotonic() + SYSTEM_STATUS_SETTLE_SECONDS
    while True:
        result = _run(
            (
                "/opt/cortex/venv/bin/cortex",
                "service",
                "status",
                "--system",
                "--json",
                "--install-receipt",
                str(receipt_path),
            ),
            env=_installed_runtime_env(),
        )
        if result.returncode != 0:
            raise QualificationFailure("upgrade-system-status=unavailable")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise QualificationFailure(
                "upgrade system-scope status returned invalid JSON"
            ) from exc
        if not _rollback_loaded_runtime_mismatch(payload, expected) or (
            time.monotonic() >= deadline
        ):
            break
        time.sleep(2)
    plan = upgrade_report.get("plan")
    _write_json(
        evidence_dir / "one-command-upgrade.json",
        {
            "schema_version": 1,
            "scenario": "one-command-upgrade-same-artifact",
            "drill": {
                "result": drill_report.get("result"),
                "failed_step": drill_report.get("failed_step"),
                "restore_safe": drill_rollback.get("restore_safe"),
                "receipt_id": rolled_back.get("receipt_id"),
                "parent_receipt_id": parent.get("receipt_id"),
            },
            "upgrade": {
                "result": upgrade_report.get("result"),
                "receipt_id": upgrade_receipt.get("receipt_id"),
                "parent_receipt_id": upgraded_parent.get("receipt_id"),
                "plan_sha256": plan.get("sha256") if isinstance(plan, Mapping) else None,
                "inherited_credentials": [
                    {
                        "principal": row.get("principal"),
                        "provider": row.get("provider"),
                        "inherited_from": row.get("inherited_from"),
                    }
                    for row in credentials
                ],
            },
            "expected": expected,
            "service_status": payload,
        },
    )
    mismatch = _rollback_loaded_runtime_mismatch(payload, expected)
    if mismatch:
        raise QualificationFailure(
            "upgraded loaded runtime did not match the new receipt: " + mismatch
        )
```

`qualification/validate.py`：

1. `REQUIRED_RELEASE_ARTIFACTS`（L73-79）在 `"evidence/rollback-loaded-runtime-status.json",` 之後加 `"evidence/one-command-upgrade.json",`。
2. `REQUIRED_RELEASE_TESTS`（L85-103）在 `"rollback-loaded-runtime",` 之後加 `"one-command-upgrade",`（`REQUIRED_CANARY_TESTS` 由它導出，自動涵蓋 canary）。
3. 在 `_artifact_json`（L197-205）之後新增：

```python
def _require_loaded_runtime_match(
    service_status: Any, expected: dict[str, Any], *, label: str
) -> None:
    service = service_status.get("service") if isinstance(service_status, dict) else None
    loaded_runtime = service.get("loaded_runtime") if isinstance(service, dict) else None
    if not isinstance(loaded_runtime, dict):
        _fail(f"{label} loaded-runtime status is unknown")
    for service_name in ("manager", "monitor"):
        report = loaded_runtime.get(service_name)
        comparison = report.get("comparison") if isinstance(report, dict) else None
        trust_root = report.get("trust_root") if isinstance(report, dict) else None
        installed = report.get("installed_artifact") if isinstance(report, dict) else None
        if not all(isinstance(row, dict) for row in (comparison, trust_root, installed)):
            _fail(f"{label} loaded-runtime report is unknown")
        if (
            any(
                comparison.get(key) != "match"
                for key in ("artifact_status", "config_status", "process_status")
            )
            or trust_root.get("status") != "verified"
            or trust_root.get("receipt_id") != expected["receipt_id"]
            or trust_root.get("wheel_sha256") != expected["wheel_sha256"]
            or trust_root.get("candidate_commit") != expected["candidate_commit"]
            or comparison.get("loaded_wheel_sha256") != expected["wheel_sha256"]
            or installed.get("wheel_sha256") != expected["wheel_sha256"]
        ):
            _fail(f"{label} loaded-runtime artifact or receipt does not match")


def _validate_one_command_upgrade(
    evidence_root: Path,
    *,
    prior_receipt_id: Any,
    drill_receipt_id: Any,
    evidence_sha: str,
    evidence_wheel_sha: str,
) -> None:
    """#1263：activate 前失敗演練與完整升級都必須綁回同一個 qualified prior。"""

    status = _artifact_json(evidence_root, "evidence/one-command-upgrade.json")
    if (
        status.get("schema_version") != 1
        or status.get("scenario") != "one-command-upgrade-same-artifact"
    ):
        _fail("one-command upgrade evidence scenario is unknown")
    drill = _required_fields(
        status.get("drill"),
        "one-command upgrade drill",
        {"result", "failed_step", "restore_safe", "receipt_id", "parent_receipt_id"},
    )
    if (
        drill["result"] != "rolled-back"
        or drill["failed_step"] != "credentials"
        or drill["restore_safe"] is not True
        or drill["parent_receipt_id"] != prior_receipt_id
        or drill["receipt_id"] != drill_receipt_id
    ):
        _fail("one-command upgrade drill did not return to the qualified prior receipt")
    upgraded = _required_fields(
        status.get("upgrade"),
        "one-command upgrade result",
        {"result", "receipt_id", "parent_receipt_id", "plan_sha256", "inherited_credentials"},
    )
    if (
        upgraded["result"] != "upgraded"
        or upgraded["parent_receipt_id"] != prior_receipt_id
        or upgraded["receipt_id"] in {prior_receipt_id, drill_receipt_id}
        or not isinstance(upgraded["plan_sha256"], str)
        or SHA256.fullmatch(upgraded["plan_sha256"]) is None
    ):
        _fail("one-command upgrade receipt is not a new successor of the qualified prior")
    inherited = upgraded["inherited_credentials"]
    if (
        not isinstance(inherited, list)
        or not inherited
        or any(
            not isinstance(row, dict) or row.get("inherited_from") != prior_receipt_id
            for row in inherited
        )
    ):
        _fail("one-command upgrade did not inherit the prior receipt's credentials")
    expected = _required_fields(
        status.get("expected"),
        "one-command upgrade expected receipt",
        {"receipt_id", "wheel_sha256", "candidate_commit"},
    )
    if (
        expected["receipt_id"] != upgraded["receipt_id"]
        or expected["wheel_sha256"] != evidence_wheel_sha
        or expected["candidate_commit"] != evidence_sha
    ):
        _fail("one-command upgrade expected receipt is not this candidate")
    _require_loaded_runtime_match(
        status.get("service_status"), expected, label="one-command upgrade"
    )
```

4. 把 L1338-1370（從 `service_status = rollback_status.get("service_status")` 到 `_fail("rollback loaded-runtime artifact or receipt does not match")`）整段換成：

```python
            _require_loaded_runtime_match(
                rollback_status.get("service_status"), expected, label="rollback"
            )
            _validate_one_command_upgrade(
                evidence_root,
                prior_receipt_id=expected["receipt_id"],
                drill_receipt_id=rollback_receipt["receipt_id"],
                evidence_sha=evidence_sha,
                evidence_wheel_sha=evidence_wheel_sha,
            )
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_phase2_qualification.py tests/test_qualification_driver_service_status.py tests/test_requirement_delivery.py tests/test_requirement_delivery_cli.py tests/test_qualification_driver_hardening.py tests/test_release_pipeline_workflows.py tests/test_qualification_legacy_profile.py -q && bash -n qualification/run.sh`
Expected: 全部 PASS，`bash -n` 無輸出。

- [ ] **Step 5: Commit**

```bash
git add qualification/run.sh qualification/Dockerfile qualification/driver.py qualification/validate.py tests/test_phase2_qualification.py tests/test_qualification_driver_service_status.py tests/test_requirement_delivery.py tests/test_requirement_delivery_cli.py
git commit -m "test(qualification): RC 以 cortex upgrade 演練 activate 前失敗回復與完整升級（#1263）"
```

---

### Task 13: 文件、changelog 與全套驗證

**Files:**
- Modify: `docs/superpowers/runbooks/trust-root-transactional-install.md:16-19`（在 `## 邊界` 之前插入「一般升級」）
- Modify: `docs/superpowers/runbooks/trust-root-legacy-adoption.md`（檔尾補一段）
- Create: `changelog.d/one-command-upgrade.md`
- Modify: `CHANGELOG.md:8-10`（`## [Unreleased]` → `### Added` 第一行）
- Test: `tests/test_phase2_closeout_runbook.py`

**Interfaces:**
- Consumes: Task 11 的指令名稱 `cortex upgrade`、`--recover`、`--status`、`--wait-idle`；Task 9 的 report 路徑 `/var/lib/cortex-installer/<版本>/upgrade-report.json`。
- Produces: runbook 開頭的「一般升級」段落（`tests/test_phase2_closeout_runbook.py` 既有斷言全部維持成立：不出現 `sudo cortex install trust-root`、`/usr/bin/python3`、`sudo test`／`sudo systemctl` 等字串，不改變任何 `index()` 先後關係，`--maintenance-token "$cortex_maintenance_token"` 出現次數仍為 9、`cortex_acquire_maintenance_lease` 仍為 2）。

- [ ] **Step 1: 寫會失敗的測試**

在 `tests/test_phase2_closeout_runbook.py` 檔尾加入：

```python
def test_current_runbook_opens_with_the_one_command_upgrade() -> None:
    current = (ROOT / CURRENT).read_text(encoding="utf-8")

    upgrade = current.index("## 一般升級")
    boundary = current.index("## 邊界")
    assert upgrade < boundary < current.index("## 1. 封存唯一 candidate CLI")
    section = current[upgrade:boundary]
    assert "sudo /opt/cortex/venv/bin/cortex upgrade <版本>" in section
    assert "cortex upgrade --recover" in section
    assert "cortex upgrade --status" in section
    assert "--wait-idle" in section
    assert "/var/lib/cortex-installer/<版本>/upgrade-report.json" in section
    assert "首次安裝與手動操作參考" in section


def test_legacy_adoption_runbook_hands_later_upgrades_to_cortex_upgrade() -> None:
    legacy = (
        ROOT / "docs/superpowers/runbooks/trust-root-legacy-adoption.md"
    ).read_text(encoding="utf-8")

    tail = legacy.rstrip().rsplit("\n\n", 1)[1]
    assert "cortex upgrade" in tail
    assert "legacy_adoption" in tail
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_phase2_closeout_runbook.py -q`
Expected: 2 failed（`ValueError: substring not found` 與 `assert 'cortex upgrade' in tail`），其餘既有測試 PASS。

- [ ] **Step 3: 最小實作**

`docs/superpowers/runbooks/trust-root-transactional-install.md`：在 L17（`診斷與決策脈絡，不得再照其中的 ...` 那一段）之後、L19 `## 邊界` 之前插入：

```markdown
## 一般升級：`cortex upgrade`

主機已有 installer 寫入、狀態為 applied＋qualified 的 receipt 時，升級只需要一個 root 指令：

    sudo /opt/cortex/venv/bin/cortex upgrade <版本>

工具依序執行下列 §1–§5：以 GitHub Releases REST metadata 取得 annotated tag 的 commit target
與三個 asset digest，驗過 qualification manifest、archive topology 與 bundle 每一個檔案後，
在新的 `/var/lib/cortex-installer/<版本>/attempt-*` 目錄封存 candidate CLI；以非 root 身分產生
plan 並自行綁定 plan sha、發布 durable plan；取得 maintenance lease、記下並停止服務；以
`--prior-receipt` apply；沿用 prior receipt 已記錄的 credentials（`credentials inherit`，
不讀憑證內容）；activate、verify，最後核對 loaded runtime 與新 receipt 一致。生效中的
receipt 由 receipt chain 判定，不看檔名或時間。結果寫在
`/var/lib/cortex-installer/<版本>/upgrade-report.json`，終端機印出摘要（`--json` 改印完整報告）。

- 只升不降：目標版本必須高於生效中的 receipt；降版改用 installer `rollback` 或下列手動流程。
- 有在飛 job 時預設直接拒絕；`--wait-idle <秒>` 會等到 idle 或逾時。
- activate 之前的失敗（ingress、plan、apply、credential handoff）會自動 rollback 並恢復原本
  active 的服務。activate 之後的失敗若 rollback 回報 `restore_safe=false`，服務維持停止、
  snapshot 保留，報告列出保留的 unknown state／drift；處理後執行
  `sudo /opt/cortex/venv/bin/cortex upgrade --recover`，或依 §6 由 operator 裁決。
- 新 plan 若要求 prior receipt 沒有記錄的 credential（例如新增 provider），升級會在 activate
  前停止並回到 prior；依 §4 匯入後改走下列手動流程。
- `cortex upgrade --status` 唯讀顯示生效中的 receipt、上一次升級結果與 loaded runtime 是否一致。
- 升級中途 shell 或主機被 SIGKILL、OOM、斷電打斷時，執行
  `sudo /opt/cortex/venv/bin/cortex upgrade --recover`：它從 maintenance snapshot 取得 plan sha、
  核對 durable plan，再走 §6 的 recovery。

下列 §1–§6 是首次安裝與手動操作參考，也是 `cortex upgrade` 每一步的逐步說明；首次安裝與
legacy adoption（`trust-root-legacy-adoption.md`）仍照這些步驟操作。

```

（上方 `sudo /opt/cortex/venv/bin/cortex upgrade <版本>` 以四格縮排呈現為程式碼，刻意不用 ```` ```bash ```` 區塊，避免既有測試以 `index()` 比對的 bash 片段在文件前段多出一份。）

`docs/superpowers/runbooks/trust-root-legacy-adoption.md`：在檔尾（`## 10. 已知限制` 清單之後）加一段：

```markdown

adoption 完成（verify 通過、receipt 為 applied＋qualified）後，之後的升級一律使用
`sudo /opt/cortex/venv/bin/cortex upgrade <版本>`（見 `trust-root-transactional-install.md`
開頭的「一般升級」）；它讀 host overlay 時會略過 `legacy_adoption` 區塊，overlay digest 不變。
```

建立 `changelog.d/one-command-upgrade.md`：

```markdown
### Added

- **#1263 一鍵升級 `cortex upgrade <版本>`**：owner 以 root 執行 `sudo /opt/cortex/venv/bin/cortex upgrade <版本>`，工具依序完成 release ingress（GitHub REST metadata 的 annotated tag target 與 asset digest、qualification manifest、archive topology、bundle 逐檔驗證、wheelhouse 離線 `--copies` 封存 venv）、以非 root 身分產生 plan 並綁定 plan sha、發布 durable plan、maintenance lease 與停服務、`apply --prior-receipt`、沿用 prior receipt 已記錄的憑證、activate、verify 與 loaded↔installed 核對，並寫出 `/var/lib/cortex-installer/<版本>/upgrade-report.json`。activate 前失敗自動 rollback 並恢復原本的服務；activate 後失敗若 `restore_safe=false` 就停在原地並列出保留狀態。`--recover` 由 maintenance snapshot 推出 durable plan 後走既有 recovery，`--status` 唯讀。
- **#1263 prior receipt 憑證 handoff**：新增 `inherit_prior_credentials` 與 `cortex install trust-root credentials inherit`；只接手 prior receipt 已記錄、落點相同、且通過 regular file／nlink 1／uid／gid／0600／sha256 檢查的列，繼承列帶 `inherited_from`。rollback 不刪 prior 的憑證檔，`restore_safe` 接受只剩繼承列的 receipt。修正 `apply --prior-receipt` 之後 activate 必定 `missing required credential` 的問題。
- **#1263 生效中 receipt 由 receipt chain 判定**：新增 `effective_receipt(state_root)`，不再依 mtime 挑選；不唯一或無法判定時拒絕。
- **#1263 RC qualification**：以 `cortex upgrade` 取代原本 apply 後立即 rollback 的升級演練，先演練 activate 前失敗自動回到 prior 並恢復服務，再做完整升級；新增 `one-command-upgrade` evidence 與 validator 檢查，loaded↔installed 比對移入 `paulsha_cortex/trust_root/install/loaded_runtime.py`。
```

`CHANGELOG.md`：在 `## [Unreleased]` 底下 `### Added` 的第一個條目之前加一行：

```markdown
- **#1263 一鍵升級 `cortex upgrade <版本>`**：root 一個指令完成 ingress → plan → apply → 憑證繼承 → activate → verify → loaded↔installed 核對，失敗自動回到前一版；新增 `--recover`／`--status`、`credentials inherit`、`effective_receipt` 與 RC 升級演練（見 `changelog.d/one-command-upgrade.md`）（#1263）。
```

- [ ] **Step 4: 跑測試確認通過，並跑全套**

Run: `python3 -m pytest tests/test_phase2_closeout_runbook.py -q`
Expected: 全部 PASS。

Run: `python3 -m pytest tests/ -q`
Expected: 全部 PASS（沒有 FAIL／ERROR）。

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/runbooks/trust-root-transactional-install.md docs/superpowers/runbooks/trust-root-legacy-adoption.md changelog.d/one-command-upgrade.md CHANGELOG.md tests/test_phase2_closeout_runbook.py
git commit -m "docs(trust-root): runbook 開頭新增一般升級 cortex upgrade 與 changelog（#1263）"
```

開 PR 前依 `CLAUDE.md` 帶 PR 上下文跑 `python3 -m policy_check --repo . --pr-title "<PR 標題>" --pr-body "<PR body>" --pr-labels "<labels>" --pr-base-ref main --pr-head-ref feature/1263-one-command-upgrade`，PR body 寫 `Closes #1263`。
