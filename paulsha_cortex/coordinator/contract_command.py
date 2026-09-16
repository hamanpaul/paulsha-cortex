from __future__ import annotations

import hashlib
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
    catalog: Mapping[str, PersonaContract] | None = None,
    spec_path: str | None = None,
    spec_hash: str | None = None,
    spec_body: str | None = None,
) -> str:
    """強制點 ①：把 persona 契約 render 成 executor-agnostic 純文字 prompt 前言。

    純字串函式、零 I/O：只嵌 plan_path 參照（agent 於 worktree 內自行讀計畫）。
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
    if spec_path is None:
        lines.append("請於本 worktree 內讀取上述 plan 並依 persona 契約邊界執行。")
        return "\n".join(lines)
    if spec_hash is None or spec_body is None:
        raise ValueError("spec_path requires spec_hash and spec_body together (#503)")
    body_hash = hashlib.sha256(spec_body.encode("utf-8")).hexdigest()
    if len(spec_body) <= PINNED_SPEC_BODY_LIMIT and body_hash != spec_hash:
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
    return "\n".join(lines)
