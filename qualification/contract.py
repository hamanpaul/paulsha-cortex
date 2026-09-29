#!/usr/bin/env python3
"""Release qualification／deployment canary 的 toolchain 與模型單一真相。

兩條 workflow（`--github-env`）、容器內的 driver／validator，以及 canary 的 model
identity overlay（`--model-identity-overlay`）都由本檔導出；其他地方不得再寫第二份
版本、integrity 或模型字面值。本檔刻意只用標準函式庫：GitHub runner 與 host 端的
`validate.py` 執行時沒有安裝 `paulsha_cortex`。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Mapping


TOOLCHAIN: Mapping[str, Mapping[str, str]] = {
    "codex": {
        "version": "0.157.1",
        "npm_package": "@openai/codex@0.157.1-linux-x64",
        "integrity": "sha512-Eac8XlC0nCXSeUjDU9l8yLJ6P9evv1mO+AnvILoNwlegBC7B3AVXqJ05QcMhQX7RcJ3Lk2618ykCb2X2ui8VAQ==",
        # 整個 bin 目錄以 tree 安裝（#716）：0.157 起 `features.code_mode_host`
        # （stable、預設開）要求 `codex-code-mode-host` 與 codex 同目錄，只裝單檔
        # codex 時 job 內每個 tool call 都因 host 不存在而失敗。
        "archive_dir": "package/vendor/x86_64-unknown-linux-musl/bin",
        "entrypoint": "codex",
    },
    "claude": {
        "version": "2.1.239",
        "npm_package": "@anthropic-ai/claude-code-linux-x64@2.1.239",
        "integrity": "sha512-glj5zramK4ebmNa0EVx1iAtBfWHKnZ1q0Ti+1veMR4GHR9ttSranQoMIsLIuJUdRX/hErJnArjwe9hanuxE+dg==",
        "archive_path": "package/claude",
    },
    "copilot": {
        "version": "1.0.88",
        "npm_package": "@github/copilot-linux-x64@1.0.88",
        "integrity": "sha512-wNigl8rqixvtYoRtygNf1NQIXx77MWC6XyqoV7fcfvSwWI3GJCltnczaNLbC611K9Hex39KyiUR5ZmGhQ+LzLw==",
        "archive_path": "package/copilot",
    },
    "agy": {
        "version": "1.2.11",
        "url": "https://github.com/google-antigravity/antigravity-cli/releases/download/1.2.11/agy_cli_linux_x64.tar.gz",
        "sha256": "c91c62c5e6fa954f5a7e1d7b9ad417d749db4aa60a4ba0b3d604dec1b645d190",
        "archive_path": "antigravity",
    },
    "srt": {
        "version": "0.0.73",
        "npm_package": "@anthropic-ai/sandbox-runtime@0.0.73",
        "integrity": "sha512-F608iUirrCqwvInZYGRRgJWDQj0tt6fNVE9aPagpotLJ5LhC4JbrMFIIZww5MFjb+HRCkpE0+xdI79c30tdVYg==",
        "entrypoint": "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js",
    },
    "openspec": {
        "version": "1.10.0",
        "npm_package": "@fission-ai/openspec@1.10.0",
        "integrity": "sha512-fuL3Rz7Jv+NnHeUM1XkbaXFo4bUdPttOWOC66/6SyfJr9rPOvGE47oBp+8XdDtPiiWZawa0Z9RDzGasetFu2eQ==",
        "entrypoint": "node_modules/@fission-ai/openspec/bin/openspec.js",
    },
}

#: 部署 venv 需要、但不是 cortex 相依的 python 發行版（permgen
#: `DEPLOYMENT_PYTHON_DISTRIBUTIONS`）。`policy-check` 是 ship preflight 的 backend
#: （`PSC_PREFLIGHT_CMD` → `paulsha_cortex.preflight_ci` → `policy_check.preflight
#: --offline`），版本必須逐字等於 probe `.project-policy.yml` 的 `policy_version`。
#: 來源是 paulsha-conventions 該版 release workflow 發行的 cp312 runtime bundle：
#: archive 與其中 wheel 各以 sha256 釘住，`release_commit` 等於 R-23 的 engine pin。
#: workflow 以 `WHEEL_<NAME>_<FIELD>` 取用；wheel 放進 wheelhouse 後隨 bundle 進部署 venv，
#: run.sh 的系統層安裝也涵蓋 Manager 以相對名 `python3 -m policy_check` 跑的 gate。
WHEELS: Mapping[str, Mapping[str, str]] = {
    "policy_check": {
        "version": "1.0.17",
        "release_commit": "9e7fabbf0b5eea9ad933fa6798764b723934a0b7",
        "url": "https://github.com/hamanpaul/paulsha-conventions/releases/download/v1.0.17/paulsha-conventions-v1.0.17-cp312.tar.gz",
        "sha256": "6e9e31e40e200509de244ba578f06cfce8d75325577cba3ecb23b830adc630e8",
        "archive_path": "paulsha-conventions-v1.0.17/wheels/policy_check-1.0.17-py3-none-any.whl",
        "wheel_sha256": "41cf79f49a354db9216e11ab4e3a1727e90afd1e6e56bf893cecce38274e66a4",
    },
}

#: reference image 以 apt 釘版本安裝的系統層套件（Ubuntu 24.04 release pocket 的版本）。
#: `qualification/Dockerfile` 逐字鏡射這張表（測試釘住）；driver 在 intake 前以 gate／
#: Manager 身分驗證它們真的可用。
#: - pytest（與它的三個 python 相依）：`PSC_GATE_CMD_PYTEST="python3 -m pytest -q"` 由
#:   gate 身分以系統層 `/usr/bin/python3` 執行（permgen `SYSTEM_PYTHON_DISTRIBUTIONS`）。
#: - universal-ctags：policy-check 帶 PR 上下文時 R-22 以 ctags 做 symbol 分析，缺它
#:   policy 階段直接 RuntimeError（conventions runtime bundle 的 prerequisites 也列它）。
IMAGE_APT_PINS: Mapping[str, str] = {
    "python3-iniconfig": "1.1.1-2",
    "python3-packaging": "24.0-1",
    "python3-pluggy": "1.4.0-1",
    "python3-pytest": "7.4.4-1",
    "universal-ctags": "5.9.20210829.0-1",
}

#: probe gate 實際跑到的 pytest 上游版本（`python3 -m pytest --version`）。
PROBE_GATE_PYTEST_VERSION = "7.4.4"

#: probe checkout 的本地 git identity。容器 hostname 沒有網域，git 自動推導的 email 會是
#: `<user>@<host>.(none)` 而拒絕 commit；`coordinator/seams.py` 把來源 repo 的本地
#: `user.name`／`user.email` 複製進 per-job clone，Manager 的 archive commit 也在同一族
#: checkout 上。`.invalid` 是 RFC 2606 保留網域，不會對應到任何真實帳號。
CANARY_GIT_IDENTITY: Mapping[str, str] = {
    "name": "Cortex deployment canary",
    "email": "cortex-deployment-canary@example.invalid",
}

#: provider smoke 要求的 (model, effort) 與執行帳號；operator 2026-09-26 實測可用。
#: codex 的 effort 另由 production `launcher._codex_default_effort()` 對同一模型發出，
#: 兩者一致由測試釘住（此檔不能 import `paulsha_cortex`）。
PROVIDERS: Mapping[str, Mapping[str, str]] = {
    "agy": {"model_id": "gemini-3.8-flash", "effort": "high", "account": "cortex-reviewer-planner"},
    "copilot": {"model_id": "gpt-5.4", "effort": "xhigh", "account": "cortex-reviewer-planner"},
    "codex": {"model_id": "gpt-6-luna", "effort": "max", "account": "cortex-builder"},
}

#: canary 派工的 builder：`--builder-executor codex --builder-model gpt-6-luna`。
CANARY_BUILDER: Mapping[str, object] = {
    "provider": "codex",
    "independence_domain": "openai",
    "capabilities": ("build",),
}
#: canary 的 planner／reviewer：必須與 builder 不同 independence domain，且容器內
#: `cortex-reviewer-planner` 有它的憑證。copilot 不行：packaged roster 的 gpt-5.4
#: 屬 openai domain，且 hardened 模式下 copilot 沒有 planner／reviewer toolchain grant。
#: AGY identity id 以 `<model>-<effort>` 表達 effort（launcher 原樣發 `--model <id>`）；
#: planning 能力依 roster 規則必須帶 google domain 與 `agy-plan-sandbox` live probe。
CANARY_REVIEWER: Mapping[str, object] = {
    "provider": "agy",
    "independence_domain": "google",
    "capabilities": ("planning", "review"),
    "live_probe": "agy-plan-sandbox",
}


def canary_identity(role: Mapping[str, object]) -> tuple[str, str]:
    """回傳 canary 身分的 `(executor, model_id)`，由 PROVIDERS 導出。"""

    provider = str(role["provider"])
    row = PROVIDERS[provider]
    if provider == "agy":
        return provider, f"{row['model_id']}-{row['effort']}"
    return provider, row["model_id"]


def model_identity_overlay() -> dict[str, object]:
    """canary 容器的 host overlay：只列 canary 用得到、且有憑證的兩個身分。

    `packaged_fallback: deny` 讓解析不會落到 packaged roster 裡容器沒有憑證的身分
    （claude、codex spark、agy 3.1-pro）。
    """

    rows: list[dict[str, object]] = []
    for role in (CANARY_BUILDER, CANARY_REVIEWER):
        executor, model_id = canary_identity(role)
        row: dict[str, object] = {
            "executor": executor,
            "model_id": model_id,
            "independence_domain": role["independence_domain"],
            "capabilities": list(role["capabilities"]),  # type: ignore[arg-type]
        }
        if "live_probe" in role:
            row["live_probe"] = role["live_probe"]
        rows.append(row)
    return {
        "schema_version": 3,
        "resolution_policy": {"packaged_fallback": "deny"},
        "identities": rows,
    }


def render_model_identity_overlay() -> str:
    """以 block YAML 輸出 overlay（純量以 JSON 字面值表示，仍是合法 YAML）。"""

    payload = model_identity_overlay()
    policy = payload["resolution_policy"]
    assert isinstance(policy, Mapping)
    lines = [
        f"schema_version: {payload['schema_version']}",
        "resolution_policy:",
        f"  packaged_fallback: {json.dumps(policy['packaged_fallback'])}",
        "identities:",
    ]
    rows = payload["identities"]
    assert isinstance(rows, list)
    for row in rows:
        lines.append(f"  - executor: {json.dumps(row['executor'])}")
        for key in ("model_id", "independence_domain"):
            lines.append(f"    {key}: {json.dumps(row[key])}")
        lines.append(
            "    capabilities: ["
            + ", ".join(json.dumps(value) for value in row["capabilities"])
            + "]"
        )
        if "live_probe" in row:
            lines.append(f"    live_probe: {json.dumps(row['live_probe'])}")
    return "\n".join(lines) + "\n"


def github_environment() -> dict[str, str]:
    """把釘選的 toolchain／wheel 攤平成 GitHub Actions 環境變數。

    toolchain 為 `TOOL_<NAME>_<FIELD>`，部署 venv 的額外 wheel 為 `WHEEL_<NAME>_<FIELD>`。
    """

    values: dict[str, str] = {}
    for kind, table in (("TOOL", TOOLCHAIN), ("WHEEL", WHEELS)):
        for name, row in table.items():
            prefix = f"{kind}_{name.upper()}_"
            for field, value in row.items():
                values[prefix + field.upper()] = value
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--github-env", type=Path)
    target.add_argument("--model-identity-overlay", type=Path)
    args = parser.parse_args()
    if args.github_env is not None:
        with args.github_env.open("a", encoding="utf-8") as stream:
            for name, value in github_environment().items():
                if "\n" in name or "\n" in value:
                    raise ValueError("qualification contract values must be single-line")
                stream.write(f"{name}={value}\n")
        return 0
    descriptor = os.open(
        args.model_identity_overlay,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o644,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(render_model_identity_overlay())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
