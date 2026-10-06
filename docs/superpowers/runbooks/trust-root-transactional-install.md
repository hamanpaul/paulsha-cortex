---
status: executable
work_item: trust-root-phase2-closeout
phase: 2
audience: operator
supersedes: docs/superpowers/runbooks/trust-root-phase2b-setup.md
authority: transactional-installer
refs:
  - docs/superpowers/specs/trust-root-isolation-spec.md
  - paulsha_cortex/trust_root/install/cli.py
  - qualification/run.sh
---

# Trust Root Phase 2 transactional install

這是目前唯一可執行的 production 安裝／升級 runbook。舊的 Phase 2b 文件只保留歷史
診斷與決策脈絡，不得再照其中的 `rm`／`cp`／`chown`／`mv` 手工重播部署狀態。

## 一般升級：`cortex upgrade`

主機已有 installer 寫入、狀態為 applied＋qualified 的 receipt 時，升級只需要一個 root 指令：

    sudo /opt/cortex/venv/bin/cortex upgrade <版本>

經 SSH 升級時，一律在 `tmux` 或 `screen` 裡執行 `cortex upgrade`：斷線送出的 SIGHUP 雖會自動
rollback，但升級本身就此中止，終端機上的摘要也會跟著消失。maintenance window 內的 INT／TERM／HUP
不會殺掉正在執行的 installer 子程序：工具會等正在執行的那一步結束，再 rollback；同一步還在跑時
再送一次訊號，才會立即中止該步（之後由 installer 的 journal crash recovery 收拾）。

工具依序執行下列 §1–§5：以 GitHub Releases REST metadata 取得 annotated tag 的 commit target
與三個 asset digest，驗過 qualification manifest、archive topology 與 bundle 每一個檔案後，
在新的 `/var/lib/cortex-installer/<版本>/attempt-*` 目錄封存 candidate CLI；以非 root 身分產生
plan 並自行綁定 plan sha、發布 durable plan；取得 maintenance lease、記下並停止服務；以
`--prior-receipt` apply；沿用 prior receipt 已記錄的 credentials（`credentials inherit`）：
新 plan 推得的落點必須與 prior 相同，落點檔照舊必須是 regular file、nlink 1、屬該帳號的
uid／gid、權限 0600。executor 會自己改寫登入檔（例如 codex 刷新 token 時重寫 `auth.json`），
因此新 receipt 記錄接手當下的 sha256 並標示 `inherited_from`，不要求等於 prior 匯入時記錄的
值（那個值仍在 prior receipt 的同一列）；除了計算 sha256 不讀憑證內容。接著 activate（以剛
記錄的 sha256 驗證憑證；升級期間服務已停，不會再刷新）、verify，最後核對 loaded runtime 與新
receipt 一致。生效中的 receipt 由 receipt chain 判定，不看檔名或時間。結果寫在
`/var/lib/cortex-installer/<版本>/upgrade-report.json`，終端機印出摘要（`--json` 改印完整報告）。

- 只升不降：目標版本必須高於生效中的 receipt；降版改用 installer `rollback` 或下列手動流程。
- 有在飛 job 時預設直接拒絕；`--wait-idle <秒>` 會等到 idle 或逾時。
- activate 之前的失敗（ingress、plan、apply、credential handoff）會自動 rollback 並恢復原本
  active 的服務。activate 之後的失敗若 rollback 回報 `restore_safe=false`，服務維持停止、
  snapshot 保留，報告列出保留的 unknown state／drift；處理後執行
  `sudo /opt/cortex/venv/bin/cortex upgrade --recover`，或依 §6「手動恢復 `cortex upgrade`
  中斷的升級」由 operator 裁決。
- 新 plan 若要求 prior receipt 沒有記錄的 credential（例如新增 provider），升級會在 activate
  前停止並回到 prior；依 §4 匯入後改走下列手動流程。
- `cortex upgrade --status` 唯讀顯示生效中的 receipt、上一次升級結果與 loaded runtime 是否一致。
- 升級中途 shell 或主機被 SIGKILL、OOM、斷電打斷時，執行
  `sudo /opt/cortex/venv/bin/cortex upgrade --recover`：它從 maintenance snapshot 取得 plan sha、
  核對 durable plan，再走 §6 的 recovery。
- 每次執行都在 `/var/lib/cortex-installer/<版本>/attempt-*` 留下完整的 install input、toolchain
  與封存 venv（數百 MB 到約 1 GB），工具不會自動清理：失敗重試會在同一版本目錄下累積。report 的
  `candidate.attempt_dir` 指向該次使用的目錄，`--recover` 與 §6 的手動恢復都要用到它，durable plan
  與 receipt 也記錄了其中的路徑。只在 `cortex upgrade --status` 顯示 `maintenance: idle`、loaded
  runtime 為 `match`，而且生效中的 receipt 已是之後的版本時，才可以刪除舊版本的 attempt 目錄；
  生效中版本的 attempt 目錄（含失敗的重試）保留到下一次升級成功之後。刪除前先確認沒有任何
  `upgrade-report.json` 的 `result` 是 `halted`／`in-progress`。

下列 §1–§6 是首次安裝與手動操作參考，也是 `cortex upgrade` 每一步的逐步說明；首次安裝與
legacy adoption（`trust-root-legacy-adoption.md`）仍照這些步驟操作。

## 邊界

- GitHub release 會發佈 immutable wheel、同一個 RC 驗過的完整
  `*-install-input.tar.gz` 與永久 qualification manifest；**不會**自動改動主機的
  `/opt/cortex`。
- `plan` 不需 root，也不更動系統；只有 operator 明確執行的 `apply`、credential
  `import`、`activate`、`verify`、`rollback` 會進入 root 邊界。
- installer 不從 `$HOME` 猜測、搜尋或複製任何憑證。每一份 credential source 都由
  operator 明確選定，內容不放在 argv、receipt 或 log。
- receipt 是 apply／activate／verify／rollback 的 authority。遇到未知 drift 時會
  fail closed；不要以手工覆寫繞過。
- `apply`、credential import、activate、verify 與 rollback 會共用不受 roots、plan
  或 `--receipt` override 影響的 host-global transaction lock；任何兩筆 installer
  mutation 不會交疊。runbook 另從讀取 service snapshot 之前到 rollback/restore
  或 verify/active checks 完成為止，持有 host-global maintenance lease，序列化合作的完整
  service lifecycle。能任意忽略這些 lock 並改寫主機的另一個 root/admin
  程序不在 job-account 威脅模型內；逐 step re-inspection 與 durable receipt
  負責在這類 out-of-model drift 發生時停止並提供 rollback authority。

## 1. 封存唯一 candidate CLI

先由 release artifact ingress 將 `v0.1.13` 的 install-input archive 與 qualification
manifest 放到 `/var/lib/cortex-installer/0.1.13/release`。這個 ingress 是前置 authority：
目錄及每一層 ancestor 必須是 root-owned、不可由 group/other 寫入、不可有 symlink。
不要直接從使用者 checkout、`$HOME` 或 `/tmp` 以 root 執行 candidate code。

