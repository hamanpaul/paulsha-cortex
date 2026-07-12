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
    lines: list[tuple[int, int, str]] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent % 2 != 0:
            raise YAMLError(f"unsupported indentation at line {lineno}")
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            continue
        lines.append((lineno, indent, stripped))
    if not lines:
        return {}

    def parse_block(index: int, indent: int):
        if index >= len(lines):
            return {}, index
        _, current_indent, stripped = lines[index]
        if current_indent != indent:
            raise YAMLError(f"unexpected indentation at line {lines[index][0]}")
        if stripped.startswith("- "):
            return parse_list(index, indent)
        return parse_mapping(index, indent)

    def parse_mapping(index: int, indent: int):
        result: dict[str, object] = {}
        i = index
        while i < len(lines):
            lineno, current_indent, stripped = lines[i]
            if current_indent < indent:
                break
            if current_indent > indent:
                raise YAMLError(f"unexpected indentation at line {lineno}")
            if stripped.startswith("- "):
                break
            if ":" not in stripped:
                raise YAMLError(f"expected mapping entry at line {lineno}")
            key, raw_value = stripped.split(":", 1)
            key = key.strip()
            if not key:
                raise YAMLError(f"empty key at line {lineno}")
            value_text = raw_value.strip()
            if value_text:
                result[key] = _parse_scalar(value_text)
                i += 1
                continue
            if i + 1 >= len(lines) or lines[i + 1][1] <= current_indent:
                result[key] = {}
                i += 1
                continue
            nested, i = parse_block(i + 1, lines[i + 1][1])
            result[key] = nested
        return result, i

    def parse_list(index: int, indent: int):
        result: list[object] = []
        i = index
        while i < len(lines):
            lineno, current_indent, stripped = lines[i]
            if current_indent < indent:
                break
            if current_indent != indent:
                raise YAMLError(f"unexpected indentation at line {lineno}")
            if not stripped.startswith("- "):
                break
            item_text = stripped[2:].strip()
            if not item_text:
                if i + 1 >= len(lines) or lines[i + 1][1] <= current_indent:
                    result.append(None)
                    i += 1
                    continue
                nested, i = parse_block(i + 1, lines[i + 1][1])
                result.append(nested)
                continue
            if ":" in item_text:
                key, raw_value = item_text.split(":", 1)
                key = key.strip()
                if not key:
                    raise YAMLError(f"empty key at line {lineno}")
                entry: dict[str, object] = {}
                value_text = raw_value.strip()
                if value_text:
                    entry[key] = _parse_scalar(value_text)
                    i += 1
                else:
                    if i + 1 >= len(lines) or lines[i + 1][1] <= current_indent:
                        entry[key] = {}
                        i += 1
                    else:
                        nested_value, i = parse_block(i + 1, lines[i + 1][1])
                        entry[key] = nested_value
                if i < len(lines) and lines[i][1] > current_indent:
                    nested_extra, i = parse_block(i, lines[i][1])
                    if isinstance(nested_extra, dict):
                        entry.update(nested_extra)
                    else:
                        raise YAMLError(f"list item mapping expected dict continuation at line {lines[i - 1][0]}")
                result.append(entry)
                continue
            result.append(_parse_scalar(item_text))
            i += 1
        return result, i

    parsed, next_index = parse_block(0, lines[0][1])
    if next_index != len(lines):
        raise YAMLError(f"unexpected trailing content at line {lines[next_index][0]}")
    return parsed
