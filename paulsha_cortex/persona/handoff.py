from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from . import contract


def write_manifest(path: str | Path, payload: Mapping[str, object]) -> Path:
    """序列化 handoff manifest 至 path，確保父目錄存在。

    寫入端不驗證（責任在呼叫者）；read 端為 fail-closed 信任邊界。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return target


def read_manifest(
    path: str | Path,
    catalog: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """讀回 handoff manifest 並 fail-closed 驗證。

    缺檔 → FileNotFoundError；非法 JSON / schema 不過 → ValueError。
    MUST NOT 回傳空或部分 manifest。
    """
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"handoff manifest 不存在: {source}")

    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"handoff manifest 解析失敗: {source}: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"handoff manifest 格式錯誤（非 object）: {source}")

    result = contract.validate_handoff_message(payload, catalog)
    if not result.ok:
        raise ValueError(f"handoff manifest schema 不合法: {result.errors}")

    return payload