candidate SHA 必須抄自 annotated tag 的 commit target；三個 release asset SHA-256 必須抄自
GitHub Releases REST asset metadata 的 `digest` 欄位（去掉 `sha256:` prefix），並逐一核對
asset name。不可由下載後的本機檔案自己產生 expected 值。先驗 archive 與 qualification
manifest，才可從 manifest 導出 bundle hash、檢查 archive topology 並解壓；完整
qualification input（`bundle.json`、`dist/`、`wheelhouse/`、`source/`、`toolchain/` 與
`install-config.yaml`）因此都受 release asset digest 保護。其後再驗 bundle 所列的每個檔案，
並用完整 wheelhouse 離線建立 venv；`--copies` 避免 venv 內出現指向 ambient Python 的
symlink。

```bash
set -euo pipefail
umask 077
PATH=/usr/bin:/bin
export PATH

cortex_installer_root=/var/lib/cortex-installer
cortex_bootstrap_root="$cortex_installer_root/0.1.13"
cortex_release_root="$cortex_bootstrap_root/release"
cortex_input_root="$cortex_bootstrap_root/input"
cortex_install_input_archive="$cortex_release_root/paulsha-cortex-0.1.13-install-input.tar.gz"
cortex_qualification_manifest="$cortex_release_root/paulsha-cortex-0.1.13-qualification.json"
cortex_bundle="$cortex_input_root/bundle.json"
cortex_install_config="$cortex_input_root/install-config.yaml"
cortex_release_candidate_sha=<40-hex-annotated-tag-target>
cortex_release_wheel_asset_name=paulsha_cortex-0.1.13-py3-none-any.whl
cortex_release_wheel_asset_sha256=<64-hex-release-wheel-asset-digest>
cortex_install_input_asset_sha256=<64-hex-release-install-input-asset-digest>
cortex_qualification_asset_sha256=<64-hex-release-qualification-asset-digest>
cortex_cli="$cortex_bootstrap_root/venv/bin/cortex"
cortex_bootstrap_requirements="$cortex_bootstrap_root/bootstrap-requirements.txt"

test "${#cortex_release_candidate_sha}" -eq 40
test "${#cortex_release_wheel_asset_sha256}" -eq 64
test "${#cortex_install_input_asset_sha256}" -eq 64
test "${#cortex_qualification_asset_sha256}" -eq 64
case $cortex_release_candidate_sha$cortex_release_wheel_asset_sha256$cortex_install_input_asset_sha256$cortex_qualification_asset_sha256 in
  (*[!0-9a-f]*) exit 1 ;;
esac

cortex_cursor=$cortex_release_root
while [ "$cortex_cursor" != / ]; do
  test ! -L "$cortex_cursor"
  test "$(stat -c %u "$cortex_cursor")" -eq 0
  cortex_mode=$(stat -c %a "$cortex_cursor")
  test $((8#$cortex_mode & 8#22)) -eq 0
  cortex_cursor=$(dirname "$cortex_cursor")
done
for cortex_release_file in "$cortex_install_input_archive" "$cortex_qualification_manifest"; do
  /usr/bin/sudo /usr/bin/test -f "$cortex_release_file"
  /usr/bin/sudo /usr/bin/test ! -L "$cortex_release_file"
  test "$(/usr/bin/sudo /usr/bin/stat -c %u "$cortex_release_file")" -eq 0
  test "$(/usr/bin/sudo /usr/bin/stat -c %h "$cortex_release_file")" -eq 1
  cortex_mode=$(/usr/bin/sudo /usr/bin/stat -c %a "$cortex_release_file")
  test $((8#$cortex_mode & 8#22)) -eq 0
done
printf '%s  %s\n' "$cortex_install_input_asset_sha256" "$cortex_install_input_archive" |
  /usr/bin/sudo /usr/bin/sha256sum --check --strict -
printf '%s  %s\n' "$cortex_qualification_asset_sha256" "$cortex_qualification_manifest" |
  /usr/bin/sudo /usr/bin/sha256sum --check --strict -

mapfile -t cortex_qualification_authority < <(
  /usr/bin/sudo /usr/bin/python3 -I -S - "$cortex_qualification_manifest" <<'PY'
import json
import re
import sys

document = json.load(open(sys.argv[1], encoding="utf-8"))
wheel = document.get("wheel")
bundle = document.get("bundle")
if (
    document.get("schema_version") != 2
    or document.get("profile") != "release"
    or document.get("status") != "passed"
    or not isinstance(wheel, dict)
    or not isinstance(bundle, dict)
):
    raise SystemExit("qualification manifest is not a passed release attestation")
values = (
    document.get("candidate_sha"),
    wheel.get("filename"),
    wheel.get("sha256"),
    bundle.get("sha256"),
)
if (
    not isinstance(values[0], str)
    or re.fullmatch(r"[0-9a-f]{40}", values[0]) is None
    or not isinstance(values[1], str)
    or "/" in values[1]
    or not all(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
        for value in values[2:]
    )
):
    raise SystemExit("qualification manifest identity is invalid")
print(*values, sep="\n")
PY
)
test "${#cortex_qualification_authority[@]}" -eq 4
cortex_candidate_sha=${cortex_qualification_authority[0]}
cortex_candidate_wheel_name=${cortex_qualification_authority[1]}
cortex_candidate_wheel_sha256=${cortex_qualification_authority[2]}
cortex_bundle_sha256=${cortex_qualification_authority[3]}
test "$cortex_candidate_sha" = "$cortex_release_candidate_sha"
test "$cortex_candidate_wheel_name" = "$cortex_release_wheel_asset_name"
test "$cortex_candidate_wheel_sha256" = "$cortex_release_wheel_asset_sha256"

/usr/bin/sudo /usr/bin/python3 -I -S - "$cortex_install_input_archive" <<'PY'
import sys
import tarfile
from pathlib import PurePosixPath

with tarfile.open(sys.argv[1], mode="r:gz") as archive:
    seen = set()
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or not path.parts
            or path.parts[0] != "qualification-input"
            or ".." in path.parts
            or member.name in seen
            or not (member.isdir() or member.isfile())
        ):
            raise SystemExit("install-input archive topology is unsafe")
        seen.add(member.name)
PY
if /usr/bin/sudo /usr/bin/test -e "$cortex_input_root" || \
   /usr/bin/sudo /usr/bin/test -L "$cortex_input_root"; then
  echo "qualification input path already exists; use a fresh immutable version root" >&2
  exit 1
fi
/usr/bin/sudo /usr/bin/install -d -o root -g root -m 0700 "$cortex_input_root"
/usr/bin/sudo /usr/bin/tar --extract --gzip --file "$cortex_install_input_archive" \
  --directory "$cortex_input_root" --strip-components=1 \
  --no-same-owner --no-same-permissions
printf '%s  %s\n' "$cortex_bundle_sha256" "$cortex_bundle" |
  /usr/bin/sudo /usr/bin/sha256sum --check --strict -

/usr/bin/sudo /usr/bin/env -i HOME=/root PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
  PYTHONNOUSERSITE=1 /usr/bin/python3 -I -S - \
  "$cortex_bundle" "$cortex_candidate_sha" "$cortex_candidate_wheel_sha256" \
  "$cortex_bootstrap_requirements" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath

bundle = Path(sys.argv[1])
candidate_sha, wheel_sha, requirements_name = sys.argv[2:]
root = bundle.parent
if not root.is_absolute():
    raise SystemExit("qualification input root must be absolute")
for path in [root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())]:
    observed = path.lstat()
    if stat.S_ISLNK(observed.st_mode):
        raise SystemExit("qualification input contains a symlink")
    if observed.st_uid != 0 or stat.S_IMODE(observed.st_mode) & 0o022:
        raise SystemExit("qualification input ownership/mode is unsafe")
    if stat.S_ISDIR(observed.st_mode):
        continue
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise SystemExit("qualification input contains an unsafe object")
document = json.loads(bundle.read_text(encoding="utf-8"))
if document.get("candidate_sha") != candidate_sha:
    raise SystemExit("bundle candidate mismatch")
wheelhouse_rows = document.get("wheelhouse")
if not isinstance(wheelhouse_rows, list) or not wheelhouse_rows:
    raise SystemExit("bundle wheelhouse is invalid")
rows = [document.get("wheel")]
rows += wheelhouse_rows
rows += document.get("generated_artifacts", [])
rows += [
    {"path": row.get("path"), "sha256": row.get("sha256")}
    for key in ("toolchain", "source_repositories")
    for row in document.get(key, [])
]
seen = set()
for row in rows:
    if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
        raise SystemExit("invalid bundle artifact row")
    relative = row["path"]
    pure = PurePosixPath(relative) if isinstance(relative, str) else PurePosixPath("/")
    if pure.is_absolute() or ".." in pure.parts or "\0" in str(relative):
        raise SystemExit("unsafe bundle artifact path")
    path = root / pure
    observed = path.lstat()
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise SystemExit("bundle artifact is not a single-link regular file")
    cursor = root
    for part in pure.parts[:-1]:
        cursor /= part
        if cursor.is_symlink():
            raise SystemExit("bundle artifact has a symlink ancestor")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != row["sha256"] or relative in seen:
        raise SystemExit("bundle artifact hash/inventory mismatch")
    seen.add(relative)
if document["wheel"]["sha256"] != wheel_sha:
    raise SystemExit("candidate wheel hash mismatch")
declared_wheelhouse = {
    row["path"] for row in wheelhouse_rows if isinstance(row, dict)
}
if (
    len(declared_wheelhouse) != len(wheelhouse_rows)
    or any(
        PurePosixPath(relative).parent != PurePosixPath("wheelhouse")
        or not relative.endswith(".whl")
        for relative in declared_wheelhouse
    )
):
    raise SystemExit("wheelhouse manifest paths are invalid or duplicated")
wheelhouse_root = root / "wheelhouse"
if wheelhouse_root.is_symlink() or not wheelhouse_root.is_dir():
    raise SystemExit("wheelhouse root is unsafe")
actual_wheelhouse = set()
for path in wheelhouse_root.iterdir():
    observed = path.lstat()
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise SystemExit("wheelhouse contains a non-regular entry")
    actual_wheelhouse.add(path.relative_to(root).as_posix())
if actual_wheelhouse != declared_wheelhouse:
    raise SystemExit("wheelhouse inventory differs from the manifest")
if not any(row.get("sha256") == wheel_sha for row in wheelhouse_rows):
    raise SystemExit("wheelhouse lacks the exact candidate wheel")
requirements = Path(requirements_name)
if requirements.exists() or requirements.parent != root.parent:
    raise SystemExit("bootstrap requirements path is unsafe")
requirements.write_text(
    "".join(
        f"{(root / PurePosixPath(row['path'])).as_uri()} "
        f"--hash=sha256:{row['sha256']}\n"
        for row in sorted(wheelhouse_rows, key=lambda item: item["path"])
    ),
    encoding="utf-8",
)
requirements.chmod(0o600)
PY

# input 不是 credential surface；hash/owner/topology 驗證後，才開放 plan user
# traverse/read。保留原本 executable bit，且不開放任何 group/other write。
/usr/bin/sudo /usr/bin/chmod 0755 "$cortex_installer_root" "$cortex_bootstrap_root" "$cortex_input_root"
/usr/bin/sudo /usr/bin/chmod -R u=rwX,go=rX "$cortex_input_root"

/usr/bin/sudo /usr/bin/test ! -e "$cortex_bootstrap_root/venv"
/usr/bin/sudo /usr/bin/env -i HOME=/root PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
  PYTHONNOUSERSITE=1 /usr/bin/python3 -I -S -m venv --copies "$cortex_bootstrap_root/venv"
/usr/bin/sudo /usr/bin/env -i HOME=/root PATH="$cortex_bootstrap_root/venv/bin:/usr/bin:/bin" \
  LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONNOUSERSITE=1 \
  "$cortex_bootstrap_root/venv/bin/python" -I -m pip install \
  --no-index --no-deps --only-binary=:all: --require-hashes \
  --requirement "$cortex_bootstrap_requirements"
/usr/bin/sudo /usr/bin/chown -R root:root "$cortex_bootstrap_root/venv"
if /usr/bin/sudo /usr/bin/test -L "$cortex_bootstrap_root/venv/lib64"; then
  test "$(/usr/bin/sudo /usr/bin/readlink "$cortex_bootstrap_root/venv/lib64")" = lib
  /usr/bin/sudo /usr/bin/unlink "$cortex_bootstrap_root/venv/lib64"
fi
/usr/bin/sudo /usr/bin/chmod -R u=rwX,go=rX "$cortex_bootstrap_root/venv"
test -x "$cortex_cli"
```

