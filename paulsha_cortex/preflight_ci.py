"""`PSC_PREFLIGHT_CMD` 的 typed-argv 進入點：cortex 的 preflight 契約 → 治理引擎。

#661。這個模組取代舊值 `~/.local/bin/cortex-preflight-ci`——那是一個 shell wrapper，
內容再指向另一個 repo 的 shell script（`custom-skills/preflight-ci/scripts/preflight.sh`），
**兩層都在 `/home` 底下**，job／服務 unit 的 `ProtectHome=yes` 之後都不可達。

## 為什麼是「模組」而不是「把那棵樹搬進部署樹」

#640 對四個 executor 的解法是搬進 `<deploy_root>/toolchain`，因為它們是**自帶內容的
獨立程式**：搬過去就是同一支程式。preflight 不是——它是一條**跨多個外部程式的管線**
（治理引擎、`openspec`、`pytest`、`gh`、`git`）。把那兩層 shell 搬進部署樹只會讓
「登記表沒涵蓋到的外部相依」從一個變成五個，正好是 #661 要收掉的那個洞。

而且**前提在調查中就過期了**：`preflight-ci` 那支 shell script 的功能，
`paulsha-conventions` 自 1.0.17 起已經以 **typed-argv 的 python 模組**上游化為
``policy_check.preflight``（發行版 `policy-check` 的 `policy-preflight` console
script）。因此正確的形態就是 doctor 一直在建議的那一個：

    PSC_PREFLIGHT_CMD="<deploy_root>/venv/bin/python3 -m paulsha_cortex.preflight_ci"

落點是**既有的部署 venv**（root-owned、job／服務唯讀＋可執行，與 `executor-toolchain`
同一類），不必新增任何檔案系統資產：本模組隨 cortex 自己進 venv，backend 則是往同一個
venv `pip install 'policy-check==<policy_version>'`（唯一相依 PyYAML 已在裡面）。

## 這個模組只做一件事：翻譯

cortex 的契約（`coordinator/preflight.build_preflight_argv`）是

    <PSC_PREFLIGHT_CMD> (--pr <N> | --metadata <絕對路徑>) [--skip-tests]

引擎的契約是 `--pr-title` / `--pr-body-file` / `--pr-labels` / `--base` / `--skip-tests`。
中間差的就是「PR metadata 從哪來、怎麼餵」——那正是舊 wrapper 那 60 行 shell 做的事。
這裡用 typed argv 重寫同一件事，不多做。

## 為什麼餵給 backend 的是 `--offline` ＋ 手動上下文，而不是 `--pr <N>`

引擎的 `--pr` 模式會自己叫 `gh`，看起來更短，但它同時把**引擎解析**推進網路路徑：
`--offline` 與 `--pr` 互斥，於是引擎會依 workflow pin 的 SHA 去 clone 一份引擎原始碼
到 cache 目錄，**再執行它**。在降權部署裡那等於「服務帳號寫得到、又執行得到」的
一塊執行面——正是 Tier-0 明文要封的那種東西（spec §R3：executable plane 必須 root
擁有、對 headless 不可寫）。

`--offline` 這條路則是：引擎解析落在**已安裝的那一份**，且引擎自己會驗
`installed == .project-policy.yml 的 policy_version`，對不上就 fail-closed。CI 端
R-23 另外釘住「workflow pin == policy_version」，兩條合起來遞移出「本機跑的引擎版本
== CI 跑的引擎版本」——**沒有網路、沒有 cache、沒有可寫的執行面**。

`gh` 仍會被用到（`--pr <N>` 時由**本模組**去讀 PR metadata），但那只讀資料、不取程式碼。

## 已知且刻意的近似：`head`

引擎會檢查 `--head` 與實際 checkout 的分支相符，因此 head 一律由 checkout 導出，而不是
用 PR 的 `headRefName`（ship 前的 exact-Candidate 檢查跑在一個臨時 checkout 上，分支名
是 `feature/preflight-<sha>-<hex>`）。**這與被取代的那支 shell script 逐字相同**
（它同樣是 `BRANCH=$(git rev-parse --abbrev-ref HEAD)`，只從 gh 取 title/body/labels/base），
不是本次新引入的落差。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Sequence

Runner = Callable[..., object]

#: backend：治理引擎自己的 CI-parity preflight 進入點。**本模組不 import 它**——
#: 只以 typed argv spawn，沒安裝時由 python 自己回非零並印出可操作訊息。
BACKEND_MODULE = "policy_check.preflight"

#: 讀 PR metadata 用的外部程式（登記表 `SYSTEM_PROGRAMS` 有登記；只讀資料，不取程式碼）。
GH_EXECUTABLE = "gh"

EXIT_USAGE = 2


class PreflightAdapterError(ValueError):
    """輸入不合契約（exit 2）。與引擎的 usage error 同一個退出碼。"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cortex-preflight-ci",
        description="cortex delivery preflight 契約 → paulsha-conventions CI-parity preflight",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pr", help="既有 PR 編號（正整數）")
    source.add_argument("--metadata", help="PR metadata JSON 的絕對路徑")
    parser.add_argument("--skip-tests", action="store_true")
    return parser


