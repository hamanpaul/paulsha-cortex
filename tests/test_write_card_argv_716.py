"""#716 選項 B 的後半：寫入卡的 argv 切換（0819 裁決落地）。

## 這半票在做什麼

`#714`／PR #715 把 codex 內層沙箱改走 legacy landlock 之後，`workspace-write` 導出的
寫入族 permission profile 在該路徑下 **100% panic rc=101**（`linux_run_main.rs:318`，
session 層級、先於任何命令）——寫入卡的「內層沙箱」不是一層防護，是讓每張卡必死的
東西。#716 涵蓋面對照量完（七個面向外層零成本接得住、代價 1 的出口網路由 PR #725
補上並部署、#718 記著今天就存在的四個缺口）之後裁決採 B：

    builder-workspace-write 那一列 → `-s danger-full-access`，且不再附
    `--enable use_legacy_landlock`（該 mode 下旗標是無意義殘留，只會在 job log
    開頭多印一筆 deprecation 的 error item）。

## 本檔釘住什麼

1. `GoldenArgvPins`——planner／reviewer／write-forbidden 三列 argv **byte-identical
   不變**（黃金釘子＝變更前實抓的字面 list，任何 refactor 動到它們當場紅）；
   unsafe-bypass 列也原樣（它是另一個顯式 opt-in，#698 的 hook 信任閘綁在上面）。
2. `WriteCardArgvShapeTests`——`danger-full-access` 列的 argv 形狀，含兩個負向斷言：
   **不帶** legacy landlock 旗標、**不帶** `--dangerously-bypass-*`（後者被
   `build_codex_argv` 與 `--dangerously-bypass-hook-trust` 綁在一起，會連 #698 封住
   的 hook 信任閘一起關掉）。
3. `EmittedModesTests`——`emitted_sandbox_modes()` 的新集合：`workspace-write` 從
   集合消失、`danger-full-access` 進來，順序依表宣告序。

## 量測記錄（0819，本票第 6 條）

headless `codex exec -s danger-full-access` 在真實加固面複本（54 條 property 全量
導出、`</dev/null`、真實 `codex exec` 一次）下**不卡核可閘**：模型自主跑兩條命令
（`git rev-parse HEAD` 因 dubious ownership exit 128 → 自行改
`git -c safe.directory="$PWD"` exit 0）、`turn.completed`、rc=0，全程零 approval
請求，不需要 `--approve-for-me`。逐字證據在該列的
`registry.SANDBOX_MODE_DERIVATION` note 與 PR body。

OS 層語意（真的沒有內層、出口真的關著）由 `trust_root inner-sandbox-probe` 的
a／b 兩段涵蓋，單 UID／無 systemd 的 CI 重現不了，不在此假驗。
"""
from __future__ import annotations

import unittest

from paulsha_cortex.coordinator.launcher import build_codex_argv
from paulsha_cortex.trust_root import registry
from paulsha_cortex.trust_root.registry import JobWriteContract


def _argv(**kwargs) -> list[str]:
    return build_codex_argv(prompt="P", slice_id="wf-716", log_dir="/lg", **kwargs)


# ---------------------------------------------------------------------------
# 1. 三列不變的黃金釘子（＋unsafe-bypass 原樣）
# ---------------------------------------------------------------------------

class GoldenArgvPins(unittest.TestCase):
    """字面 list 是**變更前**（origin/main @ 0e91ed6）實抓的，刻意不從常數組出來。

    從常數組出來的「釘子」會跟著常數一起漂——那就不是釘子了。這三列 0819 實機在
    真實 agent loop 下 rc=0（landlock 真的在擋），#716 B 後半一個位元都不動它們。
    """

    def test_planner_read_only_argv_is_byte_identical(self) -> None:
        self.assertEqual(
            _argv(read_only=True),
            [
                "codex", "exec", "--ignore-user-config", "P", "--json",
                "--sandbox", "read-only",
                "--skip-git-repo-check",
                "--enable", "use_legacy_landlock",
                "-o", "/lg/wf-716.last.json",
            ],
        )

    def test_reviewer_review_only_argv_is_byte_identical(self) -> None:
        self.assertEqual(
            _argv(review_only=True),
            [
                "codex", "exec", "--ignore-user-config", "P", "--json",
                "--sandbox", "read-only",
                "--skip-git-repo-check",
                "--enable", "use_legacy_landlock",
                "-o", "/lg/wf-716.last.json",
            ],
        )

    def test_builder_write_forbidden_argv_is_byte_identical(self) -> None:
        self.assertEqual(
            _argv(write_forbidden=True),
            [
                "codex", "exec", "--ignore-user-config", "P", "--json",
                "--sandbox", "read-only",
                "--enable", "use_legacy_landlock",
                "-o", "/lg/wf-716.last.json",
            ],
        )

    def test_unsafe_bypass_argv_is_byte_identical(self) -> None:
        """`allow_unsafe`（UNSAFE_BYPASS）維持原樣——它是另一個顯式 opt-in。"""

        self.assertEqual(
            _argv(allow_unsafe=True),
            [
                "codex", "exec", "--ignore-user-config", "P", "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                "--dangerously-bypass-hook-trust",
                "-o", "/lg/wf-716.last.json",
            ],
        )


