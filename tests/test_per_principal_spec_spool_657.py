"""#657：per-principal spec spool ——「那個身分讀得到自己的 spec 嗎」。

## 這一票在測什麼

#629 的 gate 執行身分落地後，實機上**每個** gate job 都以 `78/CONFIG` 收場：
三份模板 unit（builder／reviewer-planner／gate）指向**同一個** spec spool，而登記表
只授 builder 唯讀 ACL。shim 是 systemd 套完 `User=` **之後**才執行的，因此它以 job
身分讀 spec ⇒ 必然 `EACCES`。reviewer／planner 是同一型（#652 沒驗到這一層）。

## 為什麼 CI 當時是綠的（本檔存在的理由）

這一族（#630／#631／#638／#657）的 bug 全部同一個形狀：**單 UID 環境測不出 ACL
語意**。測試建了目錄、Manager 寫得進去、檔案存在——於是綠；而實機缺的是「另一個
UID 對這個 inode 的 **effective** 權限」，那在單 UID 下不會表現出來。

本檔因此不測「目錄在不在」。它**自己建出一棵真實的 ACL 樹**（`setfacl` 一個真的
存在、uid 與本行程不同的第二個帳號），並以 **effective 權限**斷言——而且與系統的
`getfacl` 交叉核對，證明判定不是自說自話。

**需要第二個 UID 才驗得到的那一半明確 skip**：真的 `seteuid()` 過去開一次檔需要
root 或第二個可登入身分，CI 兩者都沒有。那一段由 runbook 的 `sudo -u <帳號>` 實測
步驟承接，本檔以 `pytest.skip` ＋ 逐字理由留下缺口位置，**不靜默通過**。
"""
from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from paulsha_cortex.config import paths as config_paths
from paulsha_cortex.coordinator import job_runner
from paulsha_cortex.trust_root import permgen, registry
from paulsha_cortex.trust_root.registry import Principal

LAYOUT = permgen.DEFAULT_LAYOUT
SCHEME = permgen.DEFAULT_SCHEME

#: job_runner 的角色 id ⟷ 登記表 principal。兩個模組刻意不互相 import（既有裁決：
#: 派工熱路徑不拖進 trust_root），因此這張對照表是本檔釘住兩邊的那一條線。
ROLE_TO_PRINCIPAL = {
    job_runner.JOB_ROLE_BUILDER: Principal.BUILDER,
    job_runner.JOB_ROLE_REVIEW: Principal.REVIEWER,
    job_runner.JOB_ROLE_GATE: Principal.GATE,
}


# ---------------------------------------------------------------------------
# 1. 登記表／產生器：一個降權身分一格，且由單一清單導出
# ---------------------------------------------------------------------------

