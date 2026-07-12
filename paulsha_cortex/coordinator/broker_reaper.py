"""操作員 broker cleanup：安全呼叫 `scripts/reap-codex-brokers.sh`。

偵測/回收邏輯的單一真相源是 `scripts/reap-codex-brokers.sh`；本模組只負責從 Python
組裝 dry-run / apply 參數，不重刻偵測邏輯。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

# package 內固化腳本：repo_root/paulsha_cortex/scripts/reap-codex-brokers.sh
DEFAULT_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reap-codex-brokers.sh"


def reap_orphan_brokers(
    *,
    apply: bool = False,
    cwd_root: Path | str | None = None,
    script_path: Path | str = DEFAULT_SCRIPT,
    runner=subprocess.run,
    timeout: float = 30.0,
) -> dict:
    """跑 reap 腳本回收孤兒 codex broker。

    janitor 不得破壞 tick：腳本不存在、執行失敗或逾時皆**不拋例外**，一律以回傳 dict 表狀態。
    回 ``{"ran": bool, ...}``；``ran=True`` 時另含 ``applied`` / ``returncode`` / ``output`` / ``stderr``。
    """
    script = Path(script_path)
    if not script.is_file():
        return {"ran": False, "reason": "script-not-found", "script": str(script)}
    cmd = ["bash", str(script)]
    if apply:
        if cwd_root is None:
            raise ValueError("cwd_root is required when apply=True")
        cmd.extend(["--apply", "--cwd-root", str(Path(cwd_root).resolve())])
    try:
        proc = runner(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except Exception as exc:  # subprocess 失敗 / 逾時：吞掉，janitor 不破 tick
        return {"ran": False, "reason": f"exec-error: {exc}"}
    return {
        "ran": True,
        "applied": apply,
        "returncode": proc.returncode,
        "output": (proc.stdout or "").strip(),
        "stderr": (getattr(proc, "stderr", "") or "").strip(),
    }
