from __future__ import annotations

import hashlib
import json
from typing import Mapping

from paulsha_cortex.persona import render
from paulsha_cortex.persona.contract import PersonaContract

# #503：prompt 內逐字交付的 spec 內容上限（bytes 換算後的字元數）。超過就截斷並明示
# 「其餘請讀 [SPEC] 路徑」；hash 永遠是完整檔案的 sha256，不是截斷後的內容。
PINNED_SPEC_BODY_LIMIT = 24_000

PINNED_SPEC_DIRECTIVE = (
    "以下 [SPEC BODY] 是 Manager 交付的 pinned spec 逐字內容（sha256 見 [SPEC]），"
    "它是本 slice 的唯一 authority，含 controller／operator 後補的 recovery 指示與必做的"
    "回歸測試；plan 只是輔助材料，spec 與 plan 衝突時以 spec 為準。開工前先讀完整份，"
    "不得只依 task id 與 plan 推測需求。"
)


def _truncate_spec_body(body: str) -> str:
    if len(body) <= PINNED_SPEC_BODY_LIMIT:
        return body
    return (
        body[:PINNED_SPEC_BODY_LIMIT]
        + f"\n[… truncated at {PINNED_SPEC_BODY_LIMIT} chars; read the full file at the [SPEC] path …]"
    )


def build_dispatch_prompt(
    role: str,
    *,
    task: str,
    plan_path: str,
    worktree_root: str | None = None,
    catalog: Mapping[str, PersonaContract] | None = None,
    spec_path: str | None = None,
    spec_hash: str | None = None,
    spec_body: str | None = None,
) -> str:
    """強制點 ①：把 persona 契約 render 成 executor-agnostic 純文字 prompt 前言。

    純字串函式、零 I/O：嵌 plan_path 參照；呼叫端提供已解析 worktree_root 時，附上目錄邊界。
    未知 role → ValueError（由 render_contract_prompt 冒泡）。
    不含任何 shell/executor 包裝；executor argv 由 AgentLauncher 各自組裝（launcher.py）。

    #503：builder 角色**必須**同時拿到 pinned spec 的路徑、sha256 與逐字內容——task id
    加 plan 路徑不是 authority（controller 重釘 spec 加進的 recovery 指示會在模型邊界被
    靜默丟掉）。缺任一項即 ValueError，dispatch 端據此拒派。非 builder 角色（既有 legacy
    呼叫端）沿用舊形狀；帶了 spec 就一樣附上。``spec_hash`` 必須是呼叫端對**完整** spec
    bytes 算出的 sha256，這裡只核對它與 ``spec_body`` 相符（body 未截斷時）。
    """
    contract_prompt = render.render_contract_prompt(role, catalog)
    if role == "builder" and (not spec_path or not spec_hash or spec_body is None):
        raise ValueError(
            f"builder dispatch for {task!r} requires the pinned spec (path, sha256, body); "
            "task id + plan path alone are not authority (#503)"
        )
    lines = [
        contract_prompt,
        "",
        f"[TASK] {task}",
        f"[PLAN: {plan_path}]",
    ]
    if worktree_root is not None:
        lines.extend(
            [
                "",
                "[AUTHORITATIVE WORKTREE ROOT — JSON string]",
                json.dumps(worktree_root, ensure_ascii=False),
                "所有 repository 檔案的讀取、寫入與命令都必須留在此根目錄內；命令 cwd 設為此目錄，repo 內目標使用相對路徑。",
                "不得讀取、寫入或執行主 checkout（operator/base checkout）中的任何內容。",
                "若路徑遭拒，請依目前 cwd 重新解析成此 worktree 內的相對路徑，不要重試遭拒的絕對路徑。",
            ]
        )
    if spec_path is None:
        lines.append("請於本 worktree 內讀取上述 plan 並依 persona 契約邊界執行。")
        return "\n".join(lines)
    if spec_hash is None or spec_body is None:
        raise ValueError("spec_path requires spec_hash and spec_body together (#503)")
    # ``spec_body`` 是完整檔案內容（截斷只發生在 prompt 輸出），因此 hash 一律核對，
    # 不因超過上限而放行——否則 [SPEC] 宣稱的 sha256 與實際交付來源可能脫鉤。
    body_hash = hashlib.sha256(spec_body.encode("utf-8")).hexdigest()
    if body_hash != spec_hash:
        raise ValueError(
            f"pinned spec body for {task!r} does not match spec_hash (#503): "
            f"delivered={body_hash} pinned={spec_hash}"
        )
    lines.extend(
        [
            f"[SPEC: {spec_path} sha256={spec_hash}]",
            "請於本 worktree 內讀取上述 plan 並依 persona 契約邊界執行。",
            PINNED_SPEC_DIRECTIVE,
            "[SPEC BODY BEGIN]",
            _truncate_spec_body(spec_body),
            "[SPEC BODY END]",
        ]
    )
    if worktree_root is not None and len(spec_body) > PINNED_SPEC_BODY_LIMIT:
        # 截斷時唯一的完整來源是 [SPEC] 路徑；它可能只存在於主 checkout，
        # 因此對這一個檔案開唯讀例外，否則 worktree 邊界會讓 builder 讀不到完整 spec。
        lines.append(
            "例外：上方 [SPEC] 列出的 pinned spec 檔案可唯讀讀取（即使位於主 checkout），"
            "讀取後以 sha256 核對；不得寫入或讀取主 checkout 的其他任何檔案。"
        )
    return "\n".join(lines)
