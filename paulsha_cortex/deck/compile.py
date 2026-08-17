from __future__ import annotations

import json
import os
import re
import tempfile
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from paulsha_cortex.config import paths
from paulsha_cortex.coordinator.workflow import WorkflowManifest, WorkflowStep
from paulsha_cortex.project_policy import ProjectPolicyError, resolve_project_policy

from .schema import BAND_LEVELS, BandTriggeredSpine, Card, Combo, ComboEntry


class DeckCompileError(ValueError):
    """compile 期錯誤（fail-closed：不產任何檔）。"""


_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SLICE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
_CHANGE_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


def slugify_task(task: str) -> str:
    """將 task 正規化為 branch-safe slug。"""
    slug = _SLUG_RE.sub("-", task.lower()).strip("-")[:60].strip("-")
    if not slug:
        raise DeckCompileError(f"task 無法正規化為 slug: {task!r}")
    return slug


def _validate_change_name(change: str) -> str:
    if not _CHANGE_RE.fullmatch(change):
        raise DeckCompileError(
            f"change 名稱不合法（僅允許 [a-z0-9-]，且不可含路徑分隔）: {change!r}"
        )
    return change


def _validate_plan_ref(plan_ref: str) -> str:
    if "\n" in plan_ref or "\r" in plan_ref:
        raise DeckCompileError(f"plan 參照不可含換行: {plan_ref!r}")
    return plan_ref


def _infer_target_branch(task_slug: str, change: str | None) -> str:
    if change and change != "dry-run":
        return f"feature/{change}"
    return f"feature/{task_slug}"


def _warning_default_target_branch(task_slug: str, change: str | None) -> None:
    if change:
        return
    print(
        f"deck compile: 目錄 context 缺 change，target_branch fallback 為 feature/{task_slug}",
        file=sys.stderr,
    )


_DEFAULT_POLICY_CHECK_TIMEOUT_SECONDS = 30
_DEFAULT_TESTS_TIMEOUT_SECONDS = 60


def _preflight_steps(repo_root: Path) -> list[dict[str, object]]:
    """讀 `.project-policy.yml` 的 `preflight.steps`（issue #380：verification 骨架不得
    寫死 pytest，須以此為導出來源）。任何解析問題都視為「偵測不到」，交給呼叫端走
    fail-closed placeholder，不在 compile 期整個炸掉。"""
    try:
        resolution = resolve_project_policy(repo_root)
    except ProjectPolicyError:
        return []
    payload = resolution.payload
    if not isinstance(payload, dict):
        return []
    preflight = payload.get("preflight")
    if not isinstance(preflight, dict):
        return []
    steps = preflight.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def _first_step_of_kind(steps: Sequence[dict[str, object]], kind: str) -> dict[str, object] | None:
    for step in steps:
        if step.get("kind") == kind:
            return step
    return None


def _step_argv(step: dict[str, object] | None) -> list[str] | None:
    if step is None:
        return None
    argv = step.get("argv")
    if not isinstance(argv, list) or not argv:
        return None
    if not all(isinstance(item, str) and item.strip() for item in argv):
        return None
    return list(argv)


def _step_timeout(step: dict[str, object] | None, default: int) -> int | float:
    if step is not None:
        timeout = step.get("timeout_seconds")
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
            return timeout
    return default


def _placeholder_argv(reason: str) -> list[str]:
    """Fail-closed 佔位 argv（issue #380）：偵測不到 policy steps 時不可留空，也不可
    悄悄套用某個猜測指令——必須是「誤執行就非零退出」的明確佔位，翻 dispatch: auto
    前一定會被擋下，逼 operator 手動補上真正的驗證指令。"""
    return ["python3", "-c", "import sys; sys.exit(" + json.dumps(reason, ensure_ascii=False) + ")"]


def _warn_policy_undetected(kind: str, *, purpose: str) -> None:
    print(
        f"deck compile: [WARNING] 未偵測到 .project-policy.yml 的 preflight.steps(kind: {kind})，"
        f"verification.{purpose} 已改填 fail-closed placeholder；翻 dispatch: auto 前必須手動改成"
        "真正的驗證指令（見 issue #380）",
        file=sys.stderr,
    )


