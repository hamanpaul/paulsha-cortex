"""`cortex skill`：skill usage ledger 檢視與 park/janitor operator 入口（issue #204）。

`inspect` / `list-proposals` 唯讀免審核；`propose` 手動觸發 janitor tick（等同
生產排程未來會呼叫的同一函式）；`park` / `approve-proposal` / `restore` 需
operator 明確帶 `--approved-by` 觸發——janitor 本身（`propose`）只開 proposal，
不會、也不能核准到底。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from paulsha_cortex.config import paths
from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, Card, load_cards

from . import skill_janitor
from . import skill_ledger

_HELP = """\
usage: cortex skill <inspect|list-proposals|propose|approve-proposal|park|restore> ...

skill 治理唯讀 / 操作命令（issue #204）：
  inspect          列出每個 skill 的使用量統計與目前 cold/park 判定（唯讀）
  list-proposals   列出 janitor 產生的 park proposal（唯讀）
  propose          手動跑一次 janitor tick：偵測 cold skill 並開 proposal（不動 park state）
  approve-proposal 核准既有 pending proposal 並套用 park（需 --approved-by）
  park             不經 proposal，直接手動 park 一個 skill（需 --approved-by）
  restore          把 skill 移出 park 清單（需 --approved-by）

run 'cortex skill <command> --help' for command-specific help.
"""


def _load_cards(cards_path: str | None) -> dict[str, Card]:
    return load_cards(cards_path or DEFAULT_CARDS_PATH)


def _print(payload: object, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(payload)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortex skill", add_help=False)
    sub = parser.add_subparsers(dest="skill_command", required=True)

    common_thresholds = argparse.ArgumentParser(add_help=False)
    common_thresholds.add_argument("--cards", default=None, help="覆寫 deck cards.yaml 路徑")
    common_thresholds.add_argument(
        "--min-samples", type=int, default=skill_janitor.DEFAULT_MIN_SAMPLES,
        help=f"cold 判定最低樣本數（預設 {skill_janitor.DEFAULT_MIN_SAMPLES}）",
    )
    common_thresholds.add_argument(
        "--observation-window-days", type=int, default=skill_janitor.DEFAULT_OBSERVATION_WINDOW_DAYS,
        help=f"cold 判定觀測窗天數（預設 {skill_janitor.DEFAULT_OBSERVATION_WINDOW_DAYS}）",
    )

    inspect = sub.add_parser("inspect", parents=[common_thresholds], help="唯讀：usage 統計 + cold/park 判定")
    inspect.add_argument("--json", action="store_true")

    list_proposals = sub.add_parser("list-proposals", help="唯讀：列出 park proposal")
    list_proposals.add_argument("--json", action="store_true")

    propose = sub.add_parser("propose", parents=[common_thresholds], help="手動觸發一次 janitor tick")
    propose.add_argument("--reason", default="cold-skill-auto-detected")
    propose.add_argument("--json", action="store_true")

    approve = sub.add_parser("approve-proposal", help="核准 pending proposal 並套用 park")
    approve.add_argument("proposal_id")
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--cards", default=None)
    approve.add_argument("--json", action="store_true")

    park = sub.add_parser("park", help="不經 proposal，手動 park 一個 skill")
    park.add_argument("skill_id")
    park.add_argument("--reason", required=True)
    park.add_argument("--approved-by", required=True)
    park.add_argument("--cards", default=None)
    park.add_argument("--json", action="store_true")

    restore = sub.add_parser("restore", help="把 skill 移出 park 清單")
    restore.add_argument("skill_id")
    restore.add_argument("--approved-by", required=True)
    restore.add_argument("--reason", default=None)
    restore.add_argument("--json", action="store_true")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        sys.stdout.write(_HELP)
        return 0

    parsed = _build_parser().parse_args(args)

    if parsed.skill_command == "inspect":
        cards = _load_cards(parsed.cards)
        usage = skill_ledger.load_usage_summary(paths.skill_usage_ledger_path())
        cold = skill_janitor.find_cold_skills(
            usage, cards, min_samples=parsed.min_samples, observation_window_days=parsed.observation_window_days
        )
        park_state = skill_janitor.load_park_state(paths.skill_park_state_path())
        parked_ids = sorted(park_state["parked"])
        if parsed.json:
            payload = {
                "schema": "cortex-skill-inspect/v1",
                "cards": sorted(cards),
                "usage": {
                    card_id: {
                        "sample_count": stats.sample_count,
                        "last_used_at": stats.last_used_at,
                        "outcome_counts": stats.outcome_counts,
                    }
                    for card_id, stats in usage.items()
                },
                "cold_skills": cold,
                "parked": parked_ids,
            }
            _print(payload, as_json=True)
        else:
            for card_id in sorted(cards):
                stats = usage.get(card_id)
                marker = "cold" if card_id in cold else ("parked" if card_id in parked_ids else "-")
                count = stats.sample_count if stats is not None else 0
                last_used = stats.last_used_at if stats is not None else "-"
                print(f"{card_id}\t{cards[card_id].card_class}\t{count}\t{last_used}\t{marker}")
        return 0

    if parsed.skill_command == "list-proposals":
        proposals = skill_janitor.list_proposals(paths.skill_park_proposals_root())
        if parsed.json:
            _print(proposals, as_json=True)
        else:
            for proposal in proposals:
                print(
                    f"{proposal.get('proposal_id')}\t{proposal.get('skill_id')}\t"
                    f"{proposal.get('status')}\t{proposal.get('reason')}\t{proposal.get('created_at')}"
                )
        return 0

    if parsed.skill_command == "propose":
        cards = _load_cards(parsed.cards)
        result = skill_janitor.run_janitor_tick(
            cards=cards,
            ledger_path=paths.skill_usage_ledger_path(),
            proposals_dir=paths.skill_park_proposals_root(),
            min_samples=parsed.min_samples,
            observation_window_days=parsed.observation_window_days,
            reason=parsed.reason,
        )
        _print(result, as_json=parsed.json)
        return 0

    if parsed.skill_command == "approve-proposal":
        cards = _load_cards(parsed.cards)
        try:
            state = skill_janitor.apply_park(
                parsed.proposal_id,
                approved_by=parsed.approved_by,
                cards=cards,
                proposals_dir=paths.skill_park_proposals_root(),
                state_path=paths.skill_park_state_path(),
            )
        except ValueError as exc:
            print(f"錯誤: {exc}", file=sys.stderr)
            return 1
        _print(state, as_json=parsed.json)
        return 0

    if parsed.skill_command == "park":
        cards = _load_cards(parsed.cards)
        try:
            state = skill_janitor.manual_park(
                parsed.skill_id,
                reason=parsed.reason,
                approved_by=parsed.approved_by,
                cards=cards,
                state_path=paths.skill_park_state_path(),
            )
        except ValueError as exc:
            print(f"錯誤: {exc}", file=sys.stderr)
            return 1
        _print(state, as_json=parsed.json)
        return 0

    if parsed.skill_command == "restore":
        try:
            state = skill_janitor.restore(
                parsed.skill_id,
                approved_by=parsed.approved_by,
                state_path=paths.skill_park_state_path(),
                reason=parsed.reason,
            )
        except ValueError as exc:
            print(f"錯誤: {exc}", file=sys.stderr)
            return 1
        _print(state, as_json=parsed.json)
        return 0

    sys.stdout.write(_HELP)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
