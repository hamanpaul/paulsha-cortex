from __future__ import annotations

import ast
import hashlib
import json
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Mapping, Sequence

from ._yaml import YAMLError, safe_load

_TOP_LEVEL_KEYS = frozenset(
    {
        "version",
        "wrappers",
        "protected_paths",
        "protected_sudoers_paths",
        "block_device_prefixes",
        "protected_refs",
        "rules",
    }
)
_OPERATOR_OVERLAY_KEYS = frozenset(
    {
        "add_protected_paths",
        "add_protected_sudoers_paths",
        "add_block_device_prefixes",
        "add_protected_refs",
    }
)
_RULE_KEYS = frozenset({"id", "commands", "category", "severity", "summary"})
_SUPPORTED_RULE_IDS = frozenset(
    {
        "protected-root-destruction",
        "account-mutation",
        "block-device-clobber",
        "repository-destruction",
    }
)
_IMPLEMENTED_RULE_COMMANDS = {
    "protected-root-destruction": frozenset({"find", "python", "rm"}),
    "account-mutation": frozenset({"adduser", "echo", "passwd", "printf", "tee", "useradd"}),
    "block-device-clobber": frozenset({"dd"}),
    "repository-destruction": frozenset({"gh", "git"}),
}
_ENV_CLUSTER_VALUE_OPTIONS = frozenset({"C", "S", "u"})
_ENV_LONG_VALUE_OPTIONS = frozenset({"--chdir", "--split-string", "--unset"})
_SEGMENT_SEPARATORS = frozenset({"|", "||", "&", "&&", ";", "|&", "\n"})
_FORCE_PUSH_FLAGS = frozenset({"-f", "--force", "--force-with-lease"})
_GH_VALUE_OPTIONS = frozenset({"-R", "--repo", "--hostname"})
_GIT_GLOBAL_VALUE_OPTIONS = frozenset(
    {"-C", "-c", "--config-env", "--exec-path", "--git-dir", "--namespace", "--super-prefix", "--work-tree"}
)
_HELP_OR_VERSION_FLAGS = frozenset({"--help", "--version"})
_REFS_HEADS_PREFIX = "refs/heads/"
_REDIRECTION_OPERATORS = frozenset({">", ">>", "1>", "1>>"})
_SHELL_ARGV0 = frozenset({"sh", "bash"})
_SHELL_INLINE_COMMAND_OPTIONS = frozenset({"-c"})
_SUDO_LONG_VALUE_OPTIONS = frozenset(
    {"--chdir", "--chroot", "--group", "--host", "--prompt", "--role", "--type", "--user"}
)
_SUDO_VALUE_OPTIONS = frozenset({"-C", "-D", "-R", "-g", "-h", "-p", "-r", "-t", "-T", "-u"})
_SUDO_VALUE_OPTION_CHARS = frozenset(token[1:] for token in _SUDO_VALUE_OPTIONS)
_SUPPORTED_WRAPPERS = frozenset(
    {"command", "env", "eval", "nohup", "sudo", "timeout", "xargs"}
)
_ENV_VALUE_OPTIONS = frozenset({"-C", "-S", "-u"})
_TIMEOUT_VALUE_OPTIONS = frozenset({"-k", "-s", "--kill-after", "--signal"})
_XARGS_LONG_VALUE_OPTIONS = frozenset(
    {
        "--arg-file",
        "--delimiter",
        "--eof",
        "--max-args",
        "--max-chars",
        "--max-lines",
        "--max-procs",
        "--process-slot-var",
        "--replace",
    }
)
_XARGS_SHORT_VALUE_OPTIONS = frozenset({"a", "d", "E", "I", "L", "n", "P", "s"})
_MAX_NESTED_SHELL_DEPTH = 16


class CommandPolicyError(ValueError):
    """Raised when command-policy data is malformed or cannot be evaluated."""


@dataclass(frozen=True)
class PolicyRule:
    id: str
    commands: tuple[str, ...]
    category: str
    severity: str
    summary: str


@dataclass(frozen=True)
class CommandPolicy:
    version: int
    wrappers: tuple[str, ...]
    protected_paths: tuple[str, ...]
    protected_sudoers_paths: tuple[str, ...]
    block_device_prefixes: tuple[str, ...]
    protected_refs: tuple[str, ...]
    rules: Mapping[str, PolicyRule]


@dataclass(frozen=True)
class CommandMatch:
    rule_id: str
    category: str
    severity: str
    summary: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class CommandDecision:
    allowed: bool
    command: str
    segments: tuple[tuple[str, ...], ...]
    match: CommandMatch | None = None