def _verification_skeleton(repo_root: Path) -> dict[str, object]:
    steps = _preflight_steps(repo_root)
    validation_step = _first_step_of_kind(steps, "validation")
    tests_step = _first_step_of_kind(steps, "tests")

    policy_argv = _step_argv(validation_step)
    if policy_argv is None:
        policy_argv = _placeholder_argv(
            "cortex deck compile: 未偵測到 .project-policy.yml preflight.steps(kind: validation)，"
            "此 policy check 為 fail-closed placeholder，翻 dispatch: auto 前請手動改成真正的 policy "
            "驗證指令（例如 python3 -m policy_check --repo .）"
        )
        _warn_policy_undetected("validation", purpose="checks[name=policy].argv")
    policy_timeout = _step_timeout(validation_step, _DEFAULT_POLICY_CHECK_TIMEOUT_SECONDS)

    tests_argv = _step_argv(tests_step)
    if tests_argv is None:
        tests_argv = _placeholder_argv(
            "cortex deck compile: 未偵測到 .project-policy.yml preflight.steps(kind: tests)，"
            "此 tests/full_suite 為 fail-closed placeholder，翻 dispatch: auto 前請手動改成真正的"
            "測試指令"
        )
        _warn_policy_undetected("tests", purpose="tests/full_suite.argv")
    tests_timeout = _step_timeout(tests_step, _DEFAULT_TESTS_TIMEOUT_SECONDS)

    return {
        "docs_class": "code",
        "required_artifacts": [],
        "checks": [
            {"kind": "persona-scope"},
            {
                "kind": "command",
                "name": "policy",
                "argv": policy_argv,
                "cwd": ".",
                "timeout_seconds": policy_timeout,
            },
        ],
        "tests": [
            {
                "argv": tests_argv,
                "cwd": ".",
                "timeout_seconds": tests_timeout,
            },
        ],
        "full_suite": {
            "argv": tests_argv,
            "cwd": ".",
            "timeout_seconds": tests_timeout,
            "baseline": "no-regression",
        },
    }


def _format_scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


def _format_inline_list(values: Sequence[object]) -> str:
    return "[" + ", ".join(_format_scalar(value) for value in values) + "]"


def specs_dir() -> Path:
    """鏡射 manager_daemon.default_specs_dir 的 specs 路徑契約：
    PSC_MANAGER_SPECS_DIR → paths.specs_root()（吃 PSC_SPECS_ROOT/PSC_AGENTS_ROOT）。
    facade 為允許依賴（零 import 鐵律僅涵蓋 lifecycle/memory）。"""
    override = os.environ.get("PSC_MANAGER_SPECS_DIR")
    if override:
        return Path(override)
    return paths.specs_root()


@dataclass(frozen=True)
class SliceDoc:
    slice_id: str
    filename: str
    content: str


@dataclass(frozen=True)
class CompileResult:
    task_slug: str
    slices: tuple[SliceDoc, ...]
    checklist: tuple[str, ...]
    verify_commands: tuple[str, ...]
    external: tuple[str, ...]
    # Optional only for source compatibility with callers that construct a
    # CompileResult solely to exercise emit(); compile_combo always populates it.
    workflow_manifest: WorkflowManifest | None = None
    # gate_spine 兩層制（#221）：verify_commands 為合併總覽（沿用既有行為），這兩個
    # 欄位額外標示哪些 gate verify 指令屬於必要核心層、哪些屬於 band 觸發加掛層，
    # 供下游（H.1 sizing/acceptance_surfaces）只讀核心層而不必重新解析 combo。
    core_gate_verify_commands: tuple[str, ...] = ()
    band_gate_verify_commands: tuple[str, ...] = ()


_LEGACY_CARD_PHASES = {
    "workflow-claim": "claim",
    "brainstorming": "define",
    "openspec-propose": "define",
    "writing-plans": "plan",
    "mcu-hw-evidence": "define",
    "worktree-isolation": "build",
    "tdd-red": "build",
    "subagent-build": "build",
    "receiving-code-review": "build",
    "write-spec": "plan",
    "verification": "verify",
    "code-review": "review",
    "adversarial-review": "review",
    "openspec-archive": "ship",
    "policy-commit": "ship",
}


