"""#673：seccomp 過濾**語意**是加固剖面之外的第二個維度，且加固面複本必須全量導出。

本票開票時的診斷是「`SystemCallFilter=@system-service` 讓 codex／copilot rc=1，需要
加 `@sandbox`」。實機量測推翻了它的每一步：

* 被過濾的是 **`pkey_alloc`**（x86_64 syscall 330，systemd 歸在 `@pkey`），不是
  `landlock_*`／`seccomp`（`@sandbox`）——kernel audit `type=1326 … syscall=330`
  直接證據，全程沒有任何一筆 landlock 的 record。加 `@sandbox` 實測毫無作用。
* 真 unit 上四支 executor **全部 rc=0**：同一份 unit 上的 `SystemCallErrorNumber=EPERM`
  把「被過濾 ⇒ SIGSYS 殺行程」變成「被過濾 ⇒ 回 EPERM」，V8 走 fallback。
* 本票的 repro 是一份**手抄的十 property 複本**，抄了 `SystemCallFilter=` 卻漏抄
  `SystemCallErrorNumber=EPERM`——複本比 production **更嚴格**，於是量出一個
  production 沒有的失敗。#638（單 UID 讓 ACL 斷言真空）、#657、本票是同一族事故的
  第一、二、三次；本票的方向是**假紅**，前兩次是假綠。

因此本檔守兩件事：

1. **不放寬**：八份 unit 的 `SystemCallFilter=` 逐字是 `@system-service`，
   `@sandbox`／`@pkey`／`pkey_alloc` 一個都不得出現。
2. **承重的那一條不得被靜默拿掉**：`SystemCallErrorNumber` 是全部 node 型程式的存活
   條件，它與 `SystemCallFilter` 同列 :data:`permgen.PROFILE_LOCKED_KEYS`，任何剖面
   都不得分岔；`filtered_syscalls` 非空的程式其執行面若變成致命語意，permgen **import
   時就炸**。第 5 節證明這道檢查不是空的。

**OS 層語意（syscall 是否真的被擋、被擋時致不致命）在單 UID／無 systemd 的 CI 環境
重現不了**——第 6 節以具名 skip ＋ 完整理由標示，不留假綠（#638／#657 的教訓、
PR #671 的做法）。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paulsha_cortex.trust_root import permgen  # noqa: E402
from paulsha_cortex.trust_root.permgen import Principal  # noqa: E402

_REAL_SURFACE = bool(os.environ.get("PSC_TEST_REAL_HARDENING"))

#: 實機量到的、被 `@system-service` 過濾掉的 syscall（#673）。
_MEASURED_SYSCALL = "pkey_alloc"

#: 推測過、但**沒有量測支撐**的 systemd syscall 群。不得出現在任何 unit 上。
#:
#: **#714 把 `@sandbox`／`landlock` 從這張表移到下面 `_REQUIRED_FILTER_TOKENS`**：
#: #673 的結論「`@sandbox` 對症狀完全無效」在它量的那個症狀上**至今成立**——V8 的
#: `pkey_alloc` 與 landlock 毫無關係，那次的 audit 也逐字沒有任何 `landlock_*` record，
#: 處置仍然是非致命的過濾語意而不是放行 syscall。#714 量到的是**另一個**症狀
#: （executor 自帶的內層沙箱裝不上），而那個症狀的成因確實就是這四支被擋。
#: 兩個結論不衝突：一個說「它治不好 A」，一個說「它是 B 的必要條件」。
#:
#: **`@mount` 留在拒絕清單上，而且是 #714 加的**：那是路線 A（保留 bubblewrap）第 4 道
#: 牆的解法，實機量到有效——但它是「讓 job 有能力 mount」，與本檔案第 1 節的整個立場
#: 相反。把它寫成一條不變式，等於把「我們沒有走 A」這個決定變成機器擋得住的東西。
_REJECTED_FILTER_TOKENS = ("@pkey", "pkey_alloc", "@mount", "pivot_root")

#: #714 實機量到**必須**放行的群組（方向與放寬相反：只能讓行程把自己關得更緊）。
_REQUIRED_FILTER_TOKENS = ("@sandbox",)


def _service_directives(content: str) -> list[tuple[str, str]]:
    """unit 內容 → `[Service]` 段的 (key, value) 序列（保留重複鍵）。"""

    out: list[tuple[str, str]] = []
    section = ""
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if section != "Service" or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out.append((key.strip(), value.strip()))
    return out


def _all_units() -> dict[str, str]:
    """本 repo 產生的**全部八份** unit：6 份 job 模板 ＋ manager ＋ monitor。"""

    scheme = permgen.SCHEMES["four-way"]
    units = {
        "manager": permgen.build_manager_unit(scheme).content,
        "monitor": permgen.build_monitor_unit(scheme).content,
    }
    for principal in (Principal.BUILDER, Principal.REVIEWER, Principal.GATE):
        for profile in permgen.HARDENING_PROFILES:
            unit = permgen.build_job_unit(scheme, principal=principal, profile=profile)
            units[unit.unit_name] = unit.content
    return units


# ---------------------------------------------------------------------------
# 1. 不放寬：八份 unit 的 seccomp 白名單逐字不變
# ---------------------------------------------------------------------------

class NoWideningTests(unittest.TestCase):

    def test_all_eight_units_exist(self) -> None:
        """先確認樣本數就是八——少算一份，下面每一條都會漏驗它。"""

        self.assertEqual(len(_all_units()), 8, sorted(_all_units()))

    def test_every_unit_keeps_one_and_the_same_system_service_filter(self) -> None:
        """八份 unit 的白名單**逐字相同**，且等於加固表上的那一個值。

        #714 之前這一條斷言的是字面量 `@system-service`。改成「與加固表相等 ＋ 八份
        一致」不是放寬檢查，而是把它對準真正承重的性質：**白名單只有一個值**（＝沒有
        任何剖面分岔它，這正是 `PROFILE_LOCKED_KEYS` 的意思）。釘死字面量只會讓「全域
        加一個群組」與「某份剖面偷偷多開一支」在測試上長得一樣——兩者都是紅的，而後者
        才是那把鎖要擋的。字面量本身改由下面兩條（拒絕清單／必要清單）逐 token 守住。
        """

        table = {key: value for key, value, _why in permgen._HARDENING}
        expected = table["SystemCallFilter"]
        for name, content in _all_units().items():
            values = [v for k, v in _service_directives(content)
                      if k == "SystemCallFilter"]
            self.assertEqual(values, [expected], name)

    def test_no_unit_mentions_the_rejected_syscall_sets_as_a_filter_value(self) -> None:
        """`@pkey`／`pkey_alloc`／`@mount`／`pivot_root` 都不得被放進過濾器。

        它們可以出現在**註解**裡（#673／#714 的量測結論就寫在那），但一旦出現在指令值
        上就是「有更便宜的解卻選了放寬」。
        """

        for name, content in _all_units().items():
            for key, value in _service_directives(content):
                for token in _REJECTED_FILTER_TOKENS:
                    self.assertNotIn(
                        token, value,
                        f"{name} 的 {key}= 值出現 {token}——@pkey／pkey_alloc 沒有必要"
                        "（#673：處置是非致命的過濾語意，不是放行 syscall），"
                        "@mount／pivot_root 則是 #714 明確否決的路線 A"
                        "（讓 job 有能力 mount，與整個外層加固面的立場相反）",
                    )

    def test_every_unit_allows_the_measured_inner_sandbox_syscalls(self) -> None:
        """#714：`@sandbox` 必須在**每一份** unit 的白名單上。

        少了它，executor 自帶的內層沙箱裝不上（實機：codex 的 legacy landlock 路徑在
        `SeccompInstall … EPERM` 上 panic），job 於是只剩 systemd 這一層——而那件事
        在 log 上長得像「模型自己決定回 needs_human」。
        """

        for name, content in _all_units().items():
            values = [v for k, v in _service_directives(content)
                      if k == "SystemCallFilter"]
            self.assertEqual(len(values), 1, name)
            for token in _REQUIRED_FILTER_TOKENS:
                self.assertIn(token, values[0].split(), name)

    def test_every_unit_carries_the_non_fatal_filter_semantics(self) -> None:
        for name, content in _all_units().items():
            table = dict(_service_directives(content))
            self.assertEqual(table.get(permgen.SECCOMP_FATALITY_KEY), "EPERM", name)
            self.assertFalse(permgen.seccomp_filter_is_fatal(table), name)


# ---------------------------------------------------------------------------
# 2. 剖面導出：哪個 executor → 哪個剖面 → 哪些 SystemCallFilter 值
# ---------------------------------------------------------------------------

class ProfileDerivationTests(unittest.TestCase):

    def test_executor_to_profile_to_filter_values(self) -> None:
        expected_profile = {
            "codex": "jit", "copilot": "jit", "claude": "strict", "agy": "strict",
        }
        for executor, profile_id in expected_profile.items():
            profile = permgen.executor_hardening_profile(executor)
            self.assertEqual(profile.profile_id, profile_id, executor)
            table = profile.effective()
            # 剖面**不影響** seccomp 這兩項——這正是 #673 的重點：兩者正交。
            # #714 起白名單多了 `@sandbox`（全域、方向相反），但「剖面不影響它」
            # 這條性質**逐字不變**——所以這裡改成與加固表比對，不是與字面量比對。
            hardening = {key: value for key, value, _why in permgen._HARDENING}
            self.assertEqual(
                table["SystemCallFilter"], hardening["SystemCallFilter"], executor
            )
            self.assertEqual(table[permgen.SECCOMP_FATALITY_KEY], "EPERM", executor)

    def test_profile_mapping_is_derived_not_a_second_roster(self) -> None:
        """對應表由 `EXECUTOR_TOOLS` 機械導出，不是人工列出來的第二張表。"""

        self.assertEqual(
            dict(permgen.EXECUTOR_HARDENING_PROFILE),
            {
                tool.name: ("jit" if tool.needs_node else "strict")
                for tool in permgen.EXECUTOR_TOOLS
            },
        )

    def test_unknown_executor_still_fails_closed(self) -> None:
        with self.assertRaises(permgen.UnknownExecutorProfileError):
            permgen.executor_hardening_profile("gpt-9000")


# ---------------------------------------------------------------------------
# 3. 新維度的資料模型：filtered_syscalls 與它的機械導出
# ---------------------------------------------------------------------------

class FilteredSyscallSurfaceTests(unittest.TestCase):

    def test_measured_programs_are_exactly_the_node_shaped_ones(self) -> None:
        """`filtered_syscalls` 非空者 ＝ 有 audit record 背書的那四支。

        它今天與 `needs_node` 同集合（成因同樣是 V8），但**是分開量、分開存的**：
        兩者的處置方向相反（換剖面 vs. 鎖定過濾語意），適用面也不同（本欄位涵蓋
        跑在 Manager unit 上、根本沒有剖面的 `openspec`）。
        """

        measured = {
            tool.name for tool in permgen.TOOLCHAIN_PROGRAMS if tool.filtered_syscalls
        }
        self.assertEqual(measured, {"codex", "copilot", "srt", "openspec"})
        for tool in permgen.TOOLCHAIN_PROGRAMS:
            if tool.filtered_syscalls:
                self.assertEqual(tool.filtered_syscalls, (_MEASURED_SYSCALL,), tool.name)

    def test_native_elf_executors_have_no_measured_filtered_syscall(self) -> None:
        for name in ("claude", "agy"):
            tool = next(t for t in permgen.EXECUTOR_TOOLS if t.name == name)
            self.assertEqual(tool.filtered_syscalls, (), name)

    def test_surfaces_cover_executors_and_the_manager_surface(self) -> None:
        surfaces = {
            (item.program, item.surface) for item in permgen.filtered_syscall_surfaces()
        }
        self.assertEqual(
            surfaces,
            {
                ("codex", "codex"),          # executor ⇒ 自己的剖面
                ("copilot", "copilot"),
                ("srt", "claude"),           # 非 executor ⇒ 消費者的剖面
                ("openspec", permgen.MANAGER_SURFACE),  # ⇒ Manager unit（沒有剖面）
            },
        )

    def test_no_surface_is_fatal(self) -> None:
        for item in permgen.filtered_syscall_surfaces():
            self.assertFalse(item.fatal, f"{item.program} @ {item.detail}")


# ---------------------------------------------------------------------------
# 4. 鎖定鍵：任何剖面都不得分岔 seccomp 白名單與它的過濾語意
# ---------------------------------------------------------------------------

class LockedKeyTests(unittest.TestCase):

    def test_locked_and_divergence_keys_are_disjoint(self) -> None:
        self.assertEqual(
            permgen.PROFILE_LOCKED_KEYS & permgen.PROFILE_DIVERGENCE_KEYS, frozenset()
        )

    def test_locked_keys_exist_in_the_hardening_table(self) -> None:
        known = {key for key, _v, _w in permgen._HARDENING}
        self.assertLessEqual(permgen.PROFILE_LOCKED_KEYS, known)

    def test_locked_keys_are_the_seccomp_pair(self) -> None:
        self.assertEqual(
            permgen.PROFILE_LOCKED_KEYS,
            frozenset({"SystemCallFilter", permgen.SECCOMP_FATALITY_KEY}),
        )

    def test_no_profile_overrides_a_locked_key(self) -> None:
        for profile in permgen.HARDENING_PROFILES:
            self.assertEqual(
                set(profile.overrides) & permgen.PROFILE_LOCKED_KEYS, set(), profile.profile_id
            )

    def test_seccomp_fatality_semantics(self) -> None:
        """systemd 語意：未設／空值 ⇒ SIGSYS 殺行程；設 errno ⇒ 回錯誤碼續跑。"""

        self.assertTrue(permgen.seccomp_filter_is_fatal({}))
        self.assertTrue(permgen.seccomp_filter_is_fatal(
            {permgen.SECCOMP_FATALITY_KEY: ""}))
        self.assertTrue(permgen.seccomp_filter_is_fatal(
            {permgen.SECCOMP_FATALITY_KEY: "   "}))
        self.assertFalse(permgen.seccomp_filter_is_fatal(
            {permgen.SECCOMP_FATALITY_KEY: "EPERM"}))


# ---------------------------------------------------------------------------
# 5. 這道檢查不是空的——把加固表改成致命語意，import 時的驗證必須炸
# ---------------------------------------------------------------------------

class ToleranceGuardIsNotVacuousTests(unittest.TestCase):
    """守衛測試：證明第 3／4 節的綠是**有人在守**，不是恰好成立。

    `_validate_seccomp_tolerance()` 平時只在 import 時跑一次，因此若哪天有人把
    `SystemCallErrorNumber` 刪掉，唯一會叫的就是它——本節確認它真的會叫。
    """

    def _with_hardening(self, table: tuple[tuple[str, str, str], ...]):
        original = permgen._HARDENING
        permgen._HARDENING = table
        self.addCleanup(lambda: setattr(permgen, "_HARDENING", original))

    def test_removing_the_error_number_trips_the_import_time_check(self) -> None:
        """整行刪掉 ⇒ 撞「鎖定鍵必須存在於加固表」那一關（更早、訊息更直接）。"""

        self._with_hardening(tuple(
            row for row in permgen._HARDENING
            if row[0] != permgen.SECCOMP_FATALITY_KEY
        ))
        with self.assertRaises(ValueError) as caught:
            permgen._validate_seccomp_tolerance()
        self.assertIn(permgen.SECCOMP_FATALITY_KEY, str(caught.exception))

    def test_blanking_the_error_number_trips_the_tolerance_check(self) -> None:
        """把值清空（＝systemd 的致命語意）⇒ 撞 #673 的容忍度不變式。

        這是更危險的改法：鍵還在、diff 看起來只是「移除一個放寬」，但 codex／
        copilot／srt／openspec 會在**全部**執行面上同時靜默死。
        """

        self._with_hardening(tuple(
            (key, "" if key == permgen.SECCOMP_FATALITY_KEY else value, why)
            for key, value, why in permgen._HARDENING
        ))
        with self.assertRaises(ValueError) as caught:
            permgen._validate_seccomp_tolerance()
        message = str(caught.exception)
        # 錯誤訊息必須把處置講清楚，否則下一個人會「順手把 syscall 加進白名單」。
        self.assertIn("#673", message)
        self.assertIn(permgen.SECCOMP_FATALITY_KEY, message)
        self.assertIn("openspec", message)   # Manager 面也必須被涵蓋
        self.assertIn("codex", message)

    def test_the_real_table_passes(self) -> None:
        permgen._validate_seccomp_tolerance()   # 不得拋


# ---------------------------------------------------------------------------
# 6. 加固面複本：從**已落檔的 unit** 全量導出（本票真正的修法）
# ---------------------------------------------------------------------------

class UnitReplicaTests(unittest.TestCase):
    """runbook 與本檔共用 `unit_replica_properties()`——加固面只有一份定義。"""

    def setUp(self) -> None:
        self.scheme = permgen.SCHEMES["four-way"]
        self.unit = permgen.build_job_unit(
            self.scheme, principal=Principal.REVIEWER,
            profile=permgen.HARDENING_PROFILES_BY_ID["jit"],
        )

    def test_replica_contains_every_hardening_key(self) -> None:
        props = permgen.unit_replica_properties(self.unit.content, instance="probe")
        got = {prop.split("=", 2)[1] for prop in props}
        for key, _value, _why in permgen._HARDENING:
            self.assertIn(key, got, key)

    def test_replica_carries_the_account_env_and_writable_face(self) -> None:
        """手抄子集漏的正是這幾類——它們同樣是「加固面」的一部分。"""

        props = permgen.unit_replica_properties(self.unit.content, instance="probe")
        joined = "\n".join(props)
        account = self.scheme.resolve(Principal.REVIEWER)
        self.assertIn(f"--property=User={account}", props)
        self.assertIn(f"--property=Group={account}", props)
        self.assertIn("--property=WorkingDirectory=", joined)
        self.assertIn("--property=Environment=HOME=", joined)
        for rwp in self.unit.read_write_paths:
            self.assertIn(f"--property=ReadWritePaths={rwp.replace('%i', 'probe')}", props)

    def test_replica_drops_only_the_execution_face(self) -> None:
        props = permgen.unit_replica_properties(self.unit.content, instance="probe")
        keys = {prop.split("=", 2)[1] for prop in props}
        self.assertEqual(keys & permgen.UNIT_REPLICA_EXCLUDED_KEYS, set())
        # 排除表擋掉的必須真的是那幾項，而不是「剛好 unit 裡沒有」。
        directive_keys = {key for key, _v in _service_directives(self.unit.content)}
        self.assertIn("ExecStart", directive_keys)
        self.assertNotIn("ExecStart", keys)

    def test_instance_specifier_is_expanded(self) -> None:
        builder = permgen.build_job_unit(self.scheme, principal=Principal.BUILDER)
        props = permgen.unit_replica_properties(builder.content, instance="job-77")
        joined = "\n".join(props)
        self.assertNotIn("%i", joined)
        self.assertIn("job-77", joined)

    def test_incomplete_unit_is_refused_rather_than_silently_weakened(self) -> None:
        """**本票的核心修法**：複本比 production 弱時**不產出**，而不是產出半套。"""

        crippled = "\n".join(
            line for line in self.unit.content.splitlines()
            if not line.startswith(f"{permgen.SECCOMP_FATALITY_KEY}=")
        )
        with self.assertRaises(permgen.UnitReplicaDriftError) as caught:
            permgen.unit_replica_properties(crippled)
        self.assertIn(permgen.SECCOMP_FATALITY_KEY, str(caught.exception))

    def test_unknown_specifier_is_refused(self) -> None:
        with self.assertRaises(permgen.UnitReplicaDriftError):
            permgen.unit_replica_properties(
                "[Service]\nWorkingDirectory=/srv/%n\n", require_hardening=False
            )

    def test_escaped_percent_is_not_mistaken_for_a_specifier(self) -> None:
        """`%%i` 的正確結果是字面 `%i`，**不是** `%<instance>`。

        錯了不會報錯，只會讓探針的加固面與 unit 悄悄不同——正是本票要根除的那類。
        """

        props = permgen.unit_replica_properties(
            "[Service]\nEnvironment=FMT=%%i-%i\n",
            instance="job-9", require_hardening=False,
        )
        self.assertEqual(props, ("--property=Environment=FMT=%i-job-9",))

    def test_all_eight_units_survive_a_round_trip(self) -> None:
        for name, content in _all_units().items():
            props = permgen.unit_replica_properties(content, instance="probe")
            self.assertGreaterEqual(len(props), 30, name)
            # #714：白名單的值改由加固表導出（多了 `@sandbox`）。這一條要釘的是
            # 「複本帶得出白名單那一條」，不是那一條長什麼樣——字面量已由第 1 節
            # 逐 token 守住，這裡再抄一份只會變成第二個要同步的地方。
            hardening = {key: value for key, value, _why in permgen._HARDENING}
            self.assertIn(
                f"--property=SystemCallFilter={hardening['SystemCallFilter']}",
                props,
                name,
            )
            self.assertIn(
                f"--property={permgen.SECCOMP_FATALITY_KEY}=EPERM", props, name
            )


# ---------------------------------------------------------------------------
# 7. 明確測不到的 OS 層語意（#638／#657 的教訓：不留假綠）
# ---------------------------------------------------------------------------

class RealSeccompSurfaceTests(unittest.TestCase):
    """seccomp 的**執行期**語意在本 repo 的 CI 環境重現不了，一律具名 skip。

    需要的條件：root ＋ system-level systemd（能起 transient unit 並套 seccomp）＋
    四個真帳號 ＋ 已落檔的八份 unit ＋ 已裝的 toolchain ＋ 讀得到 kernel audit
    （`type=1326`）。CI 容器是單 UID、無 systemd，`sudo -u` 或裸跑都套不上 seccomp
    ——在那裡跑這幾條會**永遠綠**，而那個綠恰恰是 #673 誤判的成因（弱化環境下的
    結論被當成 production 的結論）。

    真實驗證在 runbook 第 5-2b 步（全矩陣 ＋ 負向 ＋ 反向對照）；具備條件的機器可設
    `PSC_TEST_REAL_HARDENING=1` 啟用。
    """

    def setUp(self) -> None:
        if not _REAL_SURFACE:
            self.skipTest(
                "需要 root ＋ systemd ＋ 已落檔的八份 unit ＋ 已裝的 toolchain ＋ "
                "kernel audit；CI 是單 UID／無 systemd，套不上 seccomp，"
                "跑出來的綠不代表任何事。真實驗證見 runbook 第 5-2b 步"
                "（PSC_TEST_REAL_HARDENING=1 可在具備條件的機器上啟用）。"
            )

    def test_pkey_alloc_is_filtered_but_non_fatal(self) -> None:
        self.skipTest(
            "「`@system-service` 是否真的擋掉 `pkey_alloc`」只能由 kernel 回答："
            "需要在真 unit 下拿掉 `SystemCallErrorNumber` 觸發 SIGSYS，再從 "
            "`dmesg` 讀 `type=1326 … syscall=330` 的 audit record。無 systemd 的 "
            "CI 既套不上過濾器，也讀不到 audit。#673 已在實機完成並記錄於該 issue。"
        )

    def test_all_executors_run_under_their_own_profile(self) -> None:
        self.skipTest(
            "executor × 剖面的全矩陣（含 `claude`／`agy` 在 jit 剖面下仍 rc=0 的**反向**"
            "對照）需要真實 UID、真實 ACL 與已落檔的 unit。runbook 第 5-2b 步 (1) 是"
            "唯一能承載該宣稱的地方；在弱化環境取得的 rc=0 不足以支撐它。"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
