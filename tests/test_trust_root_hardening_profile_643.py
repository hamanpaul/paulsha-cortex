"""#643：per-executor 加固剖面（operator 裁決＝方向 2）。

`MemoryDenyWriteExecute=yes` 與 JS runtime 天生互斥（V8 的 JIT 必須 W+X），而預設
executor（`codex`）正是 node 型——四個 executor 在真實加固面下有兩個起不來。裁決是
「node 型走一份只放寬這一項的 root-owned 模板 unit，原生執行檔型維持嚴格」。

**這整份測試真正要守的只有一條：剖面選不到寬鬆那份。** 若 job 能選到，設計就退化成
「全域移除 MDWE」，等於白做。因此下面的斷言分四層：

1. **兩份 unit 的加固表除 `MemoryDenyWriteExecute` 外逐項相同**——用集合／字典比對而
   非逐字硬編，日後往 `_HARDENING` 加一項時兩邊自動同步（漏改一邊會當場紅）。
2. **未知 executor fail-closed**——三個入口（permgen／job_runner／template 名推導）都不
   得回傳任何剖面，更不得預設落到寬鬆那份。
3. **job spec 不得攜帶剖面欄位**——寫端與讀端各掃一次，且掃的是同一支函式。
4. **polkit 對新 unit 名的正向放行與反向拒絕**——含 5-7 的 transient 五形式與名稱
   前後綴混淆，對**兩個**字幹同樣成立。

**測不到的部分明確 skip（#638 的教訓）**：MDWE 是否真的擋掉 V8、polkit 是否真的拒絕、
`CollectMode` 是否真的回收 instance——這三條都需要 root ＋ systemd ＋ polkit ＋ 已落檔
的 unit，在單 UID／寬鬆的 CI 容器裡跑出來的「綠」不代表任何事。它們由 runbook 第 5-2b
步在真實加固面下驗，這裡只留 skip 與理由，不留假綠。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paulsha_cortex.coordinator import job_runner, job_shim  # noqa: E402
from paulsha_cortex.coordinator.launcher import SubprocessLauncher  # noqa: E402
from paulsha_cortex.trust_root import permgen  # noqa: E402
from paulsha_cortex.trust_root.permgen import Principal  # noqa: E402

from test_trust_root_job_template_ab import (  # noqa: E402
    _launch_template,
    _only_spec,
    _RecordingPopen,
    _unwrap_exit_recorder,
)

_REAL_SURFACE = bool(os.environ.get("PSC_TEST_REAL_HARDENING"))


def _directives(content: str) -> dict[str, str]:
    """從 unit 內容抽出 `[Service]` 段的 `Key=Value`（忽略註解與空行）。"""

    table: dict[str, str] = {}
    section = ""
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
            continue
        if section != "[Service]" or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        # ReadWritePaths／Environment 是可重複鍵，加固表的鍵不是；只取加固表的鍵。
        if key in {k for k, _v, _w in permgen._HARDENING}:
            table[key] = value
    return table


def _section_of(content: str, key: str) -> str:
    """`key=` 這一行落在哪個 section。"""

    section = ""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
        elif stripped.startswith(f"{key}="):
            return section
    return ""


# ---------------------------------------------------------------------------
# 1. 兩份 unit 共用同一張加固表，只在一項分岔
# ---------------------------------------------------------------------------

class SharedHardeningTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scheme = permgen.DEFAULT_SCHEME
        self.strict = permgen.build_job_unit(self.scheme, profile=permgen.STRICT_PROFILE)
        self.jit = permgen.build_job_unit(self.scheme, profile=permgen.JIT_PROFILE)

    def test_both_units_carry_every_directive_in_the_shared_table(self) -> None:
        """鍵集合恆等於 `_HARDENING`——沒有任何一項在某個剖面下「消失」。"""
        expected = {key for key, _value, _why in permgen._HARDENING}
        self.assertEqual(set(_directives(self.strict.content)), expected)
        self.assertEqual(set(_directives(self.jit.content)), expected)

    def test_the_two_profiles_differ_in_exactly_the_declared_keys(self) -> None:
        """除 `PROFILE_DIVERGENCE_KEYS` 外**逐項相同**。

        用字典差集而不是逐字硬編：日後 `_HARDENING` 加一項時，只要有一邊漏了就會
        出現在 `diff` 裡而當場紅——這正是「不要複製貼上兩份完整加固段」要買的東西。
        """
        strict = _directives(self.strict.content)
        jit = _directives(self.jit.content)
        diff = {key for key in strict if strict[key] != jit[key]}
        self.assertEqual(diff, set(permgen.PROFILE_DIVERGENCE_KEYS))
        self.assertEqual(diff, {"MemoryDenyWriteExecute"})
        self.assertEqual(strict["MemoryDenyWriteExecute"], "yes")
        self.assertEqual(jit["MemoryDenyWriteExecute"], "no")

    def test_divergence_is_structurally_bounded(self) -> None:
        """剖面只能覆寫 `_HARDENING` 既有的鍵，且只能覆寫白名單內的鍵。"""
        known = {key for key, _value, _why in permgen._HARDENING}
        self.assertTrue(permgen.PROFILE_DIVERGENCE_KEYS <= known)
        for profile in permgen.HARDENING_PROFILES:
            self.assertTrue(set(profile.overrides) <= known, profile.profile_id)
            self.assertTrue(
                set(profile.overrides) <= permgen.PROFILE_DIVERGENCE_KEYS,
                profile.profile_id,
            )

    def test_a_typo_in_an_override_key_is_rejected_at_import_time(self) -> None:
        """覆寫一個不存在的鍵＝一個看起來有效、實際毫無作用的設定，必須是錯誤。"""
        bogus = permgen.HardeningProfile(
            profile_id="bogus",
            unit_suffix="-bogus",
            overrides={"MemoryDenyWriteExecut": "no"},  # 少一個 e
            rationale="typo",
        )
        original = permgen.HARDENING_PROFILES
        permgen.HARDENING_PROFILES = original + (bogus,)
        try:
            with self.assertRaises(ValueError):
                permgen._validate_hardening_profiles()
        finally:
            permgen.HARDENING_PROFILES = original
        permgen._validate_hardening_profiles()  # 還原後仍成立

    def test_strict_profile_is_the_default_everywhere(self) -> None:
        """預設就是最嚴格的那一份；要放寬必須顯式打出來。"""
        self.assertIs(permgen.DEFAULT_HARDENING_PROFILE, permgen.STRICT_PROFILE)
        self.assertEqual(permgen.STRICT_PROFILE.overrides, {})
        self.assertEqual(
            permgen.build_job_unit(self.scheme).content, self.strict.content
        )

    def test_strict_unit_name_is_unchanged(self) -> None:
        """既有部署的 `cortex-job@.service` 逐字不變（strict 後綴為空字串）。"""
        self.assertEqual(self.strict.unit_name, "cortex-job@.service")
        self.assertEqual(self.strict.install_path, "/etc/systemd/system/cortex-job@.service")
        self.assertEqual(self.jit.unit_name, "cortex-job-jit@.service")
        self.assertEqual(
            self.jit.install_path, "/etc/systemd/system/cortex-job-jit@.service"
        )

    def test_everything_but_hardening_is_identical(self) -> None:
        """身分、ExecStart、RWP、spec spool——兩份 unit 除加固外沒有第二處差異。"""
        self.assertEqual(self.strict.account, self.jit.account)
        self.assertEqual(self.strict.exec_start, self.jit.exec_start)
        self.assertEqual(self.strict.read_write_paths, self.jit.read_write_paths)
        for unit in (self.strict, self.jit):
            self.assertIn(f"User={self.scheme.resolve(Principal.BUILDER)}\n", unit.content)
            self.assertNotIn("User=root", unit.content)

    def test_the_accepted_loss_is_written_into_the_artifact(self) -> None:
        """誠實標註寫在**產物本身**，讀 unit 的人不必去翻 spec 才知道少了什麼。"""
        self.assertTrue(permgen.JIT_PROFILE.accepted_loss)
        self.assertEqual(permgen.STRICT_PROFILE.accepted_loss, ())
        self.assertIn("失去 MemoryDenyWriteExecute", self.jit.content)
        self.assertIn("JIT 型 shellcode", self.jit.content)
        self.assertIn("※ 剖面覆寫（profile=jit）", self.jit.content)
        self.assertNotIn("※ 剖面覆寫", self.strict.content)

    def test_transient_property_list_follows_the_same_profile(self) -> None:
        """A 案的 `--property=` 對照表同樣由剖面導出，不是第二份手抄。"""
        for profile in permgen.HARDENING_PROFILES:
            props = permgen.transient_unit_properties(self.scheme, profile=profile)
            effective = profile.effective()
            for key, _value, _why in permgen._HARDENING:
                self.assertIn(f"--property={key}={effective[key]}", props, key)

    def test_manager_and_monitor_units_stay_strict(self) -> None:
        """剖面只對 job 模板有意義；Manager／monitor 不受影響。"""
        for build in (permgen.build_manager_unit, permgen.build_monitor_unit):
            unit = build(self.scheme)
            self.assertEqual(unit.hardening_profile, "strict")
            self.assertEqual(_directives(unit.content)["MemoryDenyWriteExecute"], "yes")


# ---------------------------------------------------------------------------
# 2. 剖面由 executor 決定；未知 executor fail-closed
# ---------------------------------------------------------------------------

class ProfileSelectionTests(unittest.TestCase):
    def test_mapping_is_derived_from_the_existing_executor_table(self) -> None:
        """#642 的 `EXECUTOR_TOOLS` 是分類來源，不另立第二張清單。"""
        derived = {
            tool.name: permgen.executor_hardening_profile(tool.name).profile_id
            for tool in permgen.EXECUTOR_TOOLS
        }
        self.assertEqual(derived, dict(permgen.EXECUTOR_HARDENING_PROFILE))
        self.assertEqual(
            derived,
            {"codex": "jit", "copilot": "jit", "claude": "strict", "agy": "strict"},
        )
        # 反向：needs_node 為真 ⟺ jit
        for tool in permgen.EXECUTOR_TOOLS:
            expected = "jit" if tool.needs_node else "strict"
            self.assertEqual(derived[tool.name], expected, tool.name)

    def test_job_runner_table_is_pinned_to_permgen(self) -> None:
        """`job_runner` 刻意不 import permgen（派工熱路徑不拖 trust_root），兩份表
        由本契約測試釘住逐字相等——與 `DEFAULT_TEMPLATE_UNIT` 同一個既有模式。"""
        self.assertEqual(
            dict(job_runner.EXECUTOR_HARDENING_PROFILE),
            dict(permgen.EXECUTOR_HARDENING_PROFILE),
        )
        self.assertEqual(
            dict(job_runner.TEMPLATE_UNIT_SUFFIX_BY_PROFILE),
            {p.profile_id: p.unit_suffix for p in permgen.HARDENING_PROFILES},
        )
        self.assertEqual(
            set(job_runner.EXECUTOR_HARDENING_PROFILE),
            {tool.name for tool in permgen.EXECUTOR_TOOLS},
        )

    def test_template_unit_names_match_the_generated_units(self) -> None:
        """啟動器算出來的 unit 名 == permgen 產出的模板檔名。"""
        for profile in permgen.HARDENING_PROFILES:
            unit = permgen.build_job_unit(permgen.DEFAULT_SCHEME, profile=profile)
            self.assertEqual(
                job_runner.template_unit_for_profile(
                    job_runner.DEFAULT_TEMPLATE_UNIT, profile.profile_id
                ),
                unit.unit_name,
                profile.profile_id,
            )

    def test_unknown_executor_is_fail_closed_in_permgen(self) -> None:
        for name in ("mystery", "", "  ", "cg", "CODEX", "node"):
            with self.assertRaises(permgen.UnknownExecutorProfileError, msg=name):
                permgen.executor_hardening_profile(name)

    def test_unknown_executor_is_fail_closed_in_job_runner(self) -> None:
        """**不得預設落到寬鬆那份**——也不得默默給嚴格那份。一律拒。"""
        for name in ("mystery", "", "cg", "node", "CODEX"):
            with self.assertRaises(job_runner.JobRunnerError, msg=name) as ctx:
                job_runner.resolve_hardening_profile(name)
            self.assertEqual(
                ctx.exception.diagnostic.reason, "job-runner-hardening-profile-unknown"
            )

    def test_unknown_profile_id_is_fail_closed(self) -> None:
        with self.assertRaises(job_runner.JobRunnerError):
            job_runner.template_unit_for_profile(job_runner.DEFAULT_TEMPLATE_UNIT, "lax")

    def test_config_cannot_preselect_a_profile(self) -> None:
        """`PSC_JOB_TEMPLATE_UNIT` 只能給**基底**名；帶剖面後綴一律拒。

        少了這條，operator 可以把 config 直接指到 `cortex-job-jit@.service`，讓
        **所有** job（含 claude／agy）都走寬鬆剖面——那正是「退化成全域移除 MDWE」。
        """
        with self.assertRaises(job_runner.JobRunnerError) as ctx:
            job_runner.template_unit_for_profile(
                "cortex-job-jit@.service", job_runner.HARDENING_PROFILE_STRICT
            )
        self.assertEqual(
            ctx.exception.diagnostic.reason, "job-runner-template-unit-invalid"
        )

    def test_prepare_requires_an_explicit_executor(self) -> None:
        """沒有預設值：任何呼叫端都必須說出是哪個 executor。"""
        with self.assertRaises(TypeError):
            job_runner.prepare_systemd_template({}, job_id="x")  # type: ignore[call-arg]

    def test_plan_records_the_decision_for_audit(self) -> None:
        from unittest import mock

        from test_trust_root_job_template_ab import _preflight_patches, _nested, _template_env

        with tempfile.TemporaryDirectory() as d:
            spool = str(Path(d) / "job-specs")
            Path(spool).mkdir(parents=True)
            env = _template_env(spool)
            with mock.patch.dict(os.environ, env, clear=True):
                with _nested(_preflight_patches()):
                    for executor, expected, stem in (
                        ("codex", "jit", "cortex-job-jit@"),
                        ("copilot", "jit", "cortex-job-jit@"),
                        ("claude", "strict", "cortex-job@"),
                        ("agy", "strict", "cortex-job@"),
                    ):
                        plan = job_runner.prepare_systemd_template(
                            os.environ, job_id="psc-0643", executor=executor
                        )
                        self.assertEqual(plan.hardening_profile, expected, executor)
                        self.assertEqual(plan.executor, executor)
                        self.assertEqual(
                            plan.base_template_unit, job_runner.DEFAULT_TEMPLATE_UNIT
                        )
                        self.assertTrue(plan.unit.startswith(stem), plan.unit)