用下列函式對已封存 venv 做 deterministic tree attestation。plan 前記一次，apply 前再記
一次；兩次 digest 不同就停止。digest 只涵蓋 relative path、mode 與 file content，且遇到
symlink、非 root owner、group/other writable 或特殊檔案立即失敗。

```bash
cortex_cli_tree_sha() {
  /usr/bin/sudo /usr/bin/env -i HOME=/root PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    PYTHONNOUSERSITE=1 /usr/bin/python3 -I -S - "$cortex_bootstrap_root/venv" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
digest = hashlib.sha256()
paths = [root, *sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix())]
for path in paths:
    relative = "." if path == root else path.relative_to(root).as_posix()
    observed = path.lstat()
    if observed.st_uid != 0 or stat.S_IMODE(observed.st_mode) & 0o022:
        raise SystemExit("unsafe candidate CLI ownership/mode")
    digest.update(relative.encode() + b"\0")
    digest.update(format(stat.S_IMODE(observed.st_mode), "04o").encode() + b"\0")
    if stat.S_ISDIR(observed.st_mode):
        digest.update(b"D\0")
    elif stat.S_ISREG(observed.st_mode) and observed.st_nlink == 1:
        digest.update(b"F\0" + hashlib.sha256(path.read_bytes()).digest())
    else:
        raise SystemExit("unsafe candidate CLI tree object")
print(digest.hexdigest())
PY
}
cortex_sealed_cli_tree_sha=$(cortex_cli_tree_sha)
cortex_root_cli() {
  test "$(cortex_cli_tree_sha)" = "$cortex_sealed_cli_tree_sha"
  /usr/bin/sudo /usr/bin/env -i HOME=/root \
    PATH="$cortex_bootstrap_root/venv/bin:/usr/bin:/bin" \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONNOUSERSITE=1 \
    "$cortex_cli" "$@"
}
```

