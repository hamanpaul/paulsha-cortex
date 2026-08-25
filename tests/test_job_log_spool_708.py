"""issue #708：三個降權 principal 的 job log spool 由**同一條規則**導出。

## 本票要釘住的性質

`coordinator/job_shim.py` 的 `_take_over_stdio()` 在**接管 stdio 之前**就
`os.open(log_path)`。因此凡經模板 unit 派出的 job，只要那條路徑寫不進去，它連一行
log 都寫不出來就死——**失敗發生在它能記錄失敗之前**，Manager 端只看得到
`78/CONFIG`。0819 實機：define 首次收斂、builder job 第一次由 daemon 經正規路徑
派出來，當場撞上（`/var/lib/cortex/coordinator/logs/workflow/…jsonl:
[Errno 13] Permission denied`）。

`#686` 已經為 planner 做過完全相同的事，但只做了那一格——本檔因此驗的不是「builder
那一格有沒有補上」，而是**「只修一格」在結構上做不到**：

1. `registry.JOB_LOG_SPOOLS` 覆蓋 `DOWNGRADED_JOB_PRINCIPALS` 的每一格，缺一格
   `registry` **載不起來**；
2. 每一格都掛在該 principal **既有**的輸出通道底下，且路徑嚴格落在通道之內，否則
   `permgen` **載不起來**（那正是「可寫面逐字不變、零部署動作」的充要條件）；
3. 三份成對契約（`config/paths.py`／`trust_root/registry.py`／
   `coordinator/job_runner.py` 的角色表）逐列相等。

OS 層那一半（ACL mask 的 effective 位、mount 層 EROFS）在單 UID 的環境重現不了，
一律具名 skip ＋ 指向 runbook 的實機步驟，**不靜默通過**（#638／#657／#673 立下的
規矩）。
"""
from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest
from _home_paths import BUILDER_HOME

import paulsha_cortex.coordinator.gate_runner as gate_runner
import paulsha_cortex.coordinator.job_runner as job_runner
import paulsha_cortex.coordinator.job_shim as job_shim
import paulsha_cortex.coordinator.job_workspace as job_workspace
import paulsha_cortex.coordinator.spool_slot as spool_slot
import paulsha_cortex.coordinator.terminal_contract as terminal_contract
from paulsha_cortex.config import paths as config_paths
from paulsha_cortex.trust_root import permgen, registry
from paulsha_cortex.trust_root.registry import Principal


LAYOUT = permgen.DEFAULT_LAYOUT
SCHEME = permgen.DEFAULT_SCHEME


# ---------------------------------------------------------------------------
# 一、那條規則本身（import 當下強制，不是一條可以被 `-k` 跳過的測試）
# ---------------------------------------------------------------------------

class TheRuleCoversEveryDowngradedPrincipalTests(unittest.TestCase):
    def test_every_downgraded_principal_has_exactly_one_log_spool(self) -> None:
        declared = [spool.principal for spool in registry.JOB_LOG_SPOOLS]
        self.assertEqual(
            sorted(p.value for p in declared),
            sorted(p.value for p in registry.DOWNGRADED_JOB_PRINCIPALS),
        )
        self.assertEqual(len(declared), len(set(declared)))

    def test_dropping_a_principal_is_refused_at_import_time(self) -> None:
        """**「只修一格」在結構上做不到**——這是本票與 #686 的唯一實質差別。

        #698／PR #703 的 `_assert_shape_follows_enforcement_rule()` 是同一個形狀的
        先例：檢查放在展開點上，漏改的症狀是模組載不起來，而不是三個月後實機上的
        一次紅字。
        """

        only_builder = tuple(
            s for s in registry.JOB_LOG_SPOOLS if s.principal is Principal.BUILDER
        )
        with mock.patch.object(registry, "JOB_LOG_SPOOLS", only_builder):
            with self.assertRaises(ValueError) as ctx:
                registry._assert_every_downgraded_principal_has_a_job_log_spool()
        self.assertIn("reviewer", str(ctx.exception))
        self.assertIn("gate", str(ctx.exception))

    def test_a_spool_that_is_not_under_an_existing_channel_is_refused(self) -> None:
        """掛錯地方 ⇒ `_minimize()` 吃不掉 ⇒ 模板 unit 的 RWP 會多一行 ⇒ 拒絕載入。"""

        rogue = tuple(
            s if s.principal is not Principal.BUILDER
            else registry.JobLogSpool(
                principal=s.principal,
                writer=s.writer,
                asset_id=s.asset_id,
                # coordinator 樹根：builder 對它零寫入權，掛上去等於新開一條通道。
                channel_asset_id="coordinator-root-tree",
                dirname=s.dirname,
                note=s.note,
            )
            for s in registry.JOB_LOG_SPOOLS
        )
        with mock.patch.object(registry, "JOB_LOG_SPOOLS", rogue):
            with self.assertRaises(ValueError) as ctx:
                permgen._assert_job_log_spools_hang_off_existing_channels()
        self.assertIn("coordinator-root-tree", str(ctx.exception))

    def test_a_dirname_that_can_escape_the_channel_is_refused(self) -> None:
        """`dirname` 會被逐字接進一條會被 `chown`／`setfacl` 的絕對路徑。"""

        for bad in ("../../gate-logs", "", "gate logs", "/abs"):
            escaping = tuple(
                s if s.principal is not Principal.GATE
                else registry.JobLogSpool(
                    principal=s.principal,
                    writer=s.writer,
                    asset_id=s.asset_id,
                    channel_asset_id=s.channel_asset_id,
                    dirname=bad,
                    note=s.note,
                )
                for s in registry.JOB_LOG_SPOOLS
            )
            with mock.patch.object(registry, "JOB_LOG_SPOOLS", escaping):
                with self.assertRaises(ValueError, msg=bad):
                    registry._assert_every_downgraded_principal_has_a_job_log_spool()