# ---------------------------------------------------------------------------
# 2. danger-full-access 列的 argv 形狀
# ---------------------------------------------------------------------------

class WriteCardArgvShapeTests(unittest.TestCase):

    def test_write_card_argv_shape_is_pinned(self) -> None:
        """寫入卡（預設 builder）的完整 argv 形狀。"""

        self.assertEqual(
            _argv(),
            [
                "codex", "exec", "--ignore-user-config", "P", "--json",
                "--sandbox", "danger-full-access",
                "-o", "/lg/wf-716.last.json",
            ],
        )

    def test_write_card_carries_no_legacy_landlock_flag(self) -> None:
        """**負向斷言一**：不帶 `--enable use_legacy_landlock`。

        `danger-full-access` ＋ 該旗標 → 旗標是無意義殘留（#716 涵蓋面對照 3-6 的
        量測），只會在 job log 開頭多印一筆 deprecation 的 error item——而那句話
        之後只該出現在 read-only 族的 log，出現在寫入卡的 log 就是旗標漏回去了。
        """

        for kwargs in ({}, {"commit_required": True}):
            argv = _argv(**kwargs)
            self.assertNotIn("use_legacy_landlock", argv, kwargs)
            self.assertNotIn("--enable", argv, kwargs)

    def test_write_card_carries_no_dangerously_bypass(self) -> None:
        """**負向斷言二**：不帶 `--dangerously-bypass-approvals-and-sandbox`。

        `build_codex_argv` 把它與 `--dangerously-bypass-hook-trust` 綁在一起——用它
        關沙箱等於連 #698 封住的 hook 信任閘一起關掉。`-s danger-full-access` 只關
        沙箱，核可機制與 hook 信任都不動（0819 量測：headless 下也不卡核可閘）。
        """

        for kwargs in ({}, {"commit_required": True}):
            argv = _argv(**kwargs)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv, kwargs)
            self.assertNotIn("--dangerously-bypass-hook-trust", argv, kwargs)

    def test_commit_required_write_card_only_adds_git_write_dirs(self) -> None:
        """commit-required 與預設 builder 只差 `--add-dir`（linked worktree 的 git
        寫入面），mode 與內層附掛完全相同——worktree=None 時兩者 byte-identical。"""

        self.assertEqual(_argv(), _argv(commit_required=True))


# ---------------------------------------------------------------------------
# 3. 探針輸入的 mode 集合
# ---------------------------------------------------------------------------

class EmittedModesTests(unittest.TestCase):

    def test_the_emitted_mode_set_after_b(self) -> None:
        """`workspace-write` 從集合消失、`danger-full-access` 進來（#716 B 後半）。

        這個 tuple 是 `permgen.build_inner_sandbox_probe()` 的輸入；順序依
        `SANDBOX_MODE_DERIVATION` 宣告序去重，因此是確定性的。
        """

        self.assertEqual(
            registry.emitted_sandbox_modes(),
            (
                registry.SANDBOX_MODE_READ_ONLY,
                registry.SANDBOX_MODE_DANGER_FULL_ACCESS,
            ),
        )
        self.assertNotIn(
            registry.SANDBOX_MODE_WORKSPACE_WRITE, registry.emitted_sandbox_modes()
        )

    def test_the_write_card_mode_never_degrades_to_read_only(self) -> None:
        """寫入卡靜默降成 read-only 會把真的要寫檔的卡弄壞（保守方向的另一半）。"""

        self.assertEqual(
            registry.sandbox_mode_for(JobWriteContract.BUILDER_WORKSPACE_WRITE),
            registry.SANDBOX_MODE_DANGER_FULL_ACCESS,
        )
        self.assertTrue(
            registry.SANDBOX_MODE_DERIVATION[-1].grants_filesystem_write
        )