## 2. 產生並三方確認 plan

使用上一步同一支 root-owned candidate CLI，在非 root、空白環境中產生 plan。以下三個
值各有不同來源：CLI 回報值、plan 檔實際 digest、operator 人工確認值；任一不相等就
停止。

```bash
set -euo pipefail
umask 077
PATH=/usr/bin:/bin
export PATH

cortex_plan_dir=$(mktemp -d "${TMPDIR:-/tmp}/cortex-install.XXXXXX")
cortex_plan_home="$cortex_plan_dir/home"
mkdir -m 0700 "$cortex_plan_home"
cortex_plan_path="$cortex_plan_dir/install-plan.json"
cortex_plan_result="$cortex_plan_dir/plan-result.json"
cortex_install_evidence="$cortex_plan_dir/install-verification.json"

test -f "$cortex_install_config"
test -f "$cortex_bundle"
test "$(cortex_cli_tree_sha)" = "$cortex_sealed_cli_tree_sha"
env -i HOME="$cortex_plan_home" \
  PATH="$cortex_bootstrap_root/venv/bin:/usr/bin:/bin" \
  LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONNOUSERSITE=1 \
  "$cortex_cli" install trust-root plan \
  --config "$cortex_install_config" \
  --bundle "$cortex_bundle" \
  --output "$cortex_plan_path" >"$cortex_plan_result"

cortex_reported_plan_sha=$(/usr/bin/python3 -I -S - "$cortex_plan_result" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["plan_sha256"])
PY
)
cortex_observed_plan_sha=$(/usr/bin/sha256sum "$cortex_plan_path" | /usr/bin/awk '{print $1}')
test "$cortex_reported_plan_sha" = "$cortex_observed_plan_sha"

cortex_canonical_receipt_path=$(/usr/bin/python3 -I -S - "$cortex_plan_path" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["receipt_path"])
PY
)
cortex_receipt_nonce=$(/usr/bin/python3 -I -S -c 'import secrets; print(secrets.token_hex(16))')
cortex_receipt_path="${cortex_canonical_receipt_path%.json}.run-${cortex_receipt_nonce}.json"
test "${cortex_receipt_path#/}" != "$cortex_receipt_path"
/usr/bin/printf 'Effective receipt path: %s\n' "$cortex_receipt_path"

/usr/bin/python3 -I -S -m json.tool "$cortex_plan_path"
read -r -p "Type the reviewed plan SHA-256: " cortex_confirmed_plan_sha
test "$cortex_confirmed_plan_sha" = "$cortex_reported_plan_sha"

# 在任何 service stop／maintenance lease 前，把 exact reviewed plan 完整發布到
# root-only durable storage。compliant writer 以同一把 lock 序列化；既有 target
# 只在 owner／mode／inode topology／bytes 全部相符時可重用，絕不覆寫。
cortex_durable_plan_root="$cortex_installer_root/plans"
cortex_durable_plan_path="$cortex_durable_plan_root/$cortex_confirmed_plan_sha.json"
/usr/bin/sudo /usr/bin/install -d -o root -g root -m 0700 "$cortex_durable_plan_root"
/usr/bin/sudo /usr/bin/env -i HOME=/root PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
  PYTHONNOUSERSITE=1 /usr/bin/python3 -I -S - \
  "$cortex_plan_path" "$cortex_durable_plan_path" "$cortex_confirmed_plan_sha" <<'PY'
import fcntl
import hashlib
import os
import secrets
import stat
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
expected = sys.argv[3]
payload = source.read_bytes()
if hashlib.sha256(payload).hexdigest() != expected:
    raise SystemExit("reviewed plan changed before durable publication")
root = target.parent
root_stat = root.lstat()
if (
    not stat.S_ISDIR(root_stat.st_mode)
    or root_stat.st_uid != 0
    or stat.S_IMODE(root_stat.st_mode) != 0o700
):
    raise SystemExit("durable plan root is unsafe")
root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
lock_fd = os.open(
    ".publish.lock",
    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
    0o600,
    dir_fd=root_fd,
)
staging_name = f".{target.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
staging_fd = None
try:
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    lock_stat = os.fstat(lock_fd)
    if (
        not stat.S_ISREG(lock_stat.st_mode)
        or lock_stat.st_uid != 0
        or lock_stat.st_nlink != 1
        or stat.S_IMODE(lock_stat.st_mode) != 0o600
    ):
        raise SystemExit("durable plan publication lock is unsafe")
    try:
        existing_fd = os.open(
            target.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
    except FileNotFoundError:
        existing_fd = None
    if existing_fd is not None:
        try:
            observed = os.fstat(existing_fd)
            existing = b""
            while True:
                chunk = os.read(existing_fd, 1024 * 1024)
                if not chunk:
                    break
                existing += chunk
        finally:
            os.close(existing_fd)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != 0
            or observed.st_nlink != 1
            or stat.S_IMODE(observed.st_mode) != 0o600
            or existing != payload
        ):
            raise SystemExit("existing durable plan does not match reviewed bytes")
    else:
        staging_fd = os.open(
            staging_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_fd,
        )
        view = memoryview(payload)
        while view:
            written = os.write(staging_fd, view)
            if written <= 0:
                raise OSError("short durable plan write")
            view = view[written:]
        os.fsync(staging_fd)
        os.close(staging_fd)
        staging_fd = None
        # The root-only lock makes this a no-overwrite publication among all
        # compliant invocations. A privileged actor is outside this runbook's
        # unprivileged-job threat model.
        os.rename(staging_name, target.name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        os.fsync(root_fd)
finally:
    if staging_fd is not None:
        os.close(staging_fd)
    try:
        os.unlink(staging_name, dir_fd=root_fd)
    except FileNotFoundError:
        pass
    os.close(lock_fd)
    os.close(root_fd)
PY

# 後續 lease、apply 與 hard-crash recovery 都只使用 durable exact-plan path。
cortex_plan_path=$cortex_durable_plan_path
```

人工 review 至少確認 candidate SHA／wheel hash、四個 service accounts、所有目標路徑、
systemd units、polkit 規則、toolchain artifacts、required credentials、canonical receipt
parent 與本次隨機且不存在的 effective receipt path 都符合本次變更。不要只把畫面上的 SHA
複製回 prompt；確認值代表 operator 已閱讀 plan 並接受其完整 mutation set。

## 3. Stop services and apply exact plan

只有三方 SHA 一致後才進 root 邊界。preflight 明確拒絕 active services，因此先記住
哪些 units 原本 active，再停止三個 units。fresh install 不帶 `--prior-receipt`；upgrade
必須明確指定上一個已 `applied` 且 `qualified` 的 root-owned receipt，installer 只從該
receipt 繼承同 roots、同 repository remote、逐 step 相符的 provenance。