# ---------------------------------------------------------------------------
# 二、成對契約：三處字面量必須逐列相等
# ---------------------------------------------------------------------------

class PairedContractTests(unittest.TestCase):
    def test_paths_contract_matches_the_registry_table(self) -> None:
        """`config/paths.py` 刻意不 import `trust_root`（path 契約對治理平面零依賴），
        因此兩邊各有一份字面量——這一條就是把它們釘在一起的那顆釘子。"""

        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"PSC_AGENTS_ROOT": root}, clear=False):
                for spool in registry.JOB_LOG_SPOOLS:
                    resolved = config_paths.job_log_spool_root(spool.principal.value)
                    self.assertEqual(resolved.name, spool.dirname, spool.asset_id)

    def test_layout_path_matches_the_paths_contract(self) -> None:
        for spool in registry.JOB_LOG_SPOOLS:
            layout_path = LAYOUT.job_log_spool_root(spool.principal)
            self.assertTrue(layout_path.startswith("/"), layout_path)
            self.assertTrue(layout_path.endswith(f"/{spool.dirname}"), layout_path)
            self.assertEqual(layout_path, LAYOUT.asset_paths()[spool.asset_id])

    def test_role_table_maps_every_role_to_a_declared_principal(self) -> None:
        """`job_runner` 的角色字幹是 `review`，principal 是 `reviewer`——**恰好不同**，
        而那正是不能靠字串推導的理由。"""

        declared = {s.principal.value for s in registry.JOB_LOG_SPOOLS}
        mapped = {cfg.log_spool_principal for cfg in job_runner.JOB_ROLE_CONFIG.values()}
        self.assertEqual(mapped, declared)
        self.assertEqual(
            job_runner.JOB_ROLE_CONFIG[job_runner.JOB_ROLE_REVIEW].log_spool_principal,
            "reviewer",
        )

    def test_unknown_principal_is_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            config_paths.job_log_spool_root("planner")
        with self.assertRaises(ValueError):
            config_paths.job_log_spool_root("../escape")
        with self.assertRaises(KeyError):
            registry.job_log_spool_for(Principal.MANAGER)


# ---------------------------------------------------------------------------
# 三、可寫面逐字不變（本票的整個成本論證）
# ---------------------------------------------------------------------------