def _subst(glob: str, slug: str, change: str | None) -> str:
    out = glob.replace("<task-slug>", slug)
    if "<change>" in out:
        if not change:
            raise DeckCompileError("卡片 glob 使用 <change>，需提供 --change <name>")
        out = out.replace("<change>", change)
    return out


def parse_with_spec(spec: str) -> tuple[str, str | None, str | None]:
    if ":" not in spec:
        return spec, None, None
    card_id, _, rest = spec.partition(":")
    kind, _, anchor = rest.partition("=")
    if not card_id or kind not in ("after", "before") or not anchor:
        raise DeckCompileError(f"--with 定位格式錯誤: {spec!r}（card[:after=<id>|:before=<id>]）")
    return card_id, kind, anchor


def _resolve_hand(
    combo: Combo,
    cards: Mapping[str, Card],
    with_cards: Sequence[str],
    only: Sequence[str],
) -> list[ComboEntry]:
    if with_cards and only:
        raise DeckCompileError("--with 與 --only 不可同時使用")
    if only:
        combo_refs = {entry.ref for entry in combo.cards}
        unknown = [card_id for card_id in only if card_id not in combo_refs]
        if unknown:
            raise DeckCompileError(f"--only 指定了 combo 外卡片: {unknown}")
        selected = set(only)
        return [entry for entry in combo.cards if entry.ref in selected]

    hand = list(combo.cards)
    for spec in with_cards:
        card_id, kind, anchor = parse_with_spec(spec)
        if card_id not in cards:
            raise DeckCompileError(f"--with 未知卡片: {card_id}")
        refs = [entry.ref for entry in hand]
        if card_id in refs:
            raise DeckCompileError(f"--with 卡片已在骨幹中: {card_id}")

        if kind is None:
            pos: int | None = None
            requires = cards[card_id].requires
            if requires:
                seen: list[str] = []
                for index, entry in enumerate(hand):
                    seen.extend(cards[entry.ref].produces)
                    if all(any(_covered(require, produce) for produce in seen) for require in requires):
                        pos = index + 1
                        break
            if pos is None:
                raise DeckCompileError(
                    f"--with {card_id} 無法推斷插入點，請明示 :after=<id> 或 :before=<id>"
                )
        else:
            if anchor not in refs:
                raise DeckCompileError(f"--with 定位錨點不在手牌中: {anchor}")
            anchor_index = refs.index(anchor)
            pos = anchor_index + (1 if kind == "after" else 0)
        hand.insert(pos, ComboEntry(ref=card_id))
    return hand


def _prefix(glob: str) -> str:
    return glob.split("*", 1)[0]


def _covered(require: str, produce: str) -> bool:
    """保守 pattern 覆蓋（spec §5.5）：字面前綴一致至首個 wildcard，互為前綴即視為覆蓋。"""
    require_prefix = _prefix(require)
    produce_prefix = _prefix(produce)
    return require_prefix.startswith(produce_prefix) or produce_prefix.startswith(require_prefix)


def _check_requires_coverage(
    entries: Sequence[ComboEntry],
    cards: Mapping[str, Card],
    allow_external: bool,
) -> tuple[str, ...]:
    upstream: list[str] = []
    external: list[str] = []
    for entry in entries:
        card = cards[entry.ref]
        for require in card.requires:
            if not any(_covered(require, produce) for produce in upstream):
                external.append(f"{card.id}: {require}")
        upstream.extend(card.produces)
    if external and not allow_external:
        raise DeckCompileError(
            "requires 未被上游 produces 覆蓋（external input），"
            "確認後以 --allow-external 放行：\n  " + "\n  ".join(external)
        )
    return tuple(external)


def _group_slices(
    entries: Sequence[ComboEntry],
    cards: Mapping[str, Card],
    slug: str,
) -> list[tuple[str, list[Card]]]:
    groups: list[tuple[str, list[Card]]] = []
    seen_ids: set[str] = set()
    for entry in entries:
        card = cards[entry.ref]
        if card.type != "headless" or card.phase == "claim":
            continue
        if card.slice_group:
            slice_id = f"{slug}-{card.slice_group}"
        else:
            slice_id = f"{slug}-{card.id}"
        if groups and groups[-1][0] == slice_id:
            groups[-1][1].append(card)
            continue
        if slice_id in seen_ids:
            raise DeckCompileError(f"combo 產生重複 slice_id: {slice_id}")
        seen_ids.add(slice_id)
        groups.append((slice_id, [card]))
    return groups