```bash
cortex_prior_receipt=  # fresh install 保持空字串；upgrade 填絕對路徑
cortex_prior_args=()
if [ -n "$cortex_prior_receipt" ]; then
  test "${cortex_prior_receipt#/}" != "$cortex_prior_receipt"
  /usr/bin/sudo /usr/bin/test -f "$cortex_prior_receipt"
  cortex_prior_args=(--prior-receipt "$cortex_prior_receipt")
fi

cortex_services=(
  cortex-egress-proxy.service
  cortex-manager.service
  cortex-monitor.service
)
cortex_present_services=()
cortex_previously_active=()
cortex_maintenance_read_fd=
cortex_maintenance_write_fd=
cortex_maintenance_pid=
cortex_maintenance_token=
cortex_restore_active() {
  local cortex_restore_failed=0
  local cortex_failed_service=
  local -a cortex_restored_services=()
  for cortex_service in "${cortex_previously_active[@]}"; do
    if ! /usr/bin/sudo /usr/bin/systemctl start "$cortex_service"; then
      cortex_restore_failed=1
      cortex_failed_service=$cortex_service
      break
    fi
    cortex_restored_services+=("$cortex_service")
  done
  if [ "$cortex_restore_failed" -ne 0 ]; then
    # A failed start may still have changed unit state. Re-stop that unit and
    # every unit already restored; durable recovery state remains on failure.
    /usr/bin/sudo /usr/bin/systemctl stop "$cortex_failed_service" || true
    for cortex_restored_service in "${cortex_restored_services[@]}"; do
      /usr/bin/sudo /usr/bin/systemctl stop "$cortex_restored_service" || true
    done
  fi
  return "$cortex_restore_failed"
}
cortex_release_maintenance_lease() {
  local cortex_release_mode=${1:-preserve}
  local cortex_lease_status=0
  local cortex_release_token=${cortex_maintenance_token:-}
  if [ -n "${cortex_maintenance_write_fd:-}" ]; then
    if [ "$cortex_release_mode" = complete ]; then
      /usr/bin/printf 'complete\n' >&"$cortex_maintenance_write_fd" || cortex_lease_status=1
    fi
    exec {cortex_maintenance_write_fd}>&-
    cortex_maintenance_write_fd=
  fi
  if [ -n "${cortex_maintenance_pid:-}" ]; then
    wait "$cortex_maintenance_pid" || cortex_lease_status=$?
    cortex_maintenance_pid=
  fi
  if [ "$cortex_release_mode" = complete ] && \
     [ "$cortex_lease_status" -ne 0 ] && [ -n "$cortex_release_token" ]; then
    if ! cortex_root_cli install trust-root lease-release \
      --plan "$cortex_plan_path" \
      --confirm-sha256 "$cortex_confirmed_plan_sha" \
      --receipt "$cortex_receipt_path" \
      --maintenance-token "$cortex_release_token" >/dev/null; then
      cortex_lease_status=1
    else
      cortex_lease_status=0
    fi
  fi
  cortex_maintenance_token=
  return "$cortex_lease_status"
}
cortex_acquire_maintenance_lease() {
  local cortex_maintenance_result=
  coproc CORTEX_MAINTENANCE_LEASE {
    cortex_root_cli install trust-root lease \
      --plan "$cortex_plan_path" \
      --confirm-sha256 "$cortex_confirmed_plan_sha" \
      --receipt "$cortex_receipt_path"
  }
  cortex_maintenance_read_fd=${CORTEX_MAINTENANCE_LEASE[0]}
  cortex_maintenance_write_fd=${CORTEX_MAINTENANCE_LEASE[1]}
  cortex_maintenance_pid=$CORTEX_MAINTENANCE_LEASE_PID
  if ! IFS= read -r cortex_maintenance_result <&"$cortex_maintenance_read_fd"; then
    exec {cortex_maintenance_read_fd}<&-
    cortex_maintenance_read_fd=
    cortex_release_maintenance_lease || true
    return 1
  fi
  exec {cortex_maintenance_read_fd}<&-
  cortex_maintenance_read_fd=
  if ! cortex_maintenance_token=$(/usr/bin/python3 -I -S - \
    "$cortex_maintenance_result" "$cortex_confirmed_plan_sha" \
    "$cortex_receipt_path" <<'PY'
import json
import sys

document = json.loads(sys.argv[1])
if set(document) != {
    "maintenance_lease",
    "maintenance_token",
    "plan_sha256",
    "present_services",
    "previously_active",
    "receipt_path",
    "snapshot_path",
}:
    raise SystemExit("maintenance lease response has an invalid shape")
if (
    document["maintenance_lease"] is not True
    or document["plan_sha256"] != sys.argv[2]
    or document["receipt_path"] != sys.argv[3]
    or document["snapshot_path"] != "/var/lib/cortex-installer/maintenance-snapshot.json"
):
    raise SystemExit("maintenance lease is not bound to the reviewed plan")
allowed = {
    "cortex-egress-proxy.service",
    "cortex-manager.service",
    "cortex-monitor.service",
}
present = document["present_services"]
active = document["previously_active"]
if (
    not isinstance(present, list)
    or not isinstance(active, list)
    or len(present) != len(set(present))
    or len(active) != len(set(active))
    or any(name not in allowed for name in present)
    or any(name not in present for name in active)
):
    raise SystemExit("maintenance service snapshot is invalid")
token = document["maintenance_token"]
if not isinstance(token, str) or len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
    raise SystemExit("maintenance lease token is invalid")
print(token)
PY
  ); then
    cortex_release_maintenance_lease || true
    return 1
  fi
  mapfile -t cortex_present_services < <(
    /usr/bin/python3 -I -S -c \
      'import json,sys; print(*json.loads(sys.argv[1])["present_services"], sep="\n")' \
      "$cortex_maintenance_result"
  )
  mapfile -t cortex_previously_active < <(
    /usr/bin/python3 -I -S -c \
      'import json,sys; print(*json.loads(sys.argv[1])["previously_active"], sep="\n")' \
      "$cortex_maintenance_result"
  )
}
cortex_apply_attempted=0
cortex_abort_restore() {
  local cortex_abort_status=${1:-1}
  local cortex_restore_safe=0
  local cortex_rollback_result=
  trap - EXIT INT TERM
  if [ "$cortex_apply_attempted" -eq 1 ] && \
     /usr/bin/sudo /usr/bin/test -f "$cortex_receipt_path"; then
    cortex_rollback_result=$(cortex_root_cli install trust-root rollback \
      --receipt "$cortex_receipt_path" \
      --maintenance-token "$cortex_maintenance_token") || cortex_rollback_result=
    if [ -n "$cortex_rollback_result" ]; then
      if /usr/bin/printf '%s\n' "$cortex_rollback_result" | /usr/bin/python3 -I -S -c \
        'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("restore_safe") is True else 1)'; then
        cortex_restore_safe=1
      fi
    fi
  else
    cortex_restore_safe=1
  fi
  if [ "$cortex_restore_safe" -eq 1 ] && cortex_restore_active; then
    cortex_release_maintenance_lease complete || true
  else
    # Preserve the root-owned snapshot/marker for the explicit recovery command.
    cortex_release_maintenance_lease || true
  fi
  exit "$cortex_abort_status"
}

# 先取得 host-global maintenance lease，才可讀取 service snapshot；lease 的
# stdin write end 保留在本 shell，直到 verify 與 service active checks 完成。
cortex_acquire_maintenance_lease
trap 'cortex_abort_restore "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Lease ready 只會在 root helper 驗證 effective receipt 不存在、並把 service
# snapshot durable 寫入 private `/var/lib/cortex-installer` 後回傳。
for cortex_service in "${cortex_present_services[@]}"; do
  if ! /usr/bin/sudo /usr/bin/systemctl stop "$cortex_service"; then
    exit 1
  fi
done
test "$(cortex_cli_tree_sha)" = "$cortex_sealed_cli_tree_sha"

cortex_apply_attempted=1
/usr/bin/sudo /usr/bin/env -i HOME=/root \
  PATH="$cortex_bootstrap_root/venv/bin:/usr/bin:/bin" \
  LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONNOUSERSITE=1 \
  "$cortex_cli" install trust-root apply \
    --plan "$cortex_plan_path" \
    --confirm-sha256 "$cortex_confirmed_plan_sha" \
    --receipt "$cortex_receipt_path" \
    --maintenance-token "$cortex_maintenance_token" \
    "${cortex_prior_args[@]}"
```