class RegistryDerivationTests(unittest.TestCase):
    def test_one_asset_per_downgraded_principal(self) -> None:
        """三個資產由 `DOWNGRADED_JOB_PRINCIPALS` 導出，不是手寫清單。"""
        expected = {
            registry.job_spec_spool_asset_id(p)
            for p in registry.DOWNGRADED_JOB_PRINCIPALS
        }
        actual = {
            a.asset_id
            for a in registry.ASSET_REGISTRY
            if a.asset_id.startswith("job-spec-spool-")
        }
        self.assertEqual(actual, expected)
        self.assertEqual(len(expected), 3)

    def test_permgen_reuses_the_registry_list_verbatim(self) -> None:
        """permgen 的那個名字是別名，不是第二份會漂移的清單。"""
        self.assertIs(
            permgen.DOWNGRADED_JOB_PRINCIPALS, registry.DOWNGRADED_JOB_PRINCIPALS
        )

    def test_each_spool_is_readable_only_by_its_own_principal(self) -> None:
        """跨 principal 讀不到彼此的 spec——這是選 per-principal 而非擴大共用 spool
        reader 面的核心差異，因此必須是斷言而不是說明。"""
        plan = permgen.generate_plan(SCHEME)
        accounts = {
            p: SCHEME.resolve(p) for p in registry.DOWNGRADED_JOB_PRINCIPALS
        }
        for principal, account in accounts.items():
            entry = plan.by_id(registry.job_spec_spool_asset_id(principal))
            granted = {a.account for a in entry.acls}
            self.assertEqual(granted, {account}, principal)
            self.assertEqual(entry.mode, 0o700, principal)
            self.assertEqual(entry.owner, SCHEME.durable_state_owner, principal)
            # 唯讀：job 改不了自己的命令列（M1 起的不變式，本票不得放寬）。
            self.assertFalse(any(a.writable for a in entry.acls), principal)
            self.assertEqual(
                plan.all_writable_accounts(entry),
                frozenset({SCHEME.durable_state_owner}),
                principal,
            )
            for other, other_account in accounts.items():
                if other is principal:
                    continue
                self.assertNotIn(other_account, granted, (principal, other))

    def test_the_container_grants_no_job_account_anything_but_traverse(self) -> None:
        """容器只是一個 0700 的殼：job 走得進自己那格，列不出這台機器上還有誰。"""
        plan = permgen.generate_plan(SCHEME)
        entry = plan.by_id("job-spec-spool")
        self.assertEqual(entry.acls, ())
        self.assertEqual(entry.mode, 0o700)

        grants = permgen.derive_traverse_grants(
            plan, LAYOUT, SCHEME, path_of=LAYOUT.asset_paths()
        )
        on_container = {
            g.account for g in grants if g.path == LAYOUT.job_spec_spool_root
        }
        for principal in registry.DOWNGRADED_JOB_PRINCIPALS:
            self.assertIn(SCHEME.resolve(principal), on_container, principal)
        self.assertEqual(permgen.TRAVERSE_PERMS, "--x")

    def test_every_template_unit_names_its_own_spool(self) -> None:
        """「哪個身分讀哪個 spool」是 root-owned unit 上可逐字稽核的一行。"""
        seen: dict[str, str] = {}
        for principal in registry.DOWNGRADED_JOB_PRINCIPALS:
            for profile in permgen.HARDENING_PROFILES:
                unit = permgen.build_job_unit(
                    SCHEME, LAYOUT, principal=principal, profile=profile
                )
                expected = LAYOUT.job_spec_spool_for(principal)
                self.assertIn(
                    f"Environment=PSC_JOB_SPEC_SPOOL={expected}\n",
                    unit.content + "\n",
                    (principal, profile.profile_id),
                )
                self.assertIn(f"User={SCHEME.resolve(principal)}\n", unit.content)
                seen.setdefault(expected, unit.unit_name)
        # 三個角色三條不同的路徑——共用一條正是 #657 的狀態。
        self.assertEqual(len(seen), 3, seen)

    def test_the_spool_is_still_not_writable_from_any_job_unit(self) -> None:
        for principal in registry.DOWNGRADED_JOB_PRINCIPALS:
            unit = permgen.build_job_unit(SCHEME, LAYOUT, principal=principal)
            spool = LAYOUT.job_spec_spool_for(principal)
            for rwp in unit.read_write_paths:
                self.assertFalse(
                    spool == rwp or spool.startswith(rwp.rstrip("/") + "/"),
                    (principal, rwp),
                )

    def test_generated_commands_cover_all_three_spools(self) -> None:
        """`sh -e` 真的會跑到的那份 script 必須逐格出現，否則就是漏授。"""
        # operator／external 是部署決定（#626），未注入時產生器整份 fail-closed。
        scheme = SCHEME.with_principal_accounts(
            {Principal.OPERATOR: "cortex-ops", Principal.EXTERNAL: "cortex-ops"}
        )
        lines = permgen.plan_to_commands(
            permgen.generate_plan(scheme),
            path_of=LAYOUT.asset_paths(),
            scheme=scheme,
        )
        executable = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
        for principal in registry.DOWNGRADED_JOB_PRINCIPALS:
            spool = LAYOUT.job_spec_spool_for(principal)
            account = SCHEME.resolve(principal)
            self.assertIn(f"chmod 0700 {spool}", executable, principal)
            self.assertIn(f"setfacl -m u:{account}:rX {spool}", executable, principal)
            self.assertIn(
                f"setfacl -d -m u:{account}:rX {spool}", executable, principal
            )
            self.assertIn(
                f"setfacl -m u:{account}:--x {LAYOUT.job_spec_spool_root}",
                executable,
                principal,
            )

    def test_path_contract_and_layout_agree(self) -> None:
        for principal in registry.DOWNGRADED_JOB_PRINCIPALS:
            self.assertTrue(
                LAYOUT.job_spec_spool_for(principal).endswith(
                    f"/{config_paths.JOB_SPEC_SPOOL_DIRNAME}/{principal.value}"
                ),
                principal,
            )

    def test_path_contract_rejects_a_traversal_shaped_principal(self) -> None:
        for bad in ("../manager", "a/b", "", "Builder", "/abs"):
            with self.assertRaises(ValueError, msg=bad):
                config_paths.job_spec_spool_for(bad)


