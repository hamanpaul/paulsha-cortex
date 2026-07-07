from __future__ import annotations

from typing import Mapping

from . import context
from .contract import PersonaContract


def render_contract_prompt(
    role: str,
    catalog: Mapping[str, PersonaContract] | None = None,
    overlay: Mapping[str, object] | None = None,
) -> str:
    """把 persona 契約 render 成確定性的 prompt 前言（派工強制點 ①）。

    宣告 role / allowed_phases / write_paths / effective_tools。
    未知 role 由 build_persona_context 冒泡 ValueError。
    """
    ctx = context.build_persona_context(role=role, catalog=catalog, overlay=overlay)

    allowed_phases = ", ".join(ctx["allowed_phases"]) or "(none)"
    write_paths = "\n".join(f"  - {p}" for p in ctx["write_paths"])
    effective_tools = "\n".join(f"  - {t}" for t in ctx["effective_tools"])

    return (
        f"[PERSONA CONTRACT — role: {ctx['role']} (v{ctx['version']})]\n"
        "你在本次派工中扮演上述角色，且 MUST 嚴守以下契約邊界：\n"
        f"- allowed_phases: {allowed_phases}\n"
        "- write_paths（僅可寫入下列 glob，越界視為違規）:\n"
        f"{write_paths}\n"
        "- effective_tools（僅可使用下列工具）:\n"
        f"{effective_tools}\n"
        "[END PERSONA CONTRACT]"
    )