def _render_frontmatter(
    slice_id: str,
    plan_ref: str,
    deps: Sequence[str],
    target_branch: str,
    verification: dict[str, object],
) -> str:
    depends_on = "[" + ", ".join(deps) + "]" if deps else "[]"
    lines = [
        "---",
        "dispatch: hold",
        f"slice_id: {slice_id}",
        f"plan: {json.dumps(plan_ref, ensure_ascii=False)}",
        f"depends_on: {depends_on}",
        f"target_branch: {json.dumps(target_branch, ensure_ascii=False)}",
        # #469：repo 歸屬為 optional 顯式宣告——emit 只出 null 佔位，操作者翻
        # dispatch: auto 前自行補 owner/repo；自動自 claim/work item 帶入為 follow-up。
        "repo: null",
    ]
    lines.append("verification:")
    lines.append(f"  docs_class: {_format_scalar(verification.get('docs_class'))}")
    required_artifacts = verification.get("required_artifacts")
    if not required_artifacts:
        lines.append("  required_artifacts: []")
    else:
        lines.append("  required_artifacts:")
        for artifact in required_artifacts if isinstance(required_artifacts, list) else ():
            if isinstance(artifact, dict):
                lines.append("    - path: " + _format_scalar(artifact.get("path")))
                lines.append(
                    "      must_change: "
                    + _format_scalar(
                        artifact.get("must_change", False),
                    )
                )
            else:
                lines.append("    - " + _format_scalar(artifact))
    checks = verification.get("checks")
    lines.append("  checks:")
    if isinstance(checks, list):
        for check in checks:
            if not isinstance(check, dict):
                continue
            lines.append("    - kind: " + _format_scalar(check.get("kind")))
            for key, value in check.items():
                if key == "kind":
                    continue
                lines.append(f"      {key}: {_format_scalar(value)}")
    tests = verification.get("tests")
    lines.append("  tests:")
    if isinstance(tests, list):
        for test in tests:
            if not isinstance(test, dict):
                continue
            lines.append("    - argv: " + _format_inline_list(test.get("argv", ())))
            for key, value in test.items():
                if key == "argv":
                    continue
                lines.append(f"      {key}: {_format_scalar(value)}")
    full_suite = verification.get("full_suite")
    lines.append("  full_suite:")
    if isinstance(full_suite, dict):
        for key, value in full_suite.items():
            lines.append(f"    {key}: {_format_scalar(value)}")
    lines.append("parse_error: null")
    lines.append("---")
    return "\n".join(lines)


def _default_plan_ref(
    entries: Sequence[ComboEntry],
    cards: Mapping[str, Card],
    slug: str,
    change: str | None,
) -> str | None:
    for entry in reversed(entries):
        card = cards[entry.ref]
        if card.type == "interactive" and card.produces:
            return _subst(card.produces[0], slug, change)
    return None


def _uses_change(card: Card) -> bool:
    return any("<change>" in pattern for pattern in card.requires + card.produces)


def _workflow_phase(card: Card, persona) -> str:
    phase = card.phase or _LEGACY_CARD_PHASES.get(card.id)
    if phase is None:
        if len(persona.allowed_phases) == 1:
            phase = persona.allowed_phases[0]
        else:
            raise DeckCompileError(f"{card.id}: 缺 phase，無法由 persona {persona.role} 唯一推導")
    if phase not in persona.allowed_phases:
        raise DeckCompileError(f"{card.id}: persona {persona.role} 不允許 phase {phase}")
    return phase


def _manifest_subst(pattern: str, slug: str, change: str | None) -> str:
    """Resolve known locators while retaining an intentionally external change token."""
    out = pattern.replace("<task-slug>", slug)
    if change is not None:
        out = out.replace("<change>", change)
    return out