# ---------------------------------------------------------------------------
# 3. job 選不到剖面：spec 結構性禁止攜帶剖面欄位（寫端＋讀端）
# ---------------------------------------------------------------------------

class SpecCannotCarryTheProfileTests(unittest.TestCase):
    #: 任何一個都足以把「剖面由 executor 決定」變成「剖面由 spec 決定」。
    PROFILE_KEYS = (
        "hardening",
        "hardening_profile",
        "profile",
        "template",
        "template_unit",
        "unit_suffix",
        "MemoryDenyWriteExecute",
    )

    def test_the_forbidden_set_covers_the_profile_family(self) -> None:
        for key in self.PROFILE_KEYS:
            self.assertIn(key, job_runner.SPEC_FORBIDDEN_KEYS, key)
        # 既有的身分族一項未減。
        for key in ("user", "uid", "group", "gid", "properties", "exec_start"):
            self.assertIn(key, job_runner.SPEC_FORBIDDEN_KEYS, key)

    def test_write_and_read_side_run_the_same_scan(self) -> None:
        """兩端掃的是**同一支函式**與**同一份判準**，不是兩份各自的抄本。"""
        self.assertIs(job_shim.SPEC_FORBIDDEN_KEYS, job_runner.SPEC_FORBIDDEN_KEYS)
        self.assertIs(job_shim.forbidden_spec_keys, job_runner.forbidden_spec_keys)
        for key in self.PROFILE_KEYS:
            self.assertEqual(job_runner.forbidden_spec_keys({key: "x"}), [key], key)
        self.assertEqual(job_runner.forbidden_spec_keys({"command": ["x"]}), [])

    def test_write_side_refuses_a_spec_carrying_a_profile_key(self) -> None:
        """寫端：`build_job_spec` 的守衛掃過整份 spec。"""
        base = dict(
            job_id="psc-0643",
            instance="demo-deadbeef",
            unit="cortex-job@demo-deadbeef.service",
            command=["bash", "-c", "true"],
            working_directory="/tmp",
            log_path="/tmp/x.jsonl",
            env={"PATH": "/usr/bin"},
        )
        spec = job_runner.build_job_spec(**base)
        self.assertEqual(job_runner.forbidden_spec_keys(spec), [])
        # 守衛本身：一旦有人往 spec 塞剖面欄位，寫端立刻 fail-closed。
        for key in self.PROFILE_KEYS:
            leaked = dict(spec)
            leaked[key] = "jit"
            self.assertEqual(job_runner.forbidden_spec_keys(leaked), [key], key)

    def test_read_side_refuses_a_spec_carrying_a_profile_key(self) -> None:
        """讀端：即使 spool 被竄改（或未來有人「順手支援一下」），shim 也拒絕執行。"""
        for key in self.PROFILE_KEYS:
            with tempfile.TemporaryDirectory() as d:
                spool = Path(d) / "job-specs"
                spool.mkdir()
                spec = {
                    "spec_version": job_runner.JOB_SPEC_VERSION,
                    "instance": "demo-deadbeef",
                    "job_id": "psc-0643",
                    "unit": "cortex-job-jit@demo-deadbeef.service",
                    "command": ["/bin/true"],
                    "working_directory": d,
                    "log_path": str(Path(d) / "job.jsonl"),
                    "env": {"PATH": "/usr/bin"},
                    key: "jit",
                }
                (spool / "demo-deadbeef.json").write_text(
                    json.dumps(spec), encoding="utf-8"
                )
                with self.assertRaises(job_shim.ShimError, msg=key) as ctx:
                    job_shim.load_spec("demo-deadbeef", str(spool))
                self.assertIn(key, str(ctx.exception))

    def test_launched_spec_is_clean(self) -> None:
        """實跑一次 template 派工：落地的 spec 一個剖面欄位都沒有。"""
        with tempfile.TemporaryDirectory() as d:
            _launch_template(
                SubprocessLauncher("codex"), popen=_RecordingPopen(), workdir=d
            )
            spec = _only_spec(str(Path(d) / "job-specs"))
        self.assertEqual(job_runner.forbidden_spec_keys(spec), [])
        # 鍵集合是封閉的：spec 只有 `SPEC_REQUIRED_KEYS` 那幾個，多一個都沒有。
        self.assertEqual(set(spec), set(job_runner.SPEC_REQUIRED_KEYS))
        for key in job_runner.SPEC_FORBIDDEN_KEYS:
            self.assertNotIn(key, spec, key)