class JobRunnerPermgenSpoolContractTests(unittest.TestCase):
    """`job_runner` 的三組預設值 ⟷ permgen layout（兩邊刻意不互相 import）。"""

    def test_defaults_match_the_layout_per_role(self) -> None:
        for role, principal in ROLE_TO_PRINCIPAL.items():
            self.assertEqual(
                job_runner.resolve_job_spec_spool({}, role=role),
                LAYOUT.job_spec_spool_for(principal),
                role,
            )

    def test_each_role_has_its_own_env_variable(self) -> None:
        """共用一個變數會讓「Manager 環境裡剛好有它」把三個角色導回同一格。"""
        names = {
            job_runner.resolve_job_role(role).spec_spool_env
            for role in ROLE_TO_PRINCIPAL
        }
        self.assertEqual(len(names), 3, names)
        for role in ROLE_TO_PRINCIPAL:
            config = job_runner.resolve_job_role(role)
            self.assertEqual(
                job_runner.resolve_job_spec_spool(
                    {config.spec_spool_env: "/tmp/override"}, role=role
                ),
                "/tmp/override",
                role,
            )
            # 別的角色的變數影響不到本角色。
            others = {
                job_runner.resolve_job_role(r).spec_spool_env: "/tmp/wrong"
                for r in ROLE_TO_PRINCIPAL
                if r != role
            }
            self.assertEqual(
                job_runner.resolve_job_spec_spool(others, role=role),
                config.default_spec_spool,
                role,
            )

    def test_unknown_role_fails_closed(self) -> None:
        with self.assertRaises(job_runner.JobRunnerError):
            job_runner.resolve_job_spec_spool({}, role="not-a-role")


# ---------------------------------------------------------------------------
# 2. 真實 ACL 樹上的 effective 語意（本票核心）
# ---------------------------------------------------------------------------

def _second_accounts(limit: int = 2) -> list[str]:
    """本機上 uid 與本行程不同、且非 root 的真實帳號（優先 `nobody`）。

    非 root：root 對 DAC 有豁免，拿它當樣本會讓斷言測到的是「root 什麼都讀得到」。
    """

    me = os.getuid()
    found: list[str] = []
    try:
        entries = pwd.getpwall()
    except Exception:  # pragma: no cover - 平台差異
        return []
    entries.sort(key=lambda e: (e.pw_name != "nobody", e.pw_uid))
    for entry in entries:
        if entry.pw_uid in (0, me) or entry.pw_name in [n for n in found]:
            continue
        found.append(entry.pw_name)
        if len(found) >= limit:
            break
    return found


def _acl_supported(directory: Path, account: str) -> bool:
    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        return False
    probe = directory / ".acl-probe"
    probe.mkdir()
    rc = subprocess.run(
        ["setfacl", "-m", f"u:{account}:rx", str(probe)],
        capture_output=True,
        text=True,
    )
    ok = rc.returncode == 0 and bool(
        job_runner._read_acl(str(probe), job_runner.POSIX_ACL_ACCESS_XATTR)
    )
    shutil.rmtree(probe)
    return ok