def apply_operator_overlay(policy: CommandPolicy, overlay: object) -> CommandPolicy:
    """Apply an additive-only operator overlay and revalidate the full policy.

    Overlay fields add protected literals to the shipped policy. Replacing rules,
    wrappers, severities, or any unknown field is rejected so an operator file
    cannot silently weaken the baseline.
    """

    raw = _require_mapping(overlay, source="<operator-overlay>", label="operator overlay")
    _assert_known_keys(
        raw,
        allowed=_OPERATOR_OVERLAY_KEYS,
        source="<operator-overlay>",
        label="operator overlay",
    )
    payload: dict[str, object] = {
        "version": policy.version,
        "wrappers": list(policy.wrappers),
        "protected_paths": list(policy.protected_paths),
        "protected_sudoers_paths": list(policy.protected_sudoers_paths),
        "block_device_prefixes": list(policy.block_device_prefixes),
        "protected_refs": list(policy.protected_refs),
        "rules": [
            {
                "id": rule.id,
                "commands": list(rule.commands),
                "category": rule.category,
                "severity": rule.severity,
                "summary": rule.summary,
            }
            for rule in policy.rules.values()
        ],
    }
    field_names = {
        "add_protected_paths": "protected_paths",
        "add_protected_sudoers_paths": "protected_sudoers_paths",
        "add_block_device_prefixes": "block_device_prefixes",
        "add_protected_refs": "protected_refs",
    }
    for overlay_key, policy_key in field_names.items():
        additions = raw.get(overlay_key, [])
        if not isinstance(additions, list) or any(
            not isinstance(item, str) or not item.strip() for item in additions
        ):
            raise CommandPolicyError(
                f"operator overlay {overlay_key} 必須是字串清單"
            )
        current = payload[policy_key]
        assert isinstance(current, list)
        payload[policy_key] = [*current, *additions]
    validated = parse_policy(payload, source="<operator-overlay-effective>")
    for field_name in field_names.values():
        if not set(getattr(policy, field_name)) <= set(getattr(validated, field_name)):
            raise CommandPolicyError(
                f"operator overlay 不得降低基準政策保護：{field_name}"
            )
    if validated.rules != policy.rules or validated.wrappers != policy.wrappers:
        raise CommandPolicyError("operator overlay 不得修改基準規則或 wrapper")
    return validated


def load_policy_with_overlay(base_text: str, overlay_text: str | None = None) -> CommandPolicy:
    policy = load_policy_text(base_text, source="baseline")
    if overlay_text is None or not overlay_text.strip():
        return policy
    try:
        overlay = safe_load(overlay_text)
    except YAMLError as exc:
        raise CommandPolicyError(f"operator overlay YAML 解析失敗: {exc}") from exc
    return apply_operator_overlay(policy, overlay)