# ---------------------------------------------------------------------------
# 4. 派工端：剖面跟著 executor 走，且在 spec 之前就定案
# ---------------------------------------------------------------------------

class DispatchSelectsTheProfileTests(unittest.TestCase):
    def _unit_for(self, executor: str) -> str:
        popen = _RecordingPopen()
        with tempfile.TemporaryDirectory() as d:
            _launch_template(SubprocessLauncher(executor), popen=popen, workdir=d)
        return _unwrap_exit_recorder(popen.call["argv"])[4]

    def test_node_executors_get_the_jit_template(self) -> None:
        for executor in ("codex", "copilot"):
            self.assertTrue(
                self._unit_for(executor).startswith("cortex-job-jit@"), executor
            )

    def test_native_executors_stay_strict(self) -> None:
        for executor in ("claude", "agy"):
            unit = self._unit_for(executor)
            self.assertTrue(unit.startswith("cortex-job@"), unit)
            self.assertFalse(unit.startswith("cortex-job-jit@"), unit)

    def test_the_start_argv_still_carries_nothing_but_the_unit_name(self) -> None:
        """剖面是「換一個 root-owned unit 名」，**不是**多送一個屬性。"""
        for executor in ("codex", "claude"):
            popen = _RecordingPopen()
            with tempfile.TemporaryDirectory() as d:
                _launch_template(SubprocessLauncher(executor), popen=popen, workdir=d)
            argv = _unwrap_exit_recorder(popen.call["argv"])
            self.assertEqual(
                argv[:4],
                ["/usr/bin/systemctl", "start", "--wait", "--no-ask-password"],
            )
            self.assertEqual(len(argv), 5, argv)
            joined = " ".join(argv)
            for forbidden in ("--uid", "--gid", "--property", "--setenv", "-p "):
                self.assertNotIn(forbidden, joined, forbidden)

    def test_prompt_and_worktree_cannot_move_the_profile(self) -> None:
        """job 側可控的兩個輸入（prompt 與 worktree 內容）換掉，剖面不動。

        這是「job 選不到寬鬆剖面」在派工端的直接反證：`prepare_systemd_template()`
        在 `launch()` 的**第一行**求值（任何 mkdir／spec 寫入／Popen 之前），它唯一的
        剖面輸入是建構期就固定的 `self._executor`。
        """
        popen = _RecordingPopen()
        with tempfile.TemporaryDirectory() as d:
            evil = Path(d) / "hardening_profile"
            evil.write_text("jit\n", encoding="utf-8")
            _launch_template(
                SubprocessLauncher("claude"),
                popen=popen,
                workdir=d,
                slice_id="hardening-profile-jit",
            )
        unit = _unwrap_exit_recorder(popen.call["argv"])[4]
        self.assertTrue(unit.startswith("cortex-job@"), unit)
        self.assertFalse(unit.startswith("cortex-job-jit@"), unit)