def _compile_workflow_manifest(
    combo: Combo,
    entries: Sequence[ComboEntry],
    cards: Mapping[str, Card],
    slug: str,
    change: str | None,
) -> WorkflowManifest:
    # 延遲import避免persona loader載入deck catalog時形成module-level循環。
    from paulsha_cortex.persona.loader import load_catalog

    try:
        catalog = load_catalog()
    except (FileNotFoundError, ValueError) as exc:
        raise DeckCompileError(f"persona catalog 無法載入: {exc}") from exc

    steps: list[WorkflowStep] = []
    for entry in entries:
        card = cards[entry.ref]
        binding = card.persona_binding
        if not binding or binding not in catalog:
            raise DeckCompileError(f"{card.id}: persona_binding {binding!r} 無法解析到catalog")
        persona = catalog[binding]
        phase = _workflow_phase(card, persona)
        steps.append(
            WorkflowStep(
                phase=phase,
                persona=binding,
                card=card.id,
                executor=None,
                model=None,
                domain=None,
                inputs=tuple(_manifest_subst(item, slug, change) for item in card.requires),
                outputs=tuple(_manifest_subst(item, slug, change) for item in card.produces),
                skill_ref=card.skill_ref,
                action=card.action,
                commit_policy=card.commit_policy,
                test_policy=card.test_policy,
            )
        )
    return WorkflowManifest(combo=combo.id, task_slug=slug, steps=tuple(steps))


def _verify_command(card: Card, slug: str, change: str | None) -> str:
    command = f"cortex deck verify {card.id} --task-slug {slug}"
    if _uses_change(card):
        command += f" --change {change}"
    return command


def _gate_verify_commands(
    gate_spine: Sequence,
    cards: Mapping[str, Card],
    slug: str,
    change: str | None,
    selected_refs: frozenset[str],
) -> list[str]:
    commands: list[str] = []
    for gate in gate_spine:
        if gate.after not in selected_refs:
            continue
        card = cards.get(gate.after)
        if card is None:
            raise DeckCompileError(f"gate_spine.after 指向不存在卡片: {gate.after}")
        if not card.produces:
            raise DeckCompileError(f"{card.id}: gate_spine.after 卡片沒有 produces 可驗收")
        for pattern in gate.exists:
            _subst(pattern, slug, change)
        commands.append(_verify_command(card, slug, change))
    return commands


def _band_meets_trigger(band: str, trigger: str) -> bool:
    return BAND_LEVELS.index(band) >= BAND_LEVELS.index(trigger)


def _apply_band_triggered(
    hand: list[ComboEntry],
    band_spine: BandTriggeredSpine | None,
    band: str | None,
) -> tuple[list[ComboEntry], bool]:
    """band 兩層制（#221 maintainer 裁決）：band 未知（None）時保守地包含加掛層
    （行為與今日一致）；band 已知時依 BAND_LEVELS 順序與 trigger 比較。加掛卡片
    依其 depends_on 插入骨幹中最後一個依賴之後，未宣告 depends_on 則附加於尾端。
    """
    if band_spine is None:
        return hand, False
    if band is not None and band not in BAND_LEVELS:
        raise DeckCompileError(f"band 非法值 {band!r}（允許 {list(BAND_LEVELS)}）")
    if band is not None and not _band_meets_trigger(band, band_spine.trigger):
        return hand, False

    refs = {entry.ref for entry in hand}
    result = list(hand)
    for entry in band_spine.cards:
        if entry.ref in refs:
            raise DeckCompileError(f"band_triggered 卡片與骨幹重複: {entry.ref}")
        current_refs = [e.ref for e in result]
        if entry.depends_on:
            positions = [current_refs.index(dep) for dep in entry.depends_on if dep in current_refs]
            pos = max(positions) + 1 if positions else len(result)
        else:
            pos = len(result)
        result.insert(pos, entry)
        refs.add(entry.ref)
    return result, True