不要在新 apply 成功後直接重啟舊服務；繼續完成 credential import、candidate activate 與
verify。從 maintenance lease 取得成功到 verify 完成之間，任何 command failure、shell
`EXIT`、`INT` 或 `TERM` 都會進入同一個 trap。所有 mutation command 都必須提交該 lease
產生、且綁定 reviewed plan 的 token；沒有 token 的直接 installer command 在 maintenance
window 內會 fail closed。trap 不依賴 apply child 返回後才更新的 shell flag：只要本 runbook
已嘗試 apply 且 receipt 存在，就 rollback 該 receipt（包含 child 已完成但 TERM 先抵達 shell
的窗口）。本次 receipt 在停服務前必須不存在，因此不會把舊的 applied/qualified receipt
誤當本次 transaction 回滾。lease 的 root-owned plan/token marker 只在 helper 正常結束時
清除；若 coproc 先死亡，marker 仍擋住所有新 lease 與 tokenless mutation，原 token 可繼續
rollback。只有 rollback 回報 `restore_safe=true` 才恢復原本 active 的 units；restore 後才以
exact token 清除可能殘留的 marker。

### 升級時 repository step 如何接手跑過 job 的來源樹（#1124）

repository step 安裝的 `<repo-source-tree>/<slug>` 同時是 Manager 的 `PSC_REPO_ROOT`。
Manager 擁有這棵樹，跑 job 時會在裡面留下 branch、ref 與 linked worktree（builder／gate
在 `PSC_WORKTREE_ROOT` 底下的 per-job clone 工作，不寫這裡）。`--prior-receipt` 升級時，
installer 把下表這些**執行期狀態**正規化後，再與 prior receipt 的 repository identity
（HEAD commit、origin URL、工作樹乾淨、`git fsck --strict`、owner／mode）比對；表外的任何
差異照舊 fail closed。每一列的 production 寫入點逐條列在
`paulsha_cortex/trust_root/install/backend.py` 的 `_RUNTIME_*` allowlist 註解。

| 類別 | 允許的形狀 | Manager 寫入點 |
| --- | --- | --- |
| local git config | `[user]` 的 `name`／`email`（單行純值） | per-job clone 從來源樹 local config 複製 commit identity |
| linked worktree | `.psc-review-worktrees/<name>`、`.psc-verification-worktrees/<name>`，detached；`.git/worktrees/<id>` 的 `gitdir` 指回同一路徑；目錄已刪的 prunable registry 也接受 | foreign review 與 verification base checkout |
| ref | `refs/heads/feature/*`、`refs/remotes/origin/*`、`refs/tags/archive/<work>-<sha8>`、`refs/cortex/{reclaimed,main-sync,main-sync-quarantine,mirror}/…` | provision `branch -f`、bundle 回收、`fetch origin <branch>`、封存 tag／ref、main-sync pin、Monitor git mirror |

`FETCH_HEAD`、object pack、reflog 等檔案不另列：它們與整棵樹（含上表的 linked worktree）
一樣受 owner／group、world-writable、symlink 逃逸檢查，這組檢查沒有任何放寬。刻意**不**
接受的有：`branch.<name>.*` upstream 設定（trust-root 的 Manager 以 exact commit 建 branch，
不會寫它；它只出現在 #623 之前的 legacy checkout，那棵樹由 #1122 的 legacy adoption
quarantine 後重新 clone）、上表以外位置的 linked worktree（包括來源樹外的 worktree，只看
registry 就拒收，不走訪那個目錄）、其他 config section／key（尤其 `core.hooksPath`、
`core.fsmonitor`、`include*`、`filter.*`、`alias.*`、`credential.*`、`url.*`）、其他 ref
namespace（例如會改寫 checkout 內容的 `refs/replace/*`），以及 owner 不是 repo owner 的檔案
（例如 operator 擁有）。拒收時錯誤訊息會點名項目，例如：

```text
existing managed object does not match prior receipt provenance: repository:<slug> (foreign owner: README.md is <account>:<group>, expected cortex-manager:cortex-manager)
```

處置：以 Manager 身分移除或還原點名的那一項後重跑 apply；不要放寬 allowlist 或手改
receipt。替換與 rollback 只動 main worktree 的 detached checkout 與 installer 自己的 origin
URL：fetch 不寫任何 ref 與 `FETCH_HEAD`、關閉 auto gc／maintenance、候選 commit 若追蹤
runtime worktree 根目錄即在 checkout 前拒絕、chmod 不進入 Manager 的 worktree 子樹，完成後
再確認 worktree／config／ref 摘要與替換前逐項相同，否則 fail closed 並 rollback。

**RC qualification 覆蓋範圍**：`qualification/run.sh` 目前只有單一 candidate 的 fresh
install、idempotent re-apply、functional drift 拒收與 rollback 後重裝，沒有 `--prior-receipt`
升級情境。升級需要第二份不同 plan 的 candidate（另一個 commit 的 hash-locked bundle），而
RC artifact 只帶本次 candidate；release profile 以 `--network none` 執行、容器內也沒有 checkout
可以另產 bundle；prior receipt 還必須先完整走完 activate／verify 成為 `qualified`。在 release
gate 內合成第二份 candidate 等於新增一段無法在 CI 之外實跑驗證的 harness，因此本次不加。
替代驗證是真 git repo 的回歸測試：`tests/test_trust_root_install_backend.py` 與
`tests/test_trust_root_install_transaction.py` 依上表出處重現 Manager 的寫入，走真
`LocalInstallBackend` 的 inspect／replace／rollback 與 `apply_plan(--prior-receipt)`／
`rollback_receipt` 全路徑，並覆蓋 allowlist 以外的 drift 被擋且點名。正式機升級前可先以
Manager 身分執行 `git -C <repo> worktree list --porcelain`、`git -C <repo> for-each-ref` 與
`git -C <repo> config --local --list`，對照上表確認沒有表外項目。