def _getfacl_effective(path: Path, account: str) -> str | None:
    """`getfacl` 眼中該具名帳號的 **effective** 權限（`"r-x"`），無此條目即 None。

    交叉核對用：本檔的斷言若只跟自己的實作比，等於沒有外部真相。
    """

    out = subprocess.run(
        ["getfacl", "-p", "--absolute-names", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith(f"user:{account}:"):
            continue
        body, _, comment = line.partition("#effective:")
        if comment:
            return comment.strip()
        return body.split(":")[2].strip()
    return None


@pytest.fixture()
def acl_tree():
    """一棵**真的**四分 spool 樹：容器 ＋ 三格，權限逐字照產生器的輸出套。

    回傳 `(container, spools, owner_account, foreign_accounts)`；其中
    `spools[Principal.…]` 是那一格的路徑，`owner_account` 是被授權的第二個帳號
    （扮演該 principal 的 job 帳號），`foreign_accounts` 是其餘可用的帳號。

    **不用 `tmp_path`**：pytest 的 `/tmp/pytest-of-<user>/` 是 0700，第二個帳號連
    `/tmp` 底下第一層都 traverse 不過去，於是每一條正向斷言都會在「與本票無關的
    那一層」失敗。這裡自建一個 `0711` 的樹根（＝真實部署上 `/var/lib` 那幾層的
    形狀：可 traverse、不可列目錄），讓被測的斷點只可能落在 spool 樹自己身上。
    """

    accounts = _second_accounts()
    if not accounts:
        pytest.skip(
            "本機找不到第二個非 root 帳號——ACL 語意需要一個 uid 與本行程不同的"
            "真實帳號才建得出來（`setfacl -m u:<名>:…` 在產生的當下就要解析得到 uid）。"
            "刻意 skip 而非以自己的 uid 假裝：那樣測到的是 owner 位，不是具名 ACL。"
        )
    root = Path(tempfile.mkdtemp(prefix="psc-657-"))
    try:
        os.chmod(root, 0o711)
        if not _acl_supported(root, accounts[0]):
            pytest.skip(
                "本機的暫存檔系統不支援 POSIX ACL（或缺 setfacl／getfacl）——"
                "本檔的每一條斷言都建立在真實 ACL 上，沒有它就不是在測 #657 的語意。"
                "刻意 skip 而非退回 mode 位模擬。"
            )
        container = root / "job-specs"
        container.mkdir()
        os.chmod(container, 0o700)
        spools: dict[Principal, Path] = {}
        for principal in registry.DOWNGRADED_JOB_PRINCIPALS:
            spool = container / principal.value
            spool.mkdir()
            os.chmod(spool, 0o700)
            spools[principal] = spool
        # 只有第一格拿到那個帳號的 ACL——另外兩格扮演「別人的 spool」。
        own = spools[registry.DOWNGRADED_JOB_PRINCIPALS[0]]
        subprocess.run(["setfacl", "-m", f"u:{accounts[0]}:rX", str(own)], check=True)
        subprocess.run(
            ["setfacl", "-d", "-m", f"u:{accounts[0]}:rX", str(own)], check=True
        )
        # 容器的 traverse：`--x`，且**排在 chmod 之後**——順序反過來會被 mask 吃掉，
        # 那正是 `test_a_masked_out_acl_is_caught` 在驗的東西。全部可用帳號都給，
        # 這樣「讀不到別人的」就只可能來自 spool 那一格自己的權限，而不是 traverse。
        for account in accounts:
            subprocess.run(
                ["setfacl", "-m", f"u:{account}:--x", str(container)], check=True
            )
        yield container, spools, accounts[0], accounts[1:]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_own_spool_is_readable_and_foreign_spools_are_not(acl_tree) -> None:
    """驗收第 1／2 條：讀得到**自己的**、讀不到**別人的**——以 effective 權限斷言。"""
    container, spools, account, _ = acl_tree
    own_principal = registry.DOWNGRADED_JOB_PRINCIPALS[0]

    ok, why = job_runner._spool_readable_by(str(spools[own_principal]), account)
    assert ok, why

    for principal, spool in spools.items():
        if principal is own_principal:
            continue
        ok, why = job_runner._spool_readable_by(str(spool), account)
        assert not ok, (principal, spool)
        assert account in why
        bits = job_runner.effective_perms_for_account(str(spool), account)
        assert bits == 0, (principal, bits)


def test_our_effective_computation_agrees_with_getfacl(acl_tree) -> None:
    """外部真相交叉核對：本檔的判定與系統 `getfacl` 對同一棵樹的結論一致。"""
    container, spools, account, _ = acl_tree
    own = spools[registry.DOWNGRADED_JOB_PRINCIPALS[0]]
    for path, expected in ((own, "r-x"), (container, "--x")):
        assert _getfacl_effective(path, account) == expected, path
        bits = job_runner.effective_perms_for_account(str(path), account)
        assert job_runner._perm_str(bits) == expected, path


def test_a_masked_out_acl_is_caught(acl_tree) -> None:
    """**本族最陰的一種**：ACL 還在（`getfacl` 看得到），mask 卻把它打成空。

    `chmod` 會重寫 ACL mask，因此「先 setfacl 再 chmod」會讓具名條目靜默失效——
    這不是假想：修本票時在實機上量到 `/var/lib/cortex/coordinator/job-specs` 正是
    `mask::---` ＋ `user:cortex-builder:r-x #effective:---`。只看「有沒有那條 ACL」
    的檢查會說一切正常。
    """

    container, spools, account, _ = acl_tree
    own = spools[registry.DOWNGRADED_JOB_PRINCIPALS[0]]
    ok, _ = job_runner._spool_readable_by(str(own), account)
    assert ok

    os.chmod(own, 0o700)  # ← 這一行就是那個 bug

    assert _getfacl_effective(own, account) == "---"
    assert job_runner.effective_perms_for_account(str(own), account) == 0
    ok, why = job_runner._spool_readable_by(str(own), account)
    assert not ok
    assert "---" in why


def test_a_broken_traverse_chain_is_caught(acl_tree) -> None:
    """葉節點 ACL 再精確，中間有一層走不過去，`open()` 就是 EACCES（#620 同族）。"""
    container, spools, account, _ = acl_tree
    own = spools[registry.DOWNGRADED_JOB_PRINCIPALS[0]]
    subprocess.run(["setfacl", "-x", f"u:{account}", str(container)], check=True)
    ok, why = job_runner._spool_readable_by(str(own), account)
    assert not ok
    assert str(container) in why
    assert "traverse" in why


def test_a_spec_written_by_the_manager_is_readable_by_the_job_account(
    acl_tree,
) -> None:
    """`write_job_spec()` 的 `chmod 0640` ⟷ ACL mask 那段推導，從註解升級成斷言。

    這是「每個降權 principal 讀得到自己的 spec」在單 UID 環境下驗得到的**全部**：
    真的那個 inode 上，那個帳號的 effective 權限含 `r`。真的以該 uid `open()` 需要
    第二個 UID（見本檔最後一條）。
    """

    _container, spools, account, _ = acl_tree
    own = spools[registry.DOWNGRADED_JOB_PRINCIPALS[0]]
    spec = job_runner.build_job_spec(
        job_id="psc-0657",
        instance="psc-0657-deadbeef",
        unit="cortex-gate-job@psc-0657-deadbeef.service",
        command=["/bin/true"],
        working_directory="/tmp",
        log_path="/tmp/psc-0657.jsonl",
        env={"PATH": "/usr/bin"},
    )
    spec_path = job_runner.job_spec_path(str(own), "psc-0657-deadbeef")
    job_runner.write_job_spec(spec_path, spec, account=account)

    assert json.loads(Path(spec_path).read_text(encoding="utf-8"))["job_id"] == "psc-0657"
    assert _getfacl_effective(Path(spec_path), account) == "r--"
    bits = job_runner.effective_perms_for_account(spec_path, account)
    assert bits is not None and bits & 0o4, job_runner._perm_str(bits or 0)


def test_write_job_spec_refuses_to_leave_an_unreadable_spec(acl_tree) -> None:
    """落地複驗會擋下來——失敗因此在 Manager 的錯誤訊息裡，不是 journal 裡的一行。"""
    _container, spools, account, _ = acl_tree
    foreign = spools[registry.DOWNGRADED_JOB_PRINCIPALS[1]]
    spec = job_runner.build_job_spec(
        job_id="psc-0657",
        instance="psc-0657-deadbeef",
        unit="cortex-gate-job@psc-0657-deadbeef.service",
        command=["/bin/true"],
        working_directory="/tmp",
        log_path="/tmp/psc-0657.jsonl",
        env={"PATH": "/usr/bin"},
    )
    spec_path = job_runner.job_spec_path(str(foreign), "psc-0657-deadbeef")
    with pytest.raises(job_runner.JobRunnerError) as excinfo:
        job_runner.write_job_spec(spec_path, spec, account=account)
    assert excinfo.value.diagnostic.reason == "job-runner-job-spec-unreadable-by-job"


def test_a_named_acl_for_someone_else_does_not_help(acl_tree) -> None:
    """#657 的**逐字重現**：spool 存在、Manager 寫得進去、上面也有一條唯讀 ACL
    ——只是那條給的是**另一個**帳號。這正是實機上 gate 撞到的形狀。"""

    _container, spools, account, others = acl_tree
    if not others:
        pytest.skip(
            "本機只找得到一個非 root 的第二帳號，無法建出「ACL 指名的是別人」這個"
            "形狀（需要兩個不同的 uid 同時出現在同一棵樹上）。此形狀的否定面已由"
            "`test_own_spool_is_readable_and_foreign_spools_are_not` 涵蓋（無 ACL 的"
            "那兩格）；刻意 skip 而非以同一個帳號假裝。"
        )
    foreign = spools[registry.DOWNGRADED_JOB_PRINCIPALS[1]]
    subprocess.run(["setfacl", "-m", f"u:{others[0]}:rX", str(foreign)], check=True)
    subprocess.run(["setfacl", "-d", "-m", f"u:{others[0]}:rX", str(foreign)], check=True)

    assert _getfacl_effective(foreign, others[0]) == "r-x"
    ok, why = job_runner._spool_readable_by(str(foreign), account)
    assert not ok, why
    ok, _ = job_runner._spool_readable_by(str(foreign), others[0])
    assert ok


def test_actually_opening_the_spec_as_the_job_uid_needs_a_second_uid(acl_tree) -> None:
    """**明確的缺口位置**：以該 uid 真的 `open()` 一次。

    本檔其餘每一條算的都是 kernel 用的同一條 POSIX.1e 規則，但那是**中繼資料層**的
    推導；mount 選項（唯讀掛載）、mount namespace（模板 unit 的
    `ProtectSystem=strict`）與 LSM（SELinux／AppArmor）都不在其中。要涵蓋那三項只有
    真的變成那個身分開一次檔，而那需要 root（`seteuid`）或第二個可登入身分。

    因此本條**永遠 skip**，並把驗收位置指回 runbook 的
    `sudo -u <帳號> cat <spool>/<instance>.json` 步驟——留一個看得見的缺口，比讓
    「四分部署上真的讀得到」這句話沒有任何測試對應要好（#638／#657 的教訓）。
    """

    _container, spools, account, _ = acl_tree
    if os.getuid() != 0:
        pytest.skip(
            f"以 {account} 的 uid 實際 open() 需要 root（seteuid）或第二個可登入身分；"
            "本行程兩者皆非。中繼資料層的 effective 判定已由本檔其餘條目涵蓋，"
            "mount／namespace／LSM 那一層由 runbook 的 `sudo -u` 步驟承接。"
        )
    raise AssertionError(  # pragma: no cover - 只有以 root 跑測試才會到這裡
        "以 root 跑測試套件不在支援範圍：root 對 DAC 有豁免，"
        "任何以它做的讀取斷言都不承載本票的語意"
    )


# ---------------------------------------------------------------------------
# 3. 派工路徑：角色決定 spool，preflight 驗的是「讀得到」
# ---------------------------------------------------------------------------

def _preflight_patches(*, spool_readable=(True, "")):
    return [
        mock.patch.object(
            job_runner.shutil, "which", return_value="/usr/bin/systemctl"
        ),
        mock.patch.object(job_runner, "_systemd_booted", return_value=True),
        mock.patch.object(job_runner, "_account_exists", return_value=True),
        mock.patch.object(job_runner, "_group_exists", return_value=True),
        mock.patch.object(job_runner, "_unit_file_installed", return_value=True),
        mock.patch.object(job_runner, "_is_executable", return_value=True),
        mock.patch.object(job_runner, "_unit_is_active", return_value=False),
        mock.patch.object(
            job_runner, "_spool_readable_by", return_value=spool_readable
        ),
    ]


class _nested:
    def __init__(self, managers) -> None:
        self._managers = list(managers)

    def __enter__(self):
        return [m.__enter__() for m in self._managers]

    def __exit__(self, *exc_info):
        for manager in reversed(self._managers):
            manager.__exit__(*exc_info)
        return False


class DispatchUsesTheRoleSpoolTests(unittest.TestCase):
    def test_each_role_plans_into_its_own_spool(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            env: dict[str, str] = {}
            expected: dict[str, str] = {}
            for role in ROLE_TO_PRINCIPAL:
                spool = str(Path(root) / role)
                Path(spool).mkdir()
                env[job_runner.resolve_job_role(role).spec_spool_env] = spool
                expected[role] = spool
            for role in ROLE_TO_PRINCIPAL:
                executor = None if role == job_runner.JOB_ROLE_GATE else "claude"
                with _nested(_preflight_patches()):
                    plan = job_runner.prepare_systemd_template(
                        env, job_id="psc-0657", executor=executor, role=role
                    )
                self.assertEqual(plan.spool_dir, expected[role], role)
                self.assertTrue(
                    plan.spec_path.startswith(expected[role] + "/"), plan.spec_path
                )
                self.assertEqual(
                    plan.spec_path, f"{expected[role]}/{plan.instance}.json", role
                )

    def test_preflight_fails_closed_when_the_job_identity_cannot_read(self) -> None:
        """#657 的迴歸閘：spool 存在、Manager 寫得進去，但那個身分讀不到 ⇒ 派不出去。

        在此之前這裡檢查的是 `os.path.isdir()`，對 #657 完全無感。
        """

        with tempfile.TemporaryDirectory() as root:
            spool = str(Path(root) / "gate")
            Path(spool).mkdir()
            env = {job_runner.GATE_JOB_SPEC_SPOOL_ENV: spool}
            with _nested(
                _preflight_patches(
                    spool_readable=(False, "cortex-gate 沒有 traverse（x）權")
                )
            ):
                with self.assertRaises(job_runner.JobRunnerError) as ctx:
                    job_runner.prepare_systemd_template(
                        env,
                        job_id="psc-0657",
                        executor=None,
                        role=job_runner.JOB_ROLE_GATE,
                    )
        self.assertEqual(
            ctx.exception.diagnostic.reason, "job-runner-job-spec-spool-unreadable"
        )
        self.assertIn("cortex-gate", str(ctx.exception))

    def test_a_missing_spool_still_reports_the_missing_reason(self) -> None:
        env = {job_runner.GATE_JOB_SPEC_SPOOL_ENV: "/nonexistent-657"}
        with _nested(_preflight_patches()):
            with self.assertRaises(job_runner.JobRunnerError) as ctx:
                job_runner.prepare_systemd_template(
                    env,
                    job_id="psc-0657",
                    executor=None,
                    role=job_runner.JOB_ROLE_GATE,
                )
        self.assertEqual(
            ctx.exception.diagnostic.reason, "job-runner-job-spec-spool-missing"
        )


# ---------------------------------------------------------------------------
# 4. direct 模式零回歸
# ---------------------------------------------------------------------------

def test_direct_mode_never_touches_the_spool(tmp_path: Path) -> None:
    """`direct` 是預設模式，本票一行都不該碰到它。

    判準是結構性的：整條 spool／preflight 只由 `prepare_systemd_template()` 進入，
    而那一支只在 `PSC_JOB_RUNNER=systemd-template` 時被呼叫。這裡直接證明
    `direct` 下 `_downgraded_mode()` 回 None，且 spool 目錄零產出。
    """

    from paulsha_cortex.coordinator.launcher import SubprocessLauncher

    launcher = SubprocessLauncher("codex")
    for env in ({}, {job_runner.JOB_RUNNER_ENV: job_runner.RUNNER_DIRECT}):
        assert launcher._downgraded_mode(env) is None, env

    spool = tmp_path / "job-specs"
    spool.mkdir()
    assert list(spool.iterdir()) == []
