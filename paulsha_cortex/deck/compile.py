from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from paulsha_cortex.config import paths
from paulsha_cortex.coordinator.workflow import WorkflowManifest, WorkflowStep

from .schema import Card, Combo, ComboEntry


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


def _render_frontmatter(slice_id: str, plan_ref: str, deps: Sequence[str]) -> str:
    depends_on = "[" + ", ".join(deps) + "]" if deps else "[]"
    lines = [
        "---",
        "dispatch: hold",
        f"slice_id: {slice_id}",
        f"plan: {json.dumps(plan_ref, ensure_ascii=False)}",
        f"depends_on: {depends_on}",
        "target_branch: null",
        "verification: null",
        "parse_error: null",
    ]
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
    combo: Combo,
    cards: Mapping[str, Card],
    slug: str,
    change: str | None,
    selected_refs: frozenset[str],
) -> list[str]:
    commands: list[str] = []
    for gate in combo.gate_spine:
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
) -> CompileResult:
    slug = slugify_task(task)
    if change is not None:
        change = _validate_change_name(change)
    entries = _resolve_hand(combo, cards, with_cards, only)
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
    verify_commands = _gate_verify_commands(combo, cards, slug, change, selected_refs)
    previous_slice_id: str | None = None

    for slice_id, members in _group_slices(entries, cards, slug):
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
        content = (
            _render_frontmatter(slice_id, plan_ref, deps)
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