class WritableSurfaceIsUnchangedTests(unittest.TestCase):
    def _rwp(self, principal: Principal) -> tuple[str, ...]:
        return permgen.build_job_unit(SCHEME, LAYOUT, principal).read_write_paths

    def test_no_log_spool_appears_in_any_job_unit_read_write_paths(self) -> None:
        """`_minimize()` 必須把三格全部吃掉——這就是「零部署動作」的機械來源。

        實機證據（issue #708）逐字列出了 builder unit 的五條 `ReadWritePaths=`；
        本票之後那五條**一個位元組都沒有變**。
        """

        paths_by_asset = LAYOUT.asset_paths()
        for spool in registry.JOB_LOG_SPOOLS:
            rwp = self._rwp(spool.principal)
            self.assertIn(paths_by_asset[spool.asset_id] + "/%i", rwp, spool.asset_id)
            self.assertNotIn(paths_by_asset[spool.channel_asset_id], rwp, spool.asset_id)

    def test_builder_unit_read_write_paths_match_the_field_evidence(self) -> None:
        self.assertEqual(
            self._rwp(Principal.BUILDER),
            (
                "/var/lib/cortex/worktree/%i",
                "/var/lib/cortex/coordinator/commit-spool/%i",
                "/var/lib/cortex/monitor/event-spool/%i",
                "/var/lib/cortex/coordinator/commit-spool/build-logs/%i",
                "/var/lib/cortex/runtime/codex-home/builder/%i",
                "/var/lib/cortex/runtime/job-cache/builder/%i",
            ),
        )

    def test_each_spool_gets_a_write_only_acl_for_its_own_account(self) -> None:
        plan = permgen.generate_plan(SCHEME)
        entries = {e.asset_id: e for e in plan.entries}
        for spool in registry.JOB_LOG_SPOOLS:
            entry = entries[spool.asset_id]
            account = SCHEME.resolve(spool.writer)
            self.assertEqual(entry.owner, SCHEME.durable_state_owner, spool.asset_id)
            granted = {acl.account: acl.perms for acl in entry.acls}
            # `wx` 無 `r`：寫得進自己那一格、讀不到別人的 log。
            self.assertEqual(granted.get(account), "wx", spool.asset_id)


# ---------------------------------------------------------------------------
# 四、canonical Manager-readable log surface：不跨 mount 建 hard link
# ---------------------------------------------------------------------------