def compile_combo(
    combo: Combo,
    cards: Mapping[str, Card],
    task: str,
    *,
    change: str | None = None,
    with_cards: Sequence[str] = (),
    only: Sequence[str] = (),
    allow_external: bool = False,
    plan_ref: str | None = None,
    band: str | None = None,
    repo_root: str | Path | None = None,
) -> CompileResult:
    # #612：`cortex deck compile` 是 operator 手動 CLI，未帶 `repo_root` 時「以當下
    # 工作目錄為準」是它本來的語意（只讀 `.project-policy.yml` 產 verification
    # skeleton，不做任何 git mutation），因此 cwd 退路在這裡**顯式**表態。
    effective_repo_root = (
        Path(repo_root) if repo_root is not None else paths.repo_root(allow_cwd=True)
    )
    slug = slugify_task(task)
    if change is not None:
        change = _validate_change_name(change)
    entries = _resolve_hand(combo, cards, with_cards, only)
    # --only 明確選卡時尊重使用者選擇，不再併入 band 加掛層（#221）。
    band_applied = False
    if not only:
        entries, band_applied = _apply_band_triggered(entries, combo.band_triggered, band)
    external = _check_requires_coverage(entries, cards, allow_external)
    workflow_manifest = _compile_workflow_manifest(combo, entries, cards, slug, change)

    interactive_cards = [cards[entry.ref] for entry in entries if cards[entry.ref].type == "interactive"]
    checklist = tuple(
        f"[{card.id}] {card.skill_ref} → 產出: "
        + ", ".join(_subst(glob, slug, change) for glob in card.produces)
        for card in interactive_cards
    )
    if plan_ref is None:
        plan_ref = _default_plan_ref(entries, cards, slug, change)
    if plan_ref is None:
        plan_ref = _default_plan_ref(combo.cards, cards, slug, change)
    if not plan_ref:
        raise DeckCompileError("無法決定 plan 參照：無 interactive produces，請給 --plan")
    plan_ref = _validate_plan_ref(plan_ref)

    explicit_deps = {entry.ref: entry.depends_on for entry in entries}
    selected_refs = frozenset(entry.ref for entry in entries)
    slices: list[SliceDoc] = []
    core_gate_commands = _gate_verify_commands(combo.gate_spine, cards, slug, change, selected_refs)
    band_gate_commands: list[str] = (
        _gate_verify_commands(combo.band_triggered.gate_spine, cards, slug, change, selected_refs)
        if band_applied and combo.band_triggered is not None
        else []
    )
    verify_commands = core_gate_commands + band_gate_commands
    previous_slice_id: str | None = None

    slice_groups = _group_slices(entries, cards, slug)
    # 只在真的要 emit 至少一份 slice 時才讀 .project-policy.yml；同一次 compile 對所有
    # slice 共用同一份骨架（單次讀檔、單次 warning，不因 slice 數量而重複列印）。
    verification_skeleton = _verification_skeleton(effective_repo_root) if slice_groups else None

    for slice_id, members in slice_groups:
        deps: list[str] = []
        for member in members:
            for dep_ref in explicit_deps.get(member.id, ()):
                dep_card = cards.get(dep_ref)
                if dep_card is None or dep_card.type != "headless":
                    continue
                if dep_card.slice_group:
                    deps.append(f"{slug}-{dep_card.slice_group}")
                else:
                    deps.append(f"{slug}-{dep_card.id}")
        deps = sorted(set(dep for dep in deps if dep != slice_id))
        if not deps and previous_slice_id:
            deps = [previous_slice_id]

        requires = [_subst(glob, slug, change) for member in members for glob in member.requires]
        produces = [_subst(glob, slug, change) for member in members for glob in member.produces]
        requires_block = "".join(f"\n- {item}" for item in requires) or "\n-（無）"
        produces_block = "".join(f"\n- {item}" for item in produces) or "\n-（無，完成偵測=exit sentinel）"
        target_branch = _infer_target_branch(slug, change)
        if not change:
            _warning_default_target_branch(slug, change)
        content = (
            _render_frontmatter(
                slice_id,
                plan_ref,
                deps,
                target_branch,
                verification_skeleton,
            )
            + "\n"
            + f"# {slice_id}\n\n"
            + f"任務：{task}\n"
            + f"combo：{combo.id}（cards: {', '.join(member.id for member in members)}）\n\n"
            + f"requires（翻 auto 前人工確認）：{requires_block}\n\n"
            + f"produces（deck verify 驗收）：{produces_block}\n"
        )
        if not _SLICE_ID_RE.fullmatch(slice_id):
            raise DeckCompileError(
                f"slice_id 非檔名/branch 安全（僅允許 [a-z0-9-]，card id 與 slice_group 不得含路徑分隔）: {slice_id!r}"
            )
        slices.append(SliceDoc(slice_id=slice_id, filename=f"{slice_id}.md", content=content))

        for member in members:
            if member.produces:
                verify_commands.append(_verify_command(member, slug, change))
        previous_slice_id = slice_id

    return CompileResult(
        task_slug=slug,
        slices=tuple(slices),
        checklist=checklist,
        verify_commands=tuple(dict.fromkeys(verify_commands)),
        external=external,
        workflow_manifest=workflow_manifest,
        core_gate_verify_commands=tuple(dict.fromkeys(core_gate_commands)),
        band_gate_verify_commands=tuple(dict.fromkeys(band_gate_commands)),
    )