def policy_sha256(policy: CommandPolicy) -> str:
    """Return a stable digest of the validated effective policy."""

    payload = {
        "version": policy.version,
        "wrappers": list(policy.wrappers),
        "protected_paths": list(policy.protected_paths),
        "protected_sudoers_paths": list(policy.protected_sudoers_paths),
        "block_device_prefixes": list(policy.block_device_prefixes),
        "protected_refs": list(policy.protected_refs),
        "rules": [
            {
                "id": rule.id,
                "commands": list(rule.commands),
                "category": rule.category,
                "severity": rule.severity,
                "summary": rule.summary,
            }
            for rule in policy.rules.values()
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_policy_text(text: str, *, source: str = "<memory>") -> CommandPolicy:
    try:
        payload = safe_load(text)
    except YAMLError as exc:
        raise CommandPolicyError(f"command policy YAML 解析失敗: {source}: {exc}") from exc
    return parse_policy(payload, source=source)


def parse_policy(payload: object, *, source: str = "<memory>") -> CommandPolicy:
    raw = _require_mapping(payload, source=source, label="command policy")
    _assert_known_keys(raw, allowed=_TOP_LEVEL_KEYS, source=source, label="command policy")

    version = raw.get("version")
    if not isinstance(version, int) or version != 1:
        raise CommandPolicyError(f"command policy version 不合法: {source}: {version!r}")

    rules: dict[str, PolicyRule] = {}
    for index, item in enumerate(_require_list(raw.get("rules"), source=source, label="rules")):
        record = _require_mapping(item, source=source, label=f"rules[{index}]")
        _assert_known_keys(record, allowed=_RULE_KEYS, source=source, label=f"rules[{index}]")
        rule_id = _require_text(record.get("id"), source=source, label=f"rules[{index}].id")
        if rule_id not in _SUPPORTED_RULE_IDS:
            raise CommandPolicyError(f"command policy 規則未實作: {source}: {rule_id}")
        if rule_id in rules:
            raise CommandPolicyError(f"command policy 規則重複: {source}: {rule_id}")
        commands = tuple(token.lower() for token in _require_text_list(record.get("commands"), source=source, label=f"rules[{index}].commands"))
        supported_commands = _IMPLEMENTED_RULE_COMMANDS[rule_id]
        unknown_commands = sorted(set(commands) - supported_commands)
        if unknown_commands:
            raise CommandPolicyError(
                f"command policy 規則命令未實作: {source}: {rule_id}: {unknown_commands}"
            )
        rules[rule_id] = PolicyRule(
            id=rule_id,
            commands=commands,
            category=_require_text(record.get("category"), source=source, label=f"rules[{index}].category"),
            severity=_require_text(record.get("severity"), source=source, label=f"rules[{index}].severity"),
            summary=_require_text(record.get("summary"), source=source, label=f"rules[{index}].summary"),
        )

    missing_rules = sorted(_SUPPORTED_RULE_IDS - set(rules))
    if missing_rules:
        raise CommandPolicyError(f"command policy 缺少必要規則: {source}: {missing_rules}")

    wrappers = tuple(
        token.lower()
        for token in _require_text_list(raw.get("wrappers"), source=source, label="wrappers")
    )
    unknown_wrappers = sorted(set(wrappers) - _SUPPORTED_WRAPPERS)
    if unknown_wrappers:
        raise CommandPolicyError(f"command policy wrappers 未實作: {source}: {unknown_wrappers}")

    return CommandPolicy(
        version=version,
        wrappers=wrappers,
        protected_paths=tuple(
            _normalize_protected_literal(token)
            for token in _require_text_list(raw.get("protected_paths"), source=source, label="protected_paths")
        ),
        protected_sudoers_paths=tuple(
            _normalize_absolute_path(token)
            for token in _require_text_list(
                raw.get("protected_sudoers_paths"), source=source, label="protected_sudoers_paths"
            )
        ),
        block_device_prefixes=tuple(
            _normalize_absolute_path(token)
            for token in _require_text_list(
                raw.get("block_device_prefixes"), source=source, label="block_device_prefixes"
            )
        ),
        protected_refs=tuple(
            token.strip()
            for token in _require_text_list(raw.get("protected_refs"), source=source, label="protected_refs")
        ),
        rules=MappingProxyType(rules),
    )


def split_command(command: str) -> tuple[str, ...]:
    if not command.strip():
        return ()
    try:
        return tuple(shlex.split(command, posix=True))
    except ValueError as exc:
        raise CommandPolicyError(f"command tokenization 失敗: {exc}") from exc


def split_command_segments(command: str) -> tuple[tuple[str, ...], ...]:
    if not command.strip():
        return ()
    lexer = shlex.shlex(command, posix=True, punctuation_chars="|;&>\n")
    lexer.commenters = ""
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    segments: list[tuple[str, ...]] = []
    current: list[str] = []
    try:
        for token in lexer:
            if token in _SEGMENT_SEPARATORS:
                if current:
                    segments.append(tuple(current))
                    current.clear()
                continue
            current.append(token)
    except ValueError as exc:
        raise CommandPolicyError(f"command segmentation 失敗: {exc}") from exc
    if current:
        segments.append(tuple(current))
    return tuple(segments)


def normalize_argv0(token: str | None) -> str:
    if token is None:
        return ""
    text = token.strip()
    while text.startswith("\\"):
        text = text[1:]
    name = PurePosixPath(text).name or text
    return name.lower()


def unwrap_command_argv(
    argv: Sequence[str],
    *,
    wrappers: Sequence[str] | None = None,
    stop_before: Sequence[str] = (),
) -> tuple[str, ...]:
    remaining = tuple(token for token in argv if token)
    active_wrappers = frozenset(token.lower() for token in (wrappers or ()))
    if not active_wrappers:
        active_wrappers = frozenset({"sudo", "env", "timeout", "command", "nohup"})
    stop_wrappers = frozenset(token.lower() for token in stop_before)

    seen: set[tuple[str, ...]] = set()
    while remaining and remaining not in seen:
        seen.add(remaining)
        assignment_end = _leading_assignment_end(remaining)
        if assignment_end:
            remaining = remaining[assignment_end:]
            continue
        if not remaining:
            return ()
        argv0 = normalize_argv0(remaining[0])
        if argv0 in stop_wrappers:
            return remaining
        if argv0 not in active_wrappers:
            return remaining
        if argv0 == "sudo":
            next_argv = _strip_sudo(remaining)
        elif argv0 == "env":
            next_argv = _strip_env(remaining)
        elif argv0 == "timeout":
            next_argv = _strip_timeout(remaining)
        elif argv0 == "eval":
            next_argv = _strip_eval(remaining)
        elif argv0 == "xargs":
            next_argv = _strip_xargs(remaining)
        else:
            next_argv = _strip_simple_prefix_wrapper(remaining)
        if next_argv == remaining:
            return remaining
        remaining = next_argv
    return remaining


def evaluate_command(command: str, *, policy: CommandPolicy) -> CommandDecision:
    return _evaluate_command(command, policy=policy, depth=0)


def _evaluate_command(
    command: str, *, policy: CommandPolicy, depth: int
) -> CommandDecision:
    if depth > _MAX_NESTED_SHELL_DEPTH:
        raise CommandPolicyError("shell 巢狀命令超過解析上限")
    nested_commands = _nested_shell_commands(command)
    for nested_command in nested_commands:
        nested_decision = _evaluate_command(
            nested_command, policy=policy, depth=depth + 1
        )
        if nested_decision.match is not None:
            return CommandDecision(
                allowed=False,
                command=command,
                segments=split_command_segments(command),
                match=nested_decision.match,
            )
    segments = split_command_segments(command)
    for segment in segments:
        match = _match_segment(segment, policy, depth=depth)
        if match is not None:
            return CommandDecision(allowed=False, command=command, segments=segments, match=match)
    return CommandDecision(allowed=True, command=command, segments=segments)


def _match_segment(
    segment: Sequence[str], policy: CommandPolicy, *, depth: int
) -> CommandMatch | None:
    unwrapped = unwrap_command_argv(
        segment, wrappers=policy.wrappers, stop_before=("eval",)
    )
    if not unwrapped:
        return None
    argv0 = normalize_argv0(unwrapped[0])

    if argv0 == "eval":
        return _evaluate_command(
            _eval_command_text(unwrapped), policy=policy, depth=depth + 1
        ).match

    nested_command = _shell_command_payload(unwrapped)
    if argv0 in _SHELL_ARGV0 and nested_command is not None:
        return _evaluate_command(nested_command, policy=policy, depth=depth + 1).match

    if _rule_matches_command(policy, "protected-root-destruction", argv0) and _is_python_argv0(argv0):
        python_match = _match_python_command(unwrapped, policy)
        if python_match is not None:
            return python_match

    if _rule_matches_command(policy, "protected-root-destruction", argv0) and argv0 == "rm" and _rm_touches_protected_root(unwrapped[1:], policy.protected_paths):
        return _build_match(policy, "protected-root-destruction", unwrapped)
    if _rule_matches_command(policy, "protected-root-destruction", argv0) and argv0 == "find" and _find_deletes_protected_root(unwrapped[1:], policy.protected_paths):
        return _build_match(policy, "protected-root-destruction", unwrapped)
    redirect_writer = _redirect_command_word(unwrapped)
    if (
        redirect_writer
        and _rule_matches_command(policy, "account-mutation", redirect_writer)
        and _redirect_targets_protected_path(unwrapped, policy.protected_sudoers_paths)
    ):
        return _build_match(policy, "account-mutation", unwrapped)
    if _rule_matches_command(policy, "account-mutation", argv0) and argv0 in {"passwd", "useradd", "adduser"}:
        return _build_match(policy, "account-mutation", unwrapped)
    if _rule_matches_command(policy, "account-mutation", argv0) and argv0 == "tee" and _tee_targets_protected_sudoers(unwrapped[1:], policy.protected_sudoers_paths):
        return _build_match(policy, "account-mutation", unwrapped)
    if _rule_matches_command(policy, "block-device-clobber", argv0) and argv0 == "dd" and _dd_targets_block_device(unwrapped[1:], policy.block_device_prefixes):
        return _build_match(policy, "block-device-clobber", unwrapped)
    if _rule_matches_command(policy, "repository-destruction", argv0) and argv0 == "git" and _git_push_targets_protected_ref(unwrapped[1:], policy.protected_refs):
        return _build_match(policy, "repository-destruction", unwrapped)
    if _rule_matches_command(policy, "repository-destruction", argv0) and argv0 == "gh" and _gh_repo_delete(unwrapped[1:]):
        return _build_match(policy, "repository-destruction", unwrapped)
    return None


def _match_python_command(argv: Sequence[str], policy: CommandPolicy) -> CommandMatch | None:
    try:
        index = list(argv).index("-c")
    except ValueError:
        return None
    if index + 1 >= len(argv):
        return None
    code = argv[index + 1]
    try:
        module = ast.parse(code, mode="exec")
    except SyntaxError:
        return None
    module_aliases, function_aliases = _collect_shutil_aliases(module)
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        if not _is_shutil_rmtree_call(
            _dotted_name(node.func),
            module_aliases=module_aliases,
            function_aliases=function_aliases,
        ):
            continue
        if not node.args:
            continue
        target = _literal_text(node.args[0])
        if target is None:
            continue
        if _matches_protected_literal(target, policy.protected_paths):
            return _build_match(policy, "protected-root-destruction", argv)
    return None


def _build_match(policy: CommandPolicy, rule_id: str, argv: Sequence[str]) -> CommandMatch:
    rule = policy.rules[rule_id]
    return CommandMatch(
        rule_id=rule.id,
        category=rule.category,
        severity=rule.severity,
        summary=rule.summary,
        argv=tuple(argv),
    )


def _strip_sudo(argv: Sequence[str]) -> tuple[str, ...]:
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return tuple(argv[index + 1 :])
        if token in _HELP_OR_VERSION_FLAGS or token == "-V":
            return ()
        if "=" in token and token.split("=", 1)[0]:
            index += 1
            continue
        if not token.startswith("-") or token == "-":
            return tuple(argv[index:])
        if token in _SUDO_VALUE_OPTIONS and index + 1 < len(argv):
            index += 2
            continue
        cluster_value = _cluster_option_value(token, _SUDO_VALUE_OPTION_CHARS)
        if cluster_value is not None:
            if cluster_value[1] is not None:
                index += 1
                continue
            if index + 1 < len(argv):
                index += 2
                continue
        option = token.split("=", 1)[0]
        if option in _SUDO_LONG_VALUE_OPTIONS:
            if token != option:
                index += 1
                continue
            if index + 1 < len(argv):
                index += 2
                continue
        index += 1
    return ()


def _leading_assignment_end(argv: Sequence[str]) -> int:
    index = 0
    while index < len(argv):
        name, separator, _value = argv[index].partition("=")
        if not separator or not _is_shell_identifier(name):
            break
        index += 1
    return index


def _is_shell_identifier(value: str) -> bool:
    if not value:
        return False
    first = value[0]
    if first != "_" and not (first.isascii() and first.isalpha()):
        return False
    return all(char.isascii() and (char.isalnum() or char == "_") for char in value[1:])


def _strip_eval(argv: Sequence[str]) -> tuple[str, ...]:
    return split_command(_eval_command_text(argv))


def _eval_command_text(argv: Sequence[str]) -> str:
    index = 1
    if index < len(argv) and argv[index] == "--":
        index += 1
    if index >= len(argv):
        return ""
    return " ".join(argv[index:])


def _strip_xargs(argv: Sequence[str]) -> tuple[str, ...]:
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return tuple(argv[index + 1 :])
        if token.startswith("--"):
            option, separator, _value = token.partition("=")
            if option in _XARGS_LONG_VALUE_OPTIONS:
                if separator:
                    index += 1
                elif index + 1 < len(argv):
                    index += 2
                else:
                    return ()
                continue
            if option in {
                "--exit",
                "--no-run-if-empty",
                "--null",
                "--open-tty",
                "--show-limits",
                "--verbose",
            } and not separator:
                index += 1
                continue
            raise CommandPolicyError(f"xargs option 無法解析: {token}")
        if token.startswith("-") and token != "-":
            cluster = token[1:]
            option_index = 0
            while option_index < len(cluster):
                option = cluster[option_index]
                if option in _XARGS_SHORT_VALUE_OPTIONS:
                    if option_index + 1 < len(cluster):
                        index += 1
                    elif index + 1 < len(argv):
                        index += 2
                    else:
                        return ()
                    break
                if option == "i":
                    # GNU xargs accepts an optional replacement string for -i.
                    if option_index + 1 < len(cluster):
                        index += 1
                    else:
                        index += 1
                    break
                if option not in {"0", "p", "r", "t", "x"}:
                    raise CommandPolicyError(f"xargs option 無法解析: -{option}")
                option_index += 1
            else:
                index += 1
            continue
        return tuple(argv[index:])
    # Without an explicit command xargs invokes echo, which is not a policy target.
    return ()


def _strip_env(argv: Sequence[str]) -> tuple[str, ...]:
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return tuple(argv[index + 1 :])
        if token in _HELP_OR_VERSION_FLAGS:
            return ()
        if token == "-S":
            if index + 1 >= len(argv):
                return ()
            return split_command(argv[index + 1])
        option = token.split("=", 1)[0]
        if option in _ENV_LONG_VALUE_OPTIONS:
            if option == "--split-string":
                if token != option:
                    return split_command(token.split("=", 1)[1])
                if index + 1 >= len(argv):
                    return ()
                return split_command(argv[index + 1])
            if token != option:
                index += 1
                continue
            if index + 1 < len(argv):
                index += 2
                continue
        cluster_value = _cluster_option_value(token, _ENV_CLUSTER_VALUE_OPTIONS)
        if cluster_value is not None:
            option, attached = cluster_value
            if option == "S":
                if attached is not None:
                    return split_command(attached)
                if index + 1 >= len(argv):
                    return ()
                return split_command(argv[index + 1])
            if attached is not None:
                index += 1
                continue
            if index + 1 < len(argv):
                index += 2
                continue
        if token in _ENV_VALUE_OPTIONS and index + 1 < len(argv):
            index += 2
            continue
        if token.startswith("-") and token != "-":
            index += 1
            continue
        if "=" in token and token.split("=", 1)[0]:
            index += 1
            continue
        return tuple(argv[index:])
    return ()


def _strip_timeout(argv: Sequence[str]) -> tuple[str, ...]:
    index = _skip_options(argv, start=1, value_options=_TIMEOUT_VALUE_OPTIONS)
    if index >= len(argv):
        return ()
    if index + 1 >= len(argv):
        return ()
    return tuple(argv[index + 1 :])


def _strip_simple_prefix_wrapper(argv: Sequence[str]) -> tuple[str, ...]:
    index = _skip_options(argv, start=1, value_options=frozenset())
    return tuple(argv[index:])


def _skip_options(argv: Sequence[str], *, start: int, value_options: frozenset[str]) -> int:
    index = start
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return index + 1
        if not token.startswith("-") or token == "-":
            return index
        if token in value_options and index + 1 < len(argv):
            index += 2
            continue
        index += 1
    return index


def _shell_command_payload(argv: Sequence[str]) -> str | None:
    for index, token in enumerate(argv[1:], start=1):
        if token in _SHELL_INLINE_COMMAND_OPTIONS and index + 1 < len(argv):
            return argv[index + 1]
        if token.startswith("-") and not token.startswith("--") and "c" in token[1:] and index + 1 < len(argv):
            return argv[index + 1]
    return None


def _is_python_argv0(argv0: str) -> bool:
    return argv0 == "python" or argv0.startswith("python")


def _rm_touches_protected_root(args: Sequence[str], protected_paths: Sequence[str]) -> bool:
    if not any(_is_rm_recursive_flag(token) for token in args):
        return False
    preserve_root = any(
        token == "--preserve-root" or token.startswith("--preserve-root=")
        for token in args
    )
    for token in _iter_positional_args(args):
        normalized = _normalize_protected_literal(token)
        if preserve_root and normalized == "/":
            continue
        if normalized in protected_paths:
            return True
    return False


def _find_deletes_protected_root(args: Sequence[str], protected_paths: Sequence[str]) -> bool:
    if "-delete" not in args:
        return False
    delete_index = list(args).index("-delete")
    roots: list[str] = []
    index = 0
    leading = args[:delete_index]
    while index < len(leading):
        token = leading[index]
        if token == "--":
            index += 1
            continue
        if token in {"-D", "-O"} and index + 1 < len(leading):
            index += 2
            continue
        if token.startswith("-") or token in {"!", "(", ")"}:
            if roots:
                break
            index += 1
            continue
        roots.append(token)
        index += 1
    return any(_matches_protected_literal(token, protected_paths) for token in roots)


def _tee_targets_protected_sudoers(args: Sequence[str], protected_paths: Sequence[str]) -> bool:
    for token in _iter_positional_args(args):
        if _matches_protected_prefix(token, protected_paths):
            return True
    return False


def _dd_targets_block_device(args: Sequence[str], prefixes: Sequence[str]) -> bool:
    for token in args:
        if not token.startswith("of="):
            continue
        normalized = _normalize_absolute_path(token.split("=", 1)[1])
        if any(normalized.startswith(prefix) for prefix in prefixes):
            return True
        if normalized.startswith("/dev/"):
            basename = PurePosixPath(normalized).name
            if any(basename.startswith(PurePosixPath(prefix).name) for prefix in prefixes):
                return True
    return False


def _redirect_targets_protected_path(args: Sequence[str], protected_paths: Sequence[str]) -> bool:
    for index, token in enumerate(args):
        target = _inline_redirect_target(token)
        if target is None and token in _REDIRECTION_OPERATORS and index + 1 < len(args):
            target = args[index + 1]
        if target is None:
            continue
        if _matches_protected_prefix(target, protected_paths):
            return True
    return False


def _git_push_targets_protected_ref(args: Sequence[str], protected_refs: Sequence[str]) -> bool:
    git_args = _strip_git_global_options(args)
    if not git_args or git_args[0] != "push":
        return False
    push_args = git_args[1:]
    refs = _git_push_refspecs(push_args)
    if "--mirror" in push_args:
        return True
    if any(token in {"--delete", "-d"} for token in push_args):
        return any(_normalize_ref_name(token) in protected_refs for token in refs)
    if any(_deletes_protected_ref(token, protected_refs) for token in push_args):
        return True
    if not refs:
        force_requested = any(_is_force_flag(token) for token in push_args)
        return force_requested and any(token in {"--all", "--branches"} for token in push_args)
    force_requested = any(_is_force_flag(token) for token in push_args) or any(
        _is_forced_refspec(token) for token in refs
    )
    if force_requested and any(token in {"--all", "--branches"} for token in push_args):
        return True
    if not force_requested:
        return False
    return any(_normalize_ref_name(token) in protected_refs for token in refs)


def _gh_repo_delete(args: Sequence[str]) -> bool:
    gh_args = _strip_gh_global_options(args)
    return len(gh_args) >= 2 and gh_args[0] == "repo" and gh_args[1] == "delete"


def _iter_positional_args(args: Sequence[str]) -> tuple[str, ...]:
    values: list[str] = []
    literal_mode = False
    for token in args:
        if token == "--":
            literal_mode = True
            continue
        if not literal_mode and token.startswith("-") and token != "-":
            continue
        values.append(token)
    return tuple(values)


def _is_rm_recursive_flag(token: str) -> bool:
    if token.startswith("--"):
        return token == "--recursive"
    if token in {"-r", "-R", "--recursive"}:
        return True
    return token.startswith("-") and not token.startswith("--") and (
        "r" in token[1:] or "R" in token[1:]
    )


def _matches_protected_literal(token: str, protected_paths: Sequence[str]) -> bool:
    normalized = _normalize_protected_literal(token)
    return normalized in protected_paths


def _normalize_protected_literal(token: str) -> str:
    text = token.strip()
    text = _normalize_home_reference(text)
    if text.startswith("~"):
        return _normalize_tilde_path(text)
    if text.startswith("/"):
        return _normalize_anchored_path(text, anchor="/")
    return text.rstrip("/")


def _normalize_home_reference(token: str) -> str:
    for reference in ("${HOME}", "$HOME"):
        if token == reference:
            return "~"
        if token.startswith(reference) and token[len(reference) :].startswith("/"):
            return f"~{token[len(reference):]}"
    return token


def _normalize_absolute_path(token: str) -> str:
    text = token.strip()
    if not text.startswith("/"):
        return text.rstrip("/")
    return _normalize_anchored_path(text, anchor="/")


def _is_force_flag(token: str) -> bool:
    return token in _FORCE_PUSH_FLAGS or token.startswith("--force-with-lease=")


def _deletes_protected_ref(token: str, protected_refs: Sequence[str]) -> bool:
    if not token.startswith(":"):
        return False
    return _normalize_ref_name(token) in protected_refs


def _normalize_ref_name(token: str) -> str:
    text = token.lstrip("+")
    if text.startswith(":"):
        text = text[1:]
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    if text.startswith(_REFS_HEADS_PREFIX):
        return text[len(_REFS_HEADS_PREFIX) :]
    return text


def _is_forced_refspec(token: str) -> bool:
    return token.startswith("+") and len(token) > 1


def _strip_gh_global_options(args: Sequence[str]) -> tuple[str, ...]:
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            return tuple(args[index + 1 :])
        if token in _HELP_OR_VERSION_FLAGS:
            return ()
        option = token.split("=", 1)[0]
        if option in _GH_VALUE_OPTIONS:
            if token != option:
                index += 1
                continue
            if index + 1 < len(args):
                index += 2
                continue
        if token.startswith("-"):
            index += 1
            continue
        return tuple(args[index:])
    return ()


def _strip_git_global_options(args: Sequence[str]) -> tuple[str, ...]:
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            return tuple(args[index + 1 :])
        if token in _HELP_OR_VERSION_FLAGS or token == "-V":
            return ()
        option = token.split("=", 1)[0]
        if option in _GIT_GLOBAL_VALUE_OPTIONS:
            if token != option:
                index += 1
                continue
            if index + 1 < len(args):
                index += 2
                continue
        if token.startswith("-"):
            index += 1
            continue
        return tuple(args[index:])
    return ()


def _git_push_refspecs(push_args: Sequence[str]) -> tuple[str, ...]:
    positional = tuple(token for token in push_args if token and not token.startswith("-"))
    if len(positional) <= 1:
        return positional
    return positional[1:]


def _redirect_command_word(argv: Sequence[str]) -> str:
    index = 0
    while index < len(argv):
        token = argv[index]
        if (
            token.isdigit()
            and index + 2 < len(argv)
            and argv[index + 1] in _REDIRECTION_OPERATORS
        ):
            index += 3
            continue
        if token in _REDIRECTION_OPERATORS:
            index += 2
            continue
        if _inline_redirect_target(token) is not None:
            index += 1
            continue
        return normalize_argv0(token)
    return ""


def _cluster_option_value(
    token: str,
    value_option_chars: frozenset[str],
) -> tuple[str, str | None] | None:
    if not token.startswith("-") or token.startswith("--") or len(token) <= 2:
        return None
    cluster = token[1:]
    for index, char in enumerate(cluster):
        if char not in value_option_chars:
            continue
        attached = cluster[index + 1 :]
        return char, attached or None
    return None


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        if not prefix:
            return node.attr
        return f"{prefix}.{node.attr}"
    return ""


def _literal_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _rule_matches_command(policy: CommandPolicy, rule_id: str, argv0: str) -> bool:
    commands = policy.rules[rule_id].commands
    if argv0 in commands:
        return True
    return _is_python_argv0(argv0) and "python" in commands


def _collect_shutil_aliases(module: ast.AST) -> tuple[set[str], set[str]]:
    module_aliases = {"shutil"}
    function_aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "shutil":
                        name = alias.asname or alias.name
                        if name not in module_aliases:
                            module_aliases.add(name)
                            changed = True
            elif isinstance(node, ast.ImportFrom) and node.module == "shutil":
                for alias in node.names:
                    if alias.name == "rmtree":
                        name = alias.asname or alias.name
                        if name not in function_aliases:
                            function_aliases.add(name)
                            changed = True
            elif (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                alias_name = node.targets[0].id
                if _is_shutil_module_value(node.value, module_aliases) and alias_name not in module_aliases:
                    module_aliases.add(alias_name)
                    changed = True
                if _is_shutil_rmtree_value(node.value, module_aliases, function_aliases) and alias_name not in function_aliases:
                    function_aliases.add(alias_name)
                    changed = True
    return module_aliases, function_aliases


def _is_shutil_rmtree_call(
    dotted_name: str,
    *,
    module_aliases: set[str],
    function_aliases: set[str],
) -> bool:
    if dotted_name in function_aliases:
        return True
    return any(dotted_name == f"{alias}.rmtree" for alias in module_aliases)


def _is_shutil_module_value(node: ast.AST, module_aliases: set[str]) -> bool:
    return _dotted_name(node) in module_aliases or _is_import_shutil_call(node)


def _is_shutil_rmtree_value(
    node: ast.AST,
    module_aliases: set[str],
    function_aliases: set[str],
) -> bool:
    dotted_name = _dotted_name(node)
    if dotted_name in function_aliases:
        return True
    return any(dotted_name == f"{alias}.rmtree" for alias in module_aliases)


def _is_import_shutil_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if _dotted_name(node.func) != "__import__" or not node.args:
        return False
    return _literal_text(node.args[0]) == "shutil"


def _matches_protected_prefix(token: str, protected_paths: Sequence[str]) -> bool:
    normalized = _normalize_absolute_path(token)
    return any(normalized == root or normalized.startswith(f"{root}/") for root in protected_paths)


def _inline_redirect_target(token: str) -> str | None:
    if token in _REDIRECTION_OPERATORS:
        return None
    for operator in ("1>>", "1>", ">>", ">"):
        if token.startswith(operator) and len(token) > len(operator):
            return token[len(operator) :]
    return None


def _normalize_tilde_path(token: str) -> str:
    if token == "~":
        return "~"
    suffix = token[1:]
    if suffix.startswith("/"):
        normalized = _normalize_anchored_path(suffix, anchor="/")
        return "~" if normalized == "/" else f"~{normalized}"
    return token.rstrip("/")


def _normalize_anchored_path(token: str, *, anchor: str) -> str:
    text = token
    if anchor == "/" and text.startswith("/"):
        text = "/" + text.lstrip("/")

    parts: list[str] = []
    for part in PurePosixPath(text).parts:
        if part in {anchor, "", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    if not parts:
        return anchor
    return f"{anchor}{'/'.join(parts)}"


def _nested_shell_commands(command: str) -> tuple[str, ...]:
    nested: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(command):
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if char == "\\":
                index += 2
                continue
            if char == '"':
                quote = None
                index += 1
                continue
            if char == "`":
                payload, index = _extract_backtick_payload(command, index)
                nested.append(payload)
                continue
            if command.startswith("$(", index):
                payload, index = _extract_parenthesized_payload(command, index + 1)
                nested.append(payload)
                continue
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "`":
            payload, index = _extract_backtick_payload(command, index)
            nested.append(payload)
            continue
        if command.startswith("$(", index):
            payload, index = _extract_parenthesized_payload(command, index + 1)
            nested.append(payload)
            continue
        if char == "(":
            payload, index = _extract_parenthesized_payload(command, index)
            nested.append(payload)
            continue
        index += 1
    return tuple(nested)


def _extract_parenthesized_payload(command: str, opening_index: int) -> tuple[str, int]:
    if opening_index >= len(command) or command[opening_index] != "(":
        raise CommandPolicyError("shell 子命令括號格式錯誤")
    depth = 1
    index = opening_index + 1
    quote: str | None = None
    while index < len(command):
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if char == "\\":
                index += 2
                continue
            if char == '"':
                quote = None
                index += 1
                continue
            if command.startswith("$(", index):
                depth += 1
                index += 2
                continue
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "`":
            _payload, index = _extract_backtick_payload(command, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return command[opening_index + 1 : index], index + 1
        index += 1
    raise CommandPolicyError("shell 子命令括號未閉合")


def _extract_backtick_payload(command: str, opening_index: int) -> tuple[str, int]:
    index = opening_index + 1
    payload: list[str] = []
    while index < len(command):
        char = command[index]
        if char == "\\" and index + 1 < len(command):
            payload.extend((char, command[index + 1]))
            index += 2
            continue
        if char == "`":
            return "".join(payload), index + 1
        payload.append(char)
        index += 1
    raise CommandPolicyError("shell 反引號命令替換未閉合")


def _assert_known_keys(payload: Mapping[str, object], *, allowed: frozenset[str], source: str, label: str) -> None:
    extras = sorted(set(payload) - set(allowed))
    if extras:
        raise CommandPolicyError(f"{label} 出現未知鍵: {source}: {extras}")


def _require_mapping(payload: object, *, source: str, label: str) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise CommandPolicyError(f"{label} 必須是 mapping: {source}")
    return payload


def _require_list(payload: object, *, source: str, label: str) -> list[object]:
    if not isinstance(payload, list) or not payload:
        raise CommandPolicyError(f"{label} 必須是非空清單: {source}")
    return payload


def _require_text(payload: object, *, source: str, label: str) -> str:
    if not isinstance(payload, str) or not payload.strip():
        raise CommandPolicyError(f"{label} 必須是非空字串: {source}")
    return payload.strip()


def _require_text_list(payload: object, *, source: str, label: str) -> list[str]:
    items = _require_list(payload, source=source, label=label)
    result: list[str] = []
    for index, item in enumerate(items):
        result.append(_require_text(item, source=source, label=f"{label}[{index}]"))
    return result


__all__ = [
    "CommandDecision",
    "CommandMatch",
    "CommandPolicy",
    "CommandPolicyError",
    "PolicyRule",
    "evaluate_command",
    "load_policy_text",
    "normalize_argv0",
    "parse_policy",
    "split_command",
    "split_command_segments",
    "unwrap_command_argv",
]
