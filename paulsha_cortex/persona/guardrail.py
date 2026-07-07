from __future__ import annotations

from dataclasses import dataclass
import fnmatch
from pathlib import PurePosixPath
from typing import Mapping

from .contract import PERSONA_CATALOG, PersonaContract


@dataclass(frozen=True)
class GuardrailDecision:
    allowed: bool
    rule_id: str
    reason: str


class PersonaGuardrail:
    def __init__(self, catalog: Mapping[str, PersonaContract] | None = None) -> None:
        self._catalog = dict(catalog or PERSONA_CATALOG)

    def evaluate_filesystem(self, *, role: str, path: str) -> GuardrailDecision:
        persona = self._catalog.get(role)
        if persona is None:
            return GuardrailDecision(
                allowed=False,
                rule_id="unknown-role",
                reason=f"role {role} is not registered in persona catalog",
            )

        normalized_path = _normalize_path(path)
        for pattern in persona.write_paths:
            if fnmatch.fnmatch(normalized_path, pattern):
                return GuardrailDecision(
                    allowed=True,
                    rule_id="filesystem-allow",
                    reason=f"path {normalized_path} allowed by persona write scope",
                )

        return GuardrailDecision(
            allowed=False,
            rule_id="filesystem-scope",
            reason=f"path {normalized_path} outside persona write scope",
        )

    def evaluate_tool(self, *, role: str, tool: str) -> GuardrailDecision:
        persona = self._catalog.get(role)
        if persona is None:
            return GuardrailDecision(
                allowed=False,
                rule_id="unknown-role",
                reason=f"role {role} is not registered in persona catalog",
            )

        normalized_tool = _normalize_tool(tool)
        for allowed_tool in persona.allowed_tools:
            candidate = allowed_tool.lower()
            if normalized_tool == candidate or normalized_tool.startswith(f"{candidate} "):
                return GuardrailDecision(
                    allowed=True,
                    rule_id="tool-allow",
                    reason=f"tool {tool} allowed for role {role}",
                )

        return GuardrailDecision(
            allowed=False,
            rule_id="tool-allowlist",
            reason=f"tool {tool} outside allowlist for role {role}",
        )


def _normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    return PurePosixPath(normalized).as_posix().lstrip("./")


def _normalize_tool(tool: str) -> str:
    return " ".join(tool.strip().split()).lower()