def emit(result: CompileResult, target_dir: str | Path, *, force: bool = False) -> list[Path]:
    """將 compiled slices 與Manager可重啟載入的manifest一起持久化。"""
    directory = Path(target_dir)
    documents = list(result.slices)
    if result.workflow_manifest is not None:
        documents.append(
            SliceDoc(
                slice_id=f"{result.task_slug}-workflow-manifest",
                filename=f"{result.task_slug}.workflow.json",
                content=json.dumps(
                    result.workflow_manifest.to_dict(),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )
        )
    filenames = [document.filename for document in documents]
    duplicates = sorted({name for name in filenames if filenames.count(name) > 1})
    if duplicates:
        raise DeckCompileError("emit 結果含重複檔名: " + ", ".join(duplicates))
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DeckCompileError(f"emit 目標目錄不可用: {directory}: {exc}") from exc

    conflicts = [filename for filename in filenames if (directory / filename).exists()]
    if conflicts and not force:
        raise DeckCompileError("emit 目標已存在同名 spec（--force 才覆蓋）：" + ", ".join(conflicts))

    written: list[Path] = []
    temp_paths: list[Path] = []
    backups: list[tuple[Path, Path | None]] = []
    try:
        for slice_doc in documents:
            final_path = directory / slice_doc.filename
            if force:
                fd, temp_name = tempfile.mkstemp(
                    prefix=f"{slice_doc.filename}.",
                    suffix=".tmp",
                    dir=directory,
                )
                temp_path = Path(temp_name)
                temp_paths.append(temp_path)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(slice_doc.content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.chmod(temp_path, 0o644)  # mkstemp 預設 0o600，與非 force 路徑一致
                    backup_path: Path | None = None
                    if final_path.exists():
                        backup_fd, backup_name = tempfile.mkstemp(
                            prefix=f"{slice_doc.filename}.",
                            suffix=".bak",
                            dir=directory,
                        )
                        os.close(backup_fd)
                        backup_path = Path(backup_name)
                        backup_path.unlink(missing_ok=True)
                        os.replace(final_path, backup_path)
                    try:
                        os.replace(temp_path, final_path)
                    except OSError:
                        if backup_path is not None and backup_path.exists():
                            os.replace(backup_path, final_path)
                        raise
                    backups.append((final_path, backup_path))
                finally:
                    temp_path.unlink(missing_ok=True)
            else:
                fd = os.open(final_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(slice_doc.content)
                        handle.flush()
                        os.fsync(handle.fileno())
                except Exception:
                    final_path.unlink(missing_ok=True)
                    raise
            written.append(final_path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception as exc:  # 任何寫入期例外（含編碼錯誤）都必須走同一套回滾，否則 finally 會刪掉備份
        if force:
            for final_path, backup_path in reversed(backups):
                if backup_path is not None and backup_path.exists():
                    os.replace(backup_path, final_path)
                else:
                    final_path.unlink(missing_ok=True)
        else:
            for path in written:
                path.unlink(missing_ok=True)
        raise DeckCompileError(f"emit 寫入失敗: {exc}") from exc
    finally:
        for temp_path in temp_paths:
            temp_path.unlink(missing_ok=True)
        for _, backup_path in backups:
            if backup_path is not None:
                backup_path.unlink(missing_ok=True)
    return written