class CanonicalLogSurfaceTests(unittest.TestCase):
    def test_manager_reads_the_preseeded_job_surface_without_a_cross_mount_link(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manager_log = Path(root) / "logs" / "wf-1.jsonl"
            with mock.patch.dict(os.environ, {"PSC_AGENTS_ROOT": root}, clear=False):
                # The exact Manager failure was os.link(...)->EXDEV in the
                # service namespace.  The repair must not call it at all.
                with mock.patch.object(
                    os, "link", side_effect=OSError(18, "Invalid cross-device link")
                ) as link:
                    job_log = job_workspace.prepare_job_log_spool(
                        principal_id="builder",
                        spool_key="wf-1",
                        manager_log_path=manager_log,
                    )
                link.assert_not_called()
            self.assertNotEqual(job_log.parent, manager_log.parent)
            self.assertTrue(job_log.is_file())
            self.assertFalse(manager_log.exists())
            # The preseeded file remains Manager-owned/readable while the job
            # appends to the same canonical surface.
            with open(job_log, "a", encoding="utf-8") as handle:
                handle.write("from-the-job\n")
            self.assertEqual(
                job_log.read_text(encoding="utf-8"), "from-the-job\n"
            )

    def test_relaunching_the_same_key_starts_from_a_clean_inode(self) -> None:
        """retry 用同一個 slice_id；上一輪的殘留必須消失，而且是**整格重建**。"""

        with tempfile.TemporaryDirectory() as root:
            manager_log = Path(root) / "logs" / "wf-1.jsonl"
            with mock.patch.dict(os.environ, {"PSC_AGENTS_ROOT": root}, clear=False):
                first = job_workspace.prepare_job_log_spool(
                    principal_id="builder",
                    spool_key="wf-1",
                    manager_log_path=manager_log,
                )
                first.write_text("STALE FROM LAST ROUND\n", encoding="utf-8")
                second = job_workspace.prepare_job_log_spool(
                    principal_id="builder",
                    spool_key="wf-1",
                    manager_log_path=manager_log,
                )
            self.assertEqual(first, second)
            self.assertEqual(second.read_text(encoding="utf-8"), "")
            self.assertEqual(second.read_text(encoding="utf-8"), "")

    def test_explicit_manager_control_anchor_stays_separate_from_hashed_template_slot(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            slice_id = "wf-" + ("x" * 96)
            control_log = Path(root) / "runtime" / "dispatch" / f"{slice_id}.jsonl"
            spool_key = job_runner.template_instance_id(slice_id)
            with mock.patch.dict(os.environ, {"PSC_AGENTS_ROOT": root}, clear=False):
                job_log = job_workspace.prepare_job_log_spool(
                    principal_id="builder",
                    spool_key=spool_key,
                    manager_log_path=control_log,
                )
            self.assertEqual(job_log.parent.name, spool_key)
            self.assertNotEqual(control_log.stem, spool_key)
            self.assertNotEqual(job_workspace.manager_control_log_path(job_log), control_log)
            self.assertEqual(job_workspace.manager_control_log_path(control_log), control_log)
            self.assertEqual(
                str(job_workspace.manager_control_log_path(control_log).with_suffix(".exit")),
                str(control_log.with_suffix(".exit")),
            )
            self.assertEqual(
                terminal_contract.gate_ledger_path(control_log),
                control_log.with_name(f"{slice_id}.gates.json"),
            )


def test_real_systemd_namespace_keeps_log_surface_off_manager_controls() -> None:
    """Run the two-RWP mount boundary instead of mocking ``os.link``.

    The deployed Manager unit gives ``coordinator`` and ``runtime/dispatch``
    separate writable bind mounts.  A real systemd helper proves that an
    attempted bridge returns ``EXDEV`` and that the canonical source remains
    readable; product code must therefore consume that source directly.
    """

    if os.geteuid() != 0:
        pytest.skip("real service-namespace probe requires root")
    if not Path("/run/systemd/system").is_dir() or shutil.which("systemd-run") is None:
        pytest.skip("systemd system manager is unavailable")
    try:
        manager = pwd.getpwnam("cortex-manager")
    except KeyError:
        pytest.skip("deployed cortex-manager account is unavailable")

    base = Path(tempfile.mkdtemp(prefix="cortex-log-namespace-", dir="/var/lib/cortex/run/cortex"))
    spool = base / "coordinator" / "commit-spool" / "build-logs" / "probe"
    controls = base / "runtime" / "dispatch"
    spool.mkdir(parents=True)
    controls.mkdir(parents=True)
    for path in (base, spool, controls):
        os.chown(path, manager.pw_uid, manager.pw_gid)
        os.chmod(path, 0o700)
    script = (
        "import errno, os, pathlib; "
        "spool=pathlib.Path(os.environ['PSC_TEST_SPOOL']); "
        "controls=pathlib.Path(os.environ['PSC_TEST_CONTROLS']); "
        "source=spool/'job.jsonl'; target=controls/'job.jsonl'; "
        "source.write_bytes(b'canonical\\n'); "
        "try: os.link(source, target)\n"
        "except OSError as exc:\n"
        "    assert exc.errno == errno.EXDEV, exc\n"
        "    assert source.read_bytes() == b'canonical\\n'\n"
        "else: raise AssertionError('cross-RWP hard link unexpectedly succeeded')"
    )
    unit = f"cortex-log-surface-test-{os.getpid()}"
    command = [
        "systemd-run",
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        f"--unit={unit}",
        f"--property=User={manager.pw_name}",
        f"--property=Group={manager.pw_name}",
        "--property=ProtectSystem=strict",
        "--property=PrivateTmp=yes",
        f"--property=ReadWritePaths={spool}",
        f"--property=ReadWritePaths={controls}",
        f"--setenv=PSC_TEST_SPOOL={spool}",
        f"--setenv=PSC_TEST_CONTROLS={controls}",
        "--",
        sys.executable,
        "-c",
        script,
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        assert completed.returncode == 0, (
            f"systemd namespace probe failed: stdout={completed.stdout!r} "
            f"stderr={completed.stderr!r}"
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)

    def test_log_file_mode_is_0620_not_0600(self) -> None:
        """`0600` 會把繼承來的 `user:<job>:-wx` 壓成 `#effective:---`（#638 缺陷 1）。"""

        self.assertEqual(spool_slot.JOB_LOG_FILE_MODE, 0o620)
        with tempfile.TemporaryDirectory() as root:
            slot = Path(root) / "slot"
            log = spool_slot.prepare_job_log(slot, slot / "job.jsonl")
            self.assertEqual(log.stat().st_mode & 0o777, 0o620)

    def test_generated_builder_unit_keeps_log_slot_and_manager_control_root_separate(self) -> None:
        unit = permgen.build_job_unit(
            permgen.DEFAULT_SCHEME, permgen.DEFAULT_LAYOUT, Principal.BUILDER
        )
        log_slot = (
            f"{permgen.DEFAULT_LAYOUT.job_log_spool_root(Principal.BUILDER)}/%i"
        )
        self.assertIn(log_slot, unit.read_write_paths)
        self.assertNotIn(
            permgen.DEFAULT_LAYOUT.dispatch_log_root, unit.read_write_paths
        )
        self.assertIn(f"ReadWritePaths={log_slot}", unit.content)

    def test_reviewer_and_builder_land_in_different_channels(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"PSC_AGENTS_ROOT": root}, clear=False):
                builder = job_workspace.job_log_spool_dir(
                    principal_id="builder", spool_key="k"
                )
                reviewer = job_workspace.job_log_spool_dir(
                    principal_id="reviewer", spool_key="k"
                )
            self.assertIn(config_paths.COMMIT_SPOOL_DIRNAME, str(builder))
            self.assertIn(config_paths.REVIEW_VERDICT_SPOOL_DIRNAME, str(reviewer))

    def test_unsafe_spool_key_is_rejected_before_any_path_is_composed(self) -> None:
        for bad in ("../escape", "", "a/b"):
            with self.assertRaises(job_workspace.WorkspaceError):
                job_workspace.job_log_spool_dir(principal_id="builder", spool_key=bad)


# ---------------------------------------------------------------------------
# 五、gate：log 落在自己那一格，且由 Manager 預建（Manager 讀得到）
# ---------------------------------------------------------------------------

class GateJobLogTests(unittest.TestCase):
    def test_gate_log_leaves_the_ledger_slot(self) -> None:
        """舊路徑是 `<gate-ledger-spool>/<key>/gate.log`——gate **寫得進去**，所以它
        不像 builder 那樣當場死；但那個檔由 job 自己建（`UMask=0077` ⇒
        `0600 cortex-gate`），Manager 讀不到 ⇒ 失敗的逐字原因只存在於一個看不見的
        檔裡。"""

        ledger = gate_runner.gate_spool_ledger_path(
            spool_key="k1", coordinator_root="/srv/c"
        )
        log = gate_runner.gate_job_log_path(spool_key="k1", coordinator_root="/srv/c")
        self.assertNotEqual(log.parent, ledger.parent)
        self.assertEqual(
            str(log), "/srv/c/gate-ledger-spool/gate-logs/k1/gate.log"
        )

    def test_gate_log_is_pre_created_by_the_manager(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            log = gate_runner.prepare_gate_job_log(spool_key="k1", coordinator_root=root)
            self.assertTrue(log.is_file())
            self.assertEqual(log.stat().st_mode & 0o777, 0o620)


# ---------------------------------------------------------------------------
# 六、第 3 項：「失敗要能被記錄」
# ---------------------------------------------------------------------------

class ShimFailureRecordTests(unittest.TestCase):
    def test_shim_records_a_machine_readable_failure_next_to_the_log(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            log_path = str(Path(root) / "job.jsonl")
            self.assertTrue(job_shim.write_shim_error(log_path, "inst-1", "boom"))
            record = json.loads(
                (Path(root) / job_shim.SHIM_ERROR_FILENAME).read_text(encoding="utf-8")
            )
        self.assertEqual(record["schema"], job_shim.SHIM_ERROR_SCHEMA)
        self.assertEqual(record["instance"], "inst-1")
        self.assertEqual(record["error"], "boom")

    def test_the_record_is_world_readable_so_the_manager_can_read_it(self) -> None:
        """job 建的檔帶降權 unit 的 `UMask=0077` ⇒ 不顯式 `fchmod` 就是 `0600 <job>`，
        Manager 是目錄 owner 但那不給檔案內容的讀取權（#638 缺陷 2）。"""

        with tempfile.TemporaryDirectory() as root:
            previous = os.umask(0o077)
            try:
                job_shim.write_shim_error(str(Path(root) / "job.jsonl"), "i", "boom")
            finally:
                os.umask(previous)
            mode = (Path(root) / job_shim.SHIM_ERROR_FILENAME).stat().st_mode & 0o777
        self.assertEqual(mode, 0o644)

    def test_only_the_first_failure_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            log_path = str(Path(root) / "job.jsonl")
            self.assertTrue(job_shim.write_shim_error(log_path, "i", "root cause"))
            self.assertFalse(job_shim.write_shim_error(log_path, "i", "後續雜訊"))
            record = json.loads(
                (Path(root) / job_shim.SHIM_ERROR_FILENAME).read_text(encoding="utf-8")
            )
        self.assertEqual(record["error"], "root cause")

    def test_writing_the_record_never_raises(self) -> None:
        """診斷面失敗不得變成新的失敗來源，也不得改變 shim 的退出碼。"""

        self.assertFalse(job_shim.write_shim_error("/proc/nonexistent/x.jsonl", "i", "e"))

    def test_manager_surfaces_the_record_in_the_start_failure(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            job_log = Path(root) / "job.jsonl"
            job_shim.write_shim_error(str(job_log), "inst-1", "開不了 job log")
            with self.assertRaises(job_runner.JobRunnerError) as ctx:
                job_runner.confirm_template_instance_started(
                    process=_ExitedProc(),
                    sentinel=str(Path(root) / "missing.exit"),
                    unit="cortex-job@inst-1.service",
                    account="cortex-builder",
                    job_log_path=str(job_log),
                    timeout_ms=1,
                    monotonic=lambda: 0.0,
                    sleep=lambda _s: None,
                    manager_authored_sentinel=True,
                )
        self.assertIn("開不了 job log", ctx.exception.diagnostic.detail)

    def test_a_forged_or_absent_record_degrades_to_silence(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(job_runner.read_shim_error(str(Path(root) / "x.jsonl")), "")
            (Path(root) / job_shim.SHIM_ERROR_FILENAME).write_text(
                json.dumps({"schema": "other/9", "error": "x"}), encoding="utf-8"
            )
            self.assertEqual(job_runner.read_shim_error(str(Path(root) / "x.jsonl")), "")

    def test_the_shim_writes_the_record_on_a_log_open_failure(self) -> None:
        """本票的原症狀：spec 讀得到、log 開不起來。這一族現在留得下紀錄。"""

        with tempfile.TemporaryDirectory() as root:
            spool = Path(root) / "job-specs"
            spool.mkdir()
            log_dir = Path(root) / "logs"
            log_dir.mkdir()
            log_path = log_dir / "wf-1.jsonl"
            spec = {
                "spec_version": job_runner.JOB_SPEC_VERSION,
                "job_id": "wf-1",
                "instance": "wf-1",
                "unit": "cortex-job@wf-1.service",
                "command": ["/bin/true"],
                "working_directory": root,
                "log_path": str(log_path),
                "env": {"HOME": BUILDER_HOME, "PATH": "/usr/bin"},
            }
            (spool / "wf-1.json").write_text(json.dumps(spec), encoding="utf-8")
            with mock.patch.object(
                job_shim, "_take_over_stdio", side_effect=job_shim.ShimError(
                    f"開不了 job log {log_path}: [Errno 13] Permission denied"
                )
            ):
                rc = job_shim.main(
                    ["wf-1"], {job_runner.JOB_SPEC_SPOOL_ENV: str(spool)}
                )
            self.assertEqual(rc, job_shim.EXIT_SPEC_ERROR)
            record = json.loads(
                (log_dir / job_shim.SHIM_ERROR_FILENAME).read_text(encoding="utf-8")
            )
        self.assertIn("Permission denied", record["error"])


class _ExitedProc:
    """client 已結束（`systemctl start --wait` 以非零收場）的最小同形物。"""

    pid = 4242

    def poll(self) -> int:
        return 1


# ---------------------------------------------------------------------------
# 七、反向不變式的探針（產生器層；實機那一半在 runbook）
# ---------------------------------------------------------------------------

class ReverseInvariantProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lines = permgen.build_job_log_probe(SCHEME)
        self.text = "\n".join(self.lines)

    def test_probe_never_injects_environment(self) -> None:
        """D13：不得自組 `--property=`、不得自帶 `--setenv=PATH=`。

        本 repo 兩個方向的事故各兩次（#638／#657 假綠；#673 body 與 repro 假紅），
        #679 是第五個（複本比 production **多**）。判準與 #679 共用同一支
        `path_probe_env_injections()`——不另寫一份會漂移的。
        """

        self.assertEqual(permgen.path_probe_env_injections(self.lines), ())

    def test_probe_calls_the_shared_helper_and_never_redefines_it(self) -> None:
        self.assertNotIn(f"{permgen.PATH_PROBE_HELPER}() {{", self.text)
        self.assertIn(f"declare -F {permgen.PATH_PROBE_HELPER}", self.text)

    def test_probe_covers_every_downgraded_principal_both_directions(self) -> None:
        paths_by_asset = LAYOUT.asset_paths()
        for principal in permgen.downgraded_job_principals(SCHEME):
            spool = registry.job_log_spool_for(principal)
            stem = permgen.job_unit_stem(LAYOUT, principal)
            self.assertIn(f"{permgen.PATH_PROBE_HELPER} {stem} ", self.text)
            self.assertIn(paths_by_asset[spool.asset_id], self.text)
        # 反向那一半：Manager 的 dispatch log 目錄必須被當成**應該失敗**的目標。
        self.assertIn(f"{LAYOUT.coordinator_root}/logs/workflow/", self.text)

    def test_probe_builds_the_slot_with_the_product_code(self) -> None:
        """#645：手工組前置物會剛好把 bug 繞過去。本探針因此呼叫 `prepare_job_log()`
        本人，而不是自己 `mkdir` ＋ `touch`。"""

        self.assertIn("spool_slot as s; s.prepare_job_log(", self.text)
        self.assertNotIn("install -d", self.text)
        self.assertNotIn("touch ", self.text)

    def test_cli_emits_the_same_lines(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "paulsha_cortex.trust_root", "job-log-probe"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.rstrip("\n").splitlines(), self.lines)


# ---------------------------------------------------------------------------
# 八、OS 層語意：需要第二個 UID ＋ 真實 systemd ⇒ 具名 skip，不靜默通過
# ---------------------------------------------------------------------------

@pytest.mark.skip(
    reason=(
        "#638／#657／#673 立下的規矩：涉及 OS 層語意、這個環境重現不了的，明確 skip "
        "並說明理由，不得靜默通過。本條要驗的是「三個降權 principal 在**零額外 env**、"
        "真實模板 unit 的加固面下寫得出自己那一格 job log，而 Manager 讀得回來」——"
        "需要四樣這個環境都沒有的東西：(1) 第二個 UID（cortex-builder／"
        "cortex-reviewer-planner／cortex-gate）；(2) 真的 systemd ＋ polkit 授權，才起"
        "得了模板實例；(3) 支援 POSIX ACL 的檔案系統——mask 把繼承來的 `-wx` 壓成 "
        "`#effective:---` 正是 #638 缺陷 1，單 UID 下 owner 位會讓它假綠；(4) permgen "
        "已套用過的一棵真實 durable state 樹。\n"
        "實機步驟：`python3 -m paulsha_cortex.trust_root job-log-probe` 產生的探針，"
        "貼在 runbook 第 4e-2d 步（加固面複本一律由 psc_run_under／"
        "unit_replica_properties() 全量導出，**不得自組 --property=**，見 design D13）。"
    )
)
def test_every_downgraded_job_writes_its_log_with_zero_extra_env() -> None:  # pragma: no cover
    raise AssertionError("需要第二個 UID ＋ 真實 systemd 加固面 ＋ POSIX ACL；見 skip 理由")


@pytest.mark.skip(
    reason=(
        "同上一條的**反向**那一半：降權 job 對 Manager 的 dispatch log 目錄"
        "（`<coordinator_root>/logs/workflow/`）必須仍然寫不進去——那一層住著 gate "
        "ledger（`<slice>.gates.json`）與 exit sentinel（`<slice>.exit`），#604 的整個"
        "保證就是它們由 Manager 寫、採信端以 `foreign_evidence_author()` 檢查擁有者。"
        "少了這一半，「掛在既有通道底下」只是一句宣稱：真正要排除的失敗是「順手把 log "
        "目錄加進 ReadWritePaths= 讓它過」。\n"
        "單 UID 下量不到（mount namespace 與 DAC 兩層都需要真實降權身分）；實機步驟同"
        "上（runbook 第 4e-2d 步的第 4 項，期望 `Read-only file system` 或 "
        "`Permission denied`）。"
    )
)
def test_the_manager_dispatch_log_directory_stays_unwritable() -> None:  # pragma: no cover
    raise AssertionError("需要第二個 UID ＋ 真實 mount namespace；見 skip 理由")
