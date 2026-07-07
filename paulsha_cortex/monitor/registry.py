"""monitor 監控集 = manual project-cortex.yaml ⊍ hippo project-hippo.yaml。

讀共享檔為檔案契約，不引入上游 runtime import。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from paulsha_cortex.config import paths


def _default_hippo_path() -> Path:
    return paths.project_config_root() / "project-hippo.yaml"


@dataclass(frozen=True)
class ProjectEntry:
    path: Path
    name: str
    source: str


def load_hippo_projects(path: Path | None = None) -> list[ProjectEntry]:
    src = path or _default_hippo_path()
    if not src.exists():
        return []
    try:
        data = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"project-hippo.yaml 解析失敗：{src} ({exc})") from exc
    if not isinstance(data, dict):
        return []
    raw_projects = data.get("projects", []) or []
    if not isinstance(raw_projects, list):
        raise ValueError(f"project-hippo.yaml projects 必須是清單：{src}")
    entries: list[ProjectEntry] = []
    for index, project in enumerate(raw_projects):
        if not isinstance(project, dict):
            raise ValueError(f"project-hippo.yaml projects[{index}] 必須是 mapping：{src}")
        slug = str(project.get("slug") or "")
        roots = project.get("roots", []) or []
        if not isinstance(roots, list):
            raise ValueError(f"project-hippo.yaml projects[{index}].roots 必須是清單：{src}")
        for root_index, root in enumerate(roots):
            if not isinstance(root, str):
                raise ValueError(
                    f"project-hippo.yaml projects[{index}].roots[{root_index}] 必須是字串：{src}"
                )
            resolved = Path(str(root)).expanduser().resolve()
            entries.append(
                ProjectEntry(
                    path=resolved,
                    name=slug or resolved.name,
                    source="hippo",
                )
            )
    return entries


def merge_projects(
    manual: list[ProjectEntry],
    hippo: list[ProjectEntry],
) -> list[ProjectEntry]:
    seen: set[Path] = set()
    merged: list[ProjectEntry] = []
    for entry in [*manual, *hippo]:
        if entry.path in seen:
            continue
        seen.add(entry.path)
        merged.append(entry)
    return merged
