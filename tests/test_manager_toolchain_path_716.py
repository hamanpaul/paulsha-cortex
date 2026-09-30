"""#716：system 部署的 Manager 找得到 toolchain 內的 openspec。

deployment canary 一路跑到 ship lane 才停：archive gate 以相對名呼叫
`openspec validate`／`openspec archive`，但 openspec 只以 tool tree 裝在 toolchain，
Manager unit 又沒有 `PATH`，systemd 的預設值不含 toolchain ⇒
`FileNotFoundError: 'openspec'`。修法是 Manager 的 EnvironmentFile 帶與 job 同形的
`PATH`（toolchain 最前面、系統尾段、不含 sbin）。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from paulsha_cortex.trust_root import permgen  # noqa: E402
from paulsha_cortex.trust_root.install import cli as install_cli  # noqa: E402
from test_trust_root_install_legacy_inventory import (  # noqa: E402
    _release_config,
    _write_bundle,
)


def _manager_environment(tmp_path: Path) -> dict[str, str]:
    bundle = _write_bundle(tmp_path)
    config = _release_config(tmp_path, bundle)
    output = tmp_path / "plan.json"
    assert install_cli.main(
        ["plan", "--config", str(config), "--bundle", str(bundle), "--output", str(output)]
    ) == 0
    plan = json.loads(output.read_text(encoding="utf-8"))
    contents = [
        step["content"]
        for step in plan["apply_order"]
        if isinstance(step, dict)
        and str(step.get("path", "")).endswith("/cortex-manager.env")
        and isinstance(step.get("content"), str)
    ]
    assert len(contents) == 1
    values: dict[str, str] = {}
    for line in contents[0].splitlines():
        key, _, raw = line.partition("=")
        values[key] = json.loads(raw)
    return values


def test_manager_environment_path_puts_toolchain_first(tmp_path: Path) -> None:
    values = _manager_environment(tmp_path)

    parts = values["PATH"].split(":")
    assert parts[0].endswith("/toolchain/bin")
    assert tuple(parts[1:]) == permgen.JOB_PATH_SYSTEM_TAIL
    assert not any(part.endswith("sbin") for part in parts)
    # Manager 與 job 解析到同一份受 qualification 的 toolchain。
    assert values["PATH"] == values["PSC_BUILDER_PATH"]


def test_manager_environment_disables_openspec_telemetry(tmp_path: Path) -> None:
    """#716：`--jitless` 下 openspec 的 telemetry（fetch→undici→wasm）必崩，Manager 關掉它。"""
    values = _manager_environment(tmp_path)

    assert values["DO_NOT_TRACK"] == "1"


def test_manager_path_resolves_toolchain_openspec_before_system_copy(tmp_path: Path) -> None:
    toolchain = tmp_path / "opt" / "toolchain" / "bin"
    system = tmp_path / "usr" / "bin"
    for directory in (toolchain, system):
        directory.mkdir(parents=True)
        tool = directory / "openspec"
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
        tool.chmod(0o755)
    layout = permgen.PathLayout(deploy_root=str(tmp_path / "opt"))

    assert layout.manager_path_value().split(":")[0] == str(toolchain)
    resolved = shutil.which(
        "openspec", path=":".join((layout.manager_path_value().split(":")[0], str(system)))
    )
    assert resolved == str(toolchain / "openspec")