# ---------------------------------------------------------------------------
# 5. polkit：兩個字幹都放行，混淆一律拒
# ---------------------------------------------------------------------------

class PolkitCoversBothProfilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = permgen.build_polkit_rule(
            permgen.DEFAULT_SCHEME, plan=permgen.PolkitPlan.TEMPLATE
        )

    def _decide(self, unit: str | None, verb: str | None = "start") -> str:
        return permgen.evaluate_polkit(
            self.rule,
            user=self.rule.subject_account,
            action_id=permgen.POLKIT_ACTION,
            unit=unit,
            verb=verb,
        )

    def test_one_rule_one_grant(self) -> None:
        """新增剖面沒有增加放行出口：全檔仍只有一個 `return polkit.Result.YES`。"""
        self.assertEqual(self.rule.content.count("polkit.Result.YES"), 1)
        self.assertEqual(self.rule.content.count("polkit.addRule("), 1)

    def test_both_stems_are_authorised(self) -> None:
        for profile in permgen.HARDENING_PROFILES:
            stem = permgen.job_unit_stem(
                permgen.DEFAULT_LAYOUT, Principal.BUILDER, profile
            )
            self.assertEqual(self._decide(f"{stem}@psc-0643-deadbeef.service"), "YES", stem)

    def test_pattern_enumerates_the_stems_instead_of_widening(self) -> None:
        """pattern 仍然錨定、instance 段字元類一字未改、沒有任何萬用字元。"""
        pattern = self.rule.unit_pattern
        self.assertTrue(pattern.startswith("^"))
        self.assertTrue(pattern.endswith(r"@[a-z0-9][a-z0-9._-]{0,62}\.service$"))
        stems = permgen.job_unit_stems(permgen.DEFAULT_LAYOUT, Principal.BUILDER)
        self.assertEqual(stems, ("cortex-job", "cortex-job-jit"))
        head = pattern[1 : -len(r"@[a-z0-9][a-z0-9._-]{0,62}\.service$")]
        self.assertEqual(head, "(?:" + "|".join(stems) + ")")
        for wildcard in (".*", "[^", "\\w", "+"):
            self.assertNotIn(wildcard, head, wildcard)

    def test_transient_shapes_are_still_refused(self) -> None:
        """5-7 的反向測試對新字幹同樣成立（transient 五形式）。"""
        for unit in (
            # A 案自己命名的 transient
            "cortex-job-psc-0042-deadbeef.service",
            "cortex-job-jit-psc-0042-deadbeef.service",
            # systemd 對匿名 transient 自動生成的兩種
            "run-u1234.service",
            "run-r9c0ffee.service",
            # 非實例形式
            "cortex-job.service",
        ):
            self.assertEqual(self._decide(unit), "NO", unit)
        # StartTransientUnit 的檢查不帶明細 → 缺席即拒。
        self.assertEqual(self._decide(None, None), "NO")

    def test_name_confusion_around_the_new_stem_is_refused(self) -> None:
        """名稱前後綴混淆——新增一個字幹最容易被鑽的就是這裡。"""
        for unit in (
            "cortex-job-jitx@a.service",       # 後綴多一字
            "cortex-job-ji@a.service",         # 後綴少一字
            "xcortex-job-jit@a.service",       # 前綴多一字
            "cortex-job-jit@.service",         # instance 為空
            "cortex-job-jit@-a.service",       # instance 首字非英數
            "cortex-job-jit@A.service",        # instance 首字大寫（字元類只收小寫）
            "cortex-job-jit@a.socket",         # 非 .service
            "cortex-job-jit@a.service.evil",   # 尾綴混淆
            "evil-cortex-job-jit@a.service",
            "cortex-job-jit-evil@a.service",
            "cortex-jit-job@a.service",
            "cortex-job@a.service\ncortex-job-jit@b.service",  # 換行注入
            "cortex-job-jit@" + "a" * 64 + ".service",         # instance 超長
        ):
            self.assertEqual(self._decide(unit), "NO", unit)

    def test_other_verbs_and_subjects_are_unchanged(self) -> None:
        for verb in ("reload", "mask", "set-property", "restart", "kill"):
            self.assertEqual(self._decide("cortex-job-jit@a.service", verb), "NO", verb)
        self.assertEqual(
            permgen.evaluate_polkit(
                self.rule,
                user="nobody",
                action_id=permgen.POLKIT_ACTION,
                unit="cortex-job-jit@a.service",
                verb="start",
            ),
            "NOT_HANDLED",
        )

    def test_reviewer_extension_point_still_isolated(self) -> None:
        """M2 的第二 principal 與剖面正交：builder 的規則不放行 reviewer 的任一剖面。"""
        for profile in permgen.HARDENING_PROFILES:
            stem = permgen.job_unit_stem(
                permgen.DEFAULT_LAYOUT, Principal.REVIEWER, profile
            )
            self.assertEqual(self._decide(f"{stem}@a.service"), "NO", stem)

    def test_rule_documents_both_templates(self) -> None:
        for profile in permgen.HARDENING_PROFILES:
            stem = permgen.job_unit_stem(
                permgen.DEFAULT_LAYOUT, Principal.BUILDER, profile
            )
            self.assertIn(f"{stem}@<id>.service", self.rule.content, stem)
        self.assertIn("MemoryDenyWriteExecute=no", self.rule.content)
        self.assertEqual(self.rule.residual_risks, ())