## 4. 明確匯入所需 credentials

只匯入 plan 的 `required_credentials` 列出的項目。預設 release／canary config 需要下列四個
source path；若 host overlay 將 builder executor 選為 AGY，plan 會再列出
`(builder, agy)`，此時必須另外指定第五個、與 reviewer 完全分離的 AGY source path。所有
source path 都必須由 operator 逐一指定到正確檔案；不要從任何 HOME 自動探索，也不要把
secret 值放進環境變數或命令列。

各 provider 的 source basename 由 installer 的 credential adapter 決定（allowlist，檔名不符
即拒絕），內容只驗結構、不記錄：

| principal／provider | source basename | 來源 | 安裝落點（該帳號 HOME 下） |
| --- | --- | --- | --- |
| builder／codex | `auth.json` | codex 登入後的 `$CODEX_HOME/auth.json` | `.codex/auth.json` |
| reviewer-planner／agy、builder／agy | `antigravity-oauth-token` | agy 1.2.11 登入後的 `~/.gemini/antigravity-cli/antigravity-oauth-token` | `cache/gemini/antigravity-cli/antigravity-oauth-token` |
| reviewer-planner／copilot | `config.json` | copilot 1.0.88 登入後的 `~/.copilot/config.json` | `.copilot/config.json` |
| manager／github | `hosts.yml` | gh 的 `~/.config/gh/hosts.yml` | `.config/gh/hosts.yml` |

- AGY 的 basename 與落點由 permgen 的 `ExecutorCredential("agy", token_leaf=...,
  cache_target="gemini")` 導出：`~/.gemini` 是指向 `cache/gemini` 的 symlink，installer
  直接寫實體目標，不跟隨 HOME 內的 symlink。舊的 gemini-cli `oauth_creds.json` 對 agy 1.2.11
  無效（authentication failed）。
- Copilot 1.0.88 不再讀 `~/.config/github-copilot/hosts.json`。installer 採 Copilot 自己的
  `config.json`（`copilotTokens`／`loggedInUsers`／`lastLoggedInUser`），不採 gh 的
  `hosts.yml`：job 帳號依 #666 刻意沒有 `~/.config/gh`（GitHub 寫入一律由 Manager 代理），
  放進 gh token 會讓 job 內的 `gh` 重新取得寫入通道；Manager 派 Copilot job 時也是把
  `PSC_COPILOT_OAUTH_CONFIG` 複製成 `$COPILOT_HOME/config.json`，兩條路徑是同一種檔。
  匯入要求 `copilotTokens` 為非空物件，錯誤訊息不含任何來源內容。建議來源只保留上述三個
  鍵，不要帶入其他 CLI 偏好設定。
- `antigravity-cli/` 與 `.copilot/` 是 CLI 自己的狀態目錄；缺少時 installer 以 0700 建立並
  交給該帳號擁有，既有時必須已屬該帳號，否則匯入失敗。

```bash
cortex_builder_codex_source=/absolute/operator-selected/path/auth.json
cortex_reviewer_agy_source=/absolute/operator-selected/path/antigravity-oauth-token
cortex_reviewer_copilot_source=/absolute/operator-selected/path/config.json
cortex_manager_github_source=/absolute/operator-selected/path/hosts.yml

cortex_root_cli install trust-root credentials import --receipt "$cortex_receipt_path" \
  --principal builder --provider codex --source "$cortex_builder_codex_source" \
  --maintenance-token "$cortex_maintenance_token"
cortex_root_cli install trust-root credentials import --receipt "$cortex_receipt_path" \
  --principal reviewer-planner --provider agy --source "$cortex_reviewer_agy_source" \
  --maintenance-token "$cortex_maintenance_token"
cortex_root_cli install trust-root credentials import --receipt "$cortex_receipt_path" \
  --principal reviewer-planner --provider copilot --source "$cortex_reviewer_copilot_source" \
  --maintenance-token "$cortex_maintenance_token"
cortex_root_cli install trust-root credentials import --receipt "$cortex_receipt_path" \
  --principal manager --provider github --source "$cortex_manager_github_source" \
  --maintenance-token "$cortex_maintenance_token"
```

若人工 review 確認 plan 的 `required_credentials` 額外列出 `(builder, agy)`，才另外執行
下列一筆 import。source basename 必須符合 AGY credential allowlist，且內容不可與
`reviewer-planner` 的登入態共用；若 plan 沒有這一列，這筆命令應保持不執行。

```bash
cortex_builder_agy_source=/absolute/operator-selected/path/antigravity-oauth-token
cortex_root_cli install trust-root credentials import --receipt "$cortex_receipt_path" \
  --principal builder --provider agy --source "$cortex_builder_agy_source" \
  --maintenance-token "$cortex_maintenance_token"
```

來源檔的保留或銷毀由其原本的 credential 管理流程決定；installer 只管理 receipt 記錄的
目的地，不宣稱擁有 operator 的來源檔。

## 5. Activate、verify、必要時 rollback

```bash
cortex_root_cli install trust-root activate \
  --receipt "$cortex_receipt_path" \
  --maintenance-token "$cortex_maintenance_token"
cortex_root_cli install trust-root verify \
  --receipt "$cortex_receipt_path" \
  --json \
  --evidence "$cortex_install_evidence" \
  --maintenance-token "$cortex_maintenance_token"

/usr/bin/sudo /usr/bin/systemctl is-active cortex-egress-proxy.service
/usr/bin/sudo /usr/bin/systemctl is-active cortex-manager.service
/usr/bin/sudo /usr/bin/systemctl is-active cortex-monitor.service
cortex_release_maintenance_lease complete
trap - EXIT INT TERM
```

`verify` 必須回傳 PASS，且三個 units 都必須是 `active`，才可宣稱這台主機已部署。package
release、RC container success 或靜態測試都不能代替這個 live 結論。

若一般 command failure／INT／TERM 發生，trap 會使用同一份 root-owned receipt 回滾；
rollback 只處理 receipt-owned state，偵測到未知 drift 時會保留並回報，需先人工裁決。

## 6. Hard-crash recovery

若整個 operator shell／lease helper 被 SIGKILL、OOM 或主機異常終止，**不要重跑第 1–2 節**：
immutable input／bootstrap venv 本來就會拒絕覆寫。改在 fresh shell 重新輸入先前由 operator
人工確認的 plan SHA，從停服務前已完整發布的 root-owned durable plan 直接恢復；不得重生
plan，也不得以掃描目錄猜 SHA。receipt path 與 service pre-state 則由 root helper 在停服務前
寫入 private、跨 reboot 保留的 plan-bound maintenance snapshot。以下唯一 recovery command
會先取得 host-global lock；若舊 helper
仍活著就拒絕，若只剩 stale marker 則以 exact reviewed plan 旋轉 token。它會先停止當下
存在的 Cortex units，再 rollback snapshot 綁定的 receipt；只在 `restore_safe=true` 時重啟
snapshot 記錄的原 active units，
最後才清 snapshot/marker：