def _run(argv: Sequence[str], *, cwd: Path, runner: Runner) -> subprocess.CompletedProcess:
    result = runner(
        list(argv),
        cwd=str(cwd),
        shell=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    returncode = getattr(result, "returncode", None)
    if not isinstance(returncode, int):
        raise PreflightAdapterError("runner returned no integer returncode")
    return result


def load_metadata(path: str) -> tuple[str, str, tuple[str, ...]]:
    """讀 cortex 寫出來的 PR metadata（`work_bridge._metadata_file`）。

    逐條驗型別，與被取代的 shell wrapper 的 `jq -er` 判準相同：絕對路徑、regular
    non-symlink、`title` 非空字串、`body` 字串、`labels` 為字串陣列。
    """

    candidate = Path(path)
    if not candidate.is_absolute():
        raise PreflightAdapterError("--metadata 必須是絕對路徑")
    if candidate.is_symlink() or not candidate.is_file():
        raise PreflightAdapterError("--metadata 必須是 regular non-symlink file")
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreflightAdapterError(f"--metadata 讀不到或不是合法 JSON: {candidate}") from exc
    if not isinstance(payload, dict):
        raise PreflightAdapterError("--metadata 內容必須是 JSON object")
    title = payload.get("title")
    body = payload.get("body")
    labels = payload.get("labels", [])
    if not isinstance(title, str) or not title.strip():
        raise PreflightAdapterError("--metadata 的 title 必須是非空字串")
    if not isinstance(body, str):
        raise PreflightAdapterError("--metadata 的 body 必須是字串")
    if not isinstance(labels, list) or any(
        not isinstance(label, str) or not label.strip() for label in labels
    ):
        raise PreflightAdapterError("--metadata 的 labels 必須是非空字串陣列")
    return title.strip(), body, tuple(label.strip() for label in labels)


def load_pull_request(
    number: str, *, cwd: Path, runner: Runner
) -> tuple[str, str, tuple[str, ...], str]:
    """以 `gh` 讀既有 PR 的 title／body／labels／base（只讀資料，不取程式碼）。"""

    if not number.isdigit() or int(number) <= 0:
        raise PreflightAdapterError("--pr 必須是正整數")
    result = _run(
        [
            GH_EXECUTABLE,
            "pr",
            "view",
            number,
            "--json",
            "title,body,labels,baseRefName",
        ],
        cwd=cwd,
        runner=runner,
    )
    if result.returncode != 0:
        # 刻意不回吐 stderr：它可能含 token 提示字串，而 doctor／journal 的既有規範
        # 是「命令輸出一律不進診斷訊息」。
        raise PreflightAdapterError(f"gh pr view {number} 失敗（rc={result.returncode}）")
    try:
        payload = json.loads(getattr(result, "stdout", "") or "")
    except json.JSONDecodeError as exc:
        raise PreflightAdapterError("gh pr view 回傳的不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise PreflightAdapterError("gh pr view 回傳的不是 JSON object")
    title = payload.get("title")
    body = payload.get("body") or ""
    raw_labels = payload.get("labels") or []
    base = payload.get("baseRefName")
    if not isinstance(title, str) or not title.strip():
        raise PreflightAdapterError("PR title 為空")
    if not isinstance(body, str):
        raise PreflightAdapterError("PR body 型別錯誤")
    if not isinstance(base, str) or not base.strip():
        raise PreflightAdapterError("PR baseRefName 為空")
    if not isinstance(raw_labels, list):
        raise PreflightAdapterError("PR labels 型別錯誤")
    labels: list[str] = []
    for entry in raw_labels:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise PreflightAdapterError("PR label 型別錯誤")
        labels.append(name.strip())
    return title.strip(), body, tuple(labels), base.strip()


def build_backend_argv(
    *,
    repo_root: Path,
    body_file: Path,
    title: str,
    labels: Sequence[str],
    base: str | None,
    skip_tests: bool,
    python: str | None = None,
) -> list[str]:
    """組出 backend 的 typed argv。

    `--offline` 是**刻意且必要**的（見模組 docstring）：它把引擎解析釘在已安裝的那一
    份並要求版本等於 `policy_version`，換掉「執行期 clone 一份引擎再執行它」那條路。

    `--head` 刻意不傳：引擎會要求它等於實際 checkout 的分支，因此由引擎自己導出才
    不會有兩個真相。
    """

    argv = [
        python or sys.executable,
        "-m",
        BACKEND_MODULE,
        "--repo",
        str(repo_root),
        "--offline",
        "--pr-title",
        title,
        "--pr-body-file",
        str(body_file),
        "--pr-labels",
        ",".join(labels),
    ]
    if base:
        argv += ["--base", base]
    if skip_tests:
        argv.append("--skip-tests")
    return argv


def main(argv: Sequence[str] | None = None, *, runner: Runner = subprocess.run) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo_root = Path.cwd().resolve()
    try:
        if args.metadata is not None:
            title, body, labels = load_metadata(args.metadata)
            base: str | None = None
        else:
            title, body, labels, base = load_pull_request(
                args.pr, cwd=repo_root, runner=runner
            )
    except PreflightAdapterError as exc:
        print(f"PREFLIGHT ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    with tempfile.TemporaryDirectory(prefix="cortex-preflight-ci-") as temp:
        body_file = Path(temp) / "pr-body.md"
        body_file.write_text(body, encoding="utf-8")
        backend = build_backend_argv(
            repo_root=repo_root,
            body_file=body_file,
            title=title,
            labels=labels,
            base=base,
            skip_tests=args.skip_tests,
        )
        try:
            completed = runner(list(backend), cwd=str(repo_root), shell=False)
        except FileNotFoundError:
            print(
                "PREFLIGHT ERROR: 找不到 python interpreter——`PSC_PREFLIGHT_CMD` 應指向"
                "部署 venv 的絕對路徑。",
                file=sys.stderr,
            )
            return EXIT_USAGE
    returncode = getattr(completed, "returncode", None)
    if not isinstance(returncode, int):
        print("PREFLIGHT ERROR: backend 沒有回傳整數退出碼", file=sys.stderr)
        return EXIT_USAGE
    if returncode != 0:
        # backend 未安裝時 python 會以 rc=1 印 `No module named policy_check`——
        # 訊息本身已可操作，這裡只補「該裝到哪裡」。
        print(
            f"PREFLIGHT: backend `{BACKEND_MODULE}` 退出碼 {returncode}。"
            "若訊息是 No module named policy_check，請往**部署 venv** 安裝與 "
            ".project-policy.yml 的 policy_version 逐字相同的 `policy-check`。",
            file=sys.stderr,
        )
    return returncode


if __name__ == "__main__":  # pragma: no cover - 進入點
    raise SystemExit(main())