# ---------------------------------------------------------------------------
# 6. 順帶：CollectMode 放錯 section
# ---------------------------------------------------------------------------

class CollectModeSectionTests(unittest.TestCase):
    """`CollectMode` 屬 `[Unit]` 不屬 `[Service]`（產生器側已由 #645 修正）。

    放錯段時 systemd 只記一行 `Unknown key name 'CollectMode' in section 'Service',
    ignoring.` 然後忽略——unit 照樣起得來，但「失敗的 instance 自動回收」整個不生效，
    失敗的 instance 會一直留在 `systemctl list-units --failed` 上，而下一次同名派工
    會撞上 `prepare_systemd_template()` 的 busy 檢查。

    #643 把這條守衛**擴到兩份剖面**：`cortex-job-jit@.service` 是本 PR 新增的第二份
    unit，若它是複製貼上來的就可能複製到舊的放法。順帶驗「沒有任何未知鍵」。
    """

    def test_collect_mode_lives_in_the_unit_section(self) -> None:
        for profile in permgen.HARDENING_PROFILES:
            unit = permgen.build_job_unit(permgen.DEFAULT_SCHEME, profile=profile)
            # 以「真正的指令行」判段，不是字串 split——產生出來的**註解**裡就含有
            # `[Unit]`／`[Service]` 字樣（在解釋這個坑），naive split 會切錯地方。
            directives = [
                line.strip()
                for line in unit.content.splitlines()
                if line.strip().startswith("CollectMode=")
            ]
            self.assertEqual(len(directives), 1, profile.profile_id)
            self.assertEqual(
                _section_of(unit.content, "CollectMode"), "[Unit]", profile.profile_id
            )

    def test_systemd_analyze_accepts_the_generated_units(self) -> None:
        """`systemd-analyze verify` 是唯一能證明「沒有 Unknown key name」的東西。

        它不需要 root、也不需要真的起 unit，但需要機器上有 systemd；沒有就 skip
        （#638 的教訓：在測不到的環境下讓它「綠」比紅更糟）。
        """
        analyze = shutil.which("systemd-analyze")
        if analyze is None:
            self.skipTest("本機無 systemd-analyze——`Unknown key name` 只有 systemd 判得出來")
        for profile in permgen.HARDENING_PROFILES:
            unit = permgen.build_job_unit(permgen.DEFAULT_SCHEME, profile=profile)
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / unit.unit_name
                path.write_text(unit.content, encoding="utf-8")
                proc = subprocess.run(
                    [analyze, "verify", str(path)],
                    capture_output=True, text=True, timeout=60,
                )
            self.assertNotIn("Unknown key name", proc.stderr, unit.unit_name)


