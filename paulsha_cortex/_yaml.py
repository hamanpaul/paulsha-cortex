from __future__ import annotations

import ast


class YAMLError(ValueError):
    """Subset YAML parser error for zero-dependency runtime paths."""


def _parse_scalar(raw: str):
    if raw in {"null", "Null", "NULL", "~"}:
        return None
    if raw in {"true", "True"}:
        return True
    if raw in {"false", "False"}:
        return False
    if raw.startswith("["):
        if not raw.endswith("]"):
            raise YAMLError(f"malformed inline list: {raw}")
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        try:
            return ast.literal_eval(raw)
        except (SyntaxError, ValueError) as exc:
            raise YAMLError(f"invalid quoted scalar: {raw}") from exc
    if raw.isdigit():
        return int(raw)
    return raw


def safe_load(text: str):
    root: dict[str, object] = {}
    stack: list[tuple[int, dict[str, object]]] = [(-2, root)]

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent % 2 != 0:
            raise YAMLError(f"unsupported indentation at line {lineno}")
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            continue
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        if indent > stack[-1][0] + 2:
            raise YAMLError(f"unexpected indentation at line {lineno}")
        if ":" not in stripped:
            raise YAMLError(f"expected mapping entry at line {lineno}")
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        if not key:
            raise YAMLError(f"empty key at line {lineno}")
        value_text = raw_value.strip()
        container = stack[-1][1]
        if not value_text:
            nested: dict[str, object] = {}
            container[key] = nested
            stack.append((indent, nested))
            continue
        container[key] = _parse_scalar(value_text)
    return root