```bash
set -euo pipefail
umask 077
PATH=/usr/bin:/bin
export PATH

cortex_installer_root=/var/lib/cortex-installer
cortex_bootstrap_root="$cortex_installer_root/0.1.13"
cortex_cli="$cortex_bootstrap_root/venv/bin/cortex"
read -r -p "Re-enter the previously reviewed plan SHA-256: " cortex_confirmed_plan_sha
test "${#cortex_confirmed_plan_sha}" -eq 64
case $cortex_confirmed_plan_sha in
  (*[!0-9a-f]*) exit 1 ;;
esac
cortex_plan_path="$cortex_installer_root/plans/$cortex_confirmed_plan_sha.json"

# 在 fresh shell 重新驗 durable plan 與 root-owned sealed CLI topology。這裡的
# 威脅模型是 unprivileged jobs；能改寫 root-owned installer storage 的 root actor
# 仍屬明確的人工裁決邊界。
/usr/bin/sudo /usr/bin/env -i HOME=/root PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
  PYTHONNOUSERSITE=1 /usr/bin/python3 -I -S - \
  "$cortex_plan_path" "$cortex_confirmed_plan_sha" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
expected = sys.argv[2]
observed = path.lstat()
if (
    not stat.S_ISREG(observed.st_mode)
    or observed.st_uid != 0
    or observed.st_nlink != 1
    or stat.S_IMODE(observed.st_mode) != 0o600
):
    raise SystemExit("durable reviewed plan is unsafe")
if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
    raise SystemExit("durable reviewed plan digest mismatch")
parent = path.parent.lstat()
if (
    not stat.S_ISDIR(parent.st_mode)
    or parent.st_uid != 0
    or stat.S_IMODE(parent.st_mode) != 0o700
):
    raise SystemExit("durable plan root is unsafe")
PY

cortex_recovery_cli_tree_sha() {
  /usr/bin/sudo /usr/bin/env -i HOME=/root PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    PYTHONNOUSERSITE=1 /usr/bin/python3 -I -S - "$cortex_bootstrap_root/venv" <<'PY'
import hashlib
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
digest = hashlib.sha256()
paths = [root, *sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix())]
for path in paths:
    relative = "." if path == root else path.relative_to(root).as_posix()
    observed = path.lstat()
    if observed.st_uid != 0 or stat.S_IMODE(observed.st_mode) & 0o022:
        raise SystemExit("unsafe candidate CLI ownership/mode")
    digest.update(relative.encode() + b"\0")
    digest.update(format(stat.S_IMODE(observed.st_mode), "04o").encode() + b"\0")
    if stat.S_ISDIR(observed.st_mode):
        digest.update(b"D\0")
    elif stat.S_ISREG(observed.st_mode) and observed.st_nlink == 1:
        digest.update(b"F\0" + hashlib.sha256(path.read_bytes()).digest())
    else:
        raise SystemExit("unsafe candidate CLI tree object")
print(digest.hexdigest())
PY
}
cortex_recovery_sealed_cli_tree_sha=$(cortex_recovery_cli_tree_sha)
cortex_root_cli() {
  test "$(cortex_recovery_cli_tree_sha)" = "$cortex_recovery_sealed_cli_tree_sha"
  /usr/bin/sudo /usr/bin/env -i HOME=/root \
    PATH="$cortex_bootstrap_root/venv/bin:/usr/bin:/bin" \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONNOUSERSITE=1 \
    "$cortex_cli" "$@"
}

cortex_root_cli install trust-root recover \
  --plan "$cortex_plan_path" \
  --confirm-sha256 "$cortex_confirmed_plan_sha"
```

`maintenance_recovered=true`、`restore_safe=true` 且 `services_restored` 符合 snapshot 才算
recovery 完成。若回報 retained unknown/drift，marker 與 snapshot 會保留，services 維持
stopped；若 service restore 本身失敗，流程會重新 stop 已嘗試恢復的 units 並保留 recovery
state，必須先人工裁決。不得改用 tokenless rollback 或手動刪 receipt。下一次正常執行會
產生新的 effective receipt nonce，因此已安全收斂的舊 receipt 可保留為稽核紀錄。

### 手動恢復 `cortex upgrade` 中斷的升級

`cortex upgrade` 中斷或停在原地（`halted`）時，第一步一律先執行
`sudo /opt/cortex/venv/bin/cortex upgrade --recover`：它從 maintenance snapshot（只剩 stale
lease marker 時改用 marker）取得 plan sha、核對 durable plan，再走上面同一個 `recover`。只有
它拒絕執行或回報要人工裁決時，才照上面的 snippet 手動恢復。這時 snippet 裡寫死的
`0.1.13/venv` 不適用：該次升級的 sealed CLI 在它自己的 attempt 目錄。每個變數改由該次升級的
report（`/var/lib/cortex-installer/<版本>/upgrade-report.json`，與
`/var/lib/cortex-installer/last-upgrade-report.json` 內容相同）對應：

| snippet 變數 | 該次 `cortex upgrade` 的來源 |
| --- | --- |
| `cortex_bootstrap_root` | report 的 `candidate.attempt_dir`（`/var/lib/cortex-installer/<版本>/attempt-*`），因此 `cortex_cli` 是 `<attempt_dir>/venv/bin/cortex` |
| `cortex_recovery_sealed_cli_tree_sha` | 照 snippet 算出後必須等於 report 的 `candidate.cli_tree_sha256`（同一套 tree digest）；不相等就停止，交給人工裁決 |
| `cortex_confirmed_plan_sha`（`read -r -p` 輸入） | report 的 `plan.sha256`；必須等於 `/var/lib/cortex-installer/maintenance-snapshot.json` 的 `plan_sha256`，沒有 snapshot 時則等於 `/run/paulsha-cortex-trust-root/maintenance.lock` marker 的 `plan_sha256` |
| `cortex_plan_path` | report 的 `plan.durable_path`，也就是 `/var/lib/cortex-installer/plans/<plan sha>.json` |
| 要 rollback 的 receipt | report 的 `receipt.path`；必須等於 snapshot 的 `receipt_path`。被 SIGKILL、OOM 或斷電打斷時，durable report 停在 `in-progress`、`receipt` 為 null（apply 之前的狀態才會落盤），這時只依 snapshot 的 `receipt_path`，其餘變數照舊取自同一份 report。`recover` 自己讀 snapshot，不需輸入 |

三者（report、snapshot 或 marker、durable plan）任何一處對不上，都不要硬湊變數執行，維持服務
停止並人工裁決。

## 7. Deployment canary

production host verify 與 protected GitHub deployment canary 是兩個 gate。canary 另外需要
四份 GitHub environment secrets 與三個非秘密 variables；workflow 會在 disposable
container 內安裝 exact wheel、跑完整 intake-to-closeout，並要求 `worktree-isolation`
確實由指定 Codex model 自主產生至少一筆成功且有輸出的 command event。沒有成功的 live
canary run 時，#716 必須維持 open。probe repository 的準備、secret 形狀與重置見
`docs/superpowers/runbooks/deployment-canary-probe.md`。