# ---------------------------------------------------------------------------
# 7. 明確測不到的語意（#638 的教訓：不留假綠）
# ---------------------------------------------------------------------------

class RealHardeningSurfaceTests(unittest.TestCase):
    """下面三條的語意**在單 UID／寬鬆環境下測不出來**，一律 skip 並說明理由。

    #643 本身就是這條教訓的實例：四個 executor 在寬鬆環境下 `--version` 全部 rc=0、
    版本全部相符，唯一能看見問題的是 `systemd-run` 帶上真實加固面那一條。因此這裡
    **不**用 `sudo -u`／裸跑當替身——那只會產生一個永遠綠、卻什麼都沒驗到的測試。

    要跑真的：runbook 第 5-2b 步（root ＋ systemd ＋ 已落檔的兩份 unit），或在具備
    root 的機器上設 `PSC_TEST_REAL_HARDENING=1`。
    """

    def setUp(self) -> None:
        if not _REAL_SURFACE:
            self.skipTest(
                "需要 root ＋ systemd ＋ 已落檔的兩份 job unit ＋ 已裝的 toolchain；"
                "CI 容器是單 UID／無 systemd，跑出來的綠不代表任何事。"
                "真實驗證見 runbook 第 5-2b 步（PSC_TEST_REAL_HARDENING=1 可在具備"
                "條件的機器上啟用）。"
            )

    def test_mdwe_blocks_node_and_the_jit_profile_does_not(self) -> None:
        """負向對照是重點：strict 剖面下 node **必須失敗**，jit 下必須成功。

        只驗 jit 成功會整個溜過去——那正是 #642 差點漏掉這條的原因。
        """
        node = shutil.which("node")
        if node is None:
            self.skipTest("本機無 node")
        base = [
            "systemd-run", "--pipe", "--wait", "--collect", "--quiet",
            "--property=NoNewPrivileges=yes",
        ]
        script = "console.log('node OK ' + process.version)"
        strict = subprocess.run(
            base + ["--property=MemoryDenyWriteExecute=yes", node, "-e", script],
            capture_output=True, text=True, timeout=120,
        )
        lax = subprocess.run(
            base + ["--property=MemoryDenyWriteExecute=no", node, "-e", script],
            capture_output=True, text=True, timeout=120,
        )
        self.assertNotIn("node OK", strict.stdout, "strict 剖面下 V8 不該起得來")
        self.assertIn("node OK", lax.stdout, "jit 剖面下 node 必須正常")

    def test_polkit_refuses_the_jit_template_from_a_foreign_account(self) -> None:
        self.skipTest(
            "polkit 決策無法在本機純函式化驗證（規則跑在 polkitd 內，且 subject 必須"
            "是真的 cortex-manager）。`evaluate_polkit` 是產生邏輯的鏡像，不是 polkitd。"
            "真實反向測試見 runbook 第 5-7 步。"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
