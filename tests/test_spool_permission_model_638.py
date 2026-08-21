"""#638：兩個 spool 的 producer／consumer 權限模型（**在真實 ACL 樹上**驗）。

`review-verdict-spool`（Phase 2a／#599）與 `commit-spool`（#623／#636）在三分下有
三個獨立缺陷，operator 全部有實機證據：

1. per-job 目錄用明確 mode 建立會**重設 ACL mask**，把 default ACL 繼承來的具名
   條目壓成 `#effective:---`，producer 因此連建檔都不行；
2. `wx` 無 `r` 的那一格上，producer 建的檔由 producer 擁有，**consumer 讀不到**；
3. 「落地後轉唯讀」若實作成 `chmod` producer 擁有的**檔案**，consumer 根本 chmod
   不了它（只有 owner 或 root 能），而該處刻意不 raise ⇒ **無聲失敗**。

**為什麼這一檔不能只驗 mode**：既有測試全部在單 UID 環境下跑，那裡 ACL mask 不
影響任何事，於是 `mkdir(mode=0o700)` 看起來完全正常——#637 的 CI 全綠卻在實機第一
步就斷掉，正是這個形狀（同類前例：#630 的「綠靠 cwd 剛好是 repo」、#631 的「長
TMPDIR 下 36 個測試靜默 skip」）。本檔因此**自己建出帶 default ACL 的容器**，並
直接斷言具名條目的 **effective 權限**（mask 套用後的結果），不是斷言 mode。

ACL 的設定與讀取一律走 `system.posix_acl_*` xattr（`setfacl`／`getfacl` 用的是同一
個核心介面）：不依賴 `acl` 套件有沒有裝，只依賴檔案系統支不支援 ACL。不支援時
**明確 skip 並說明理由**（見 `_require_acl_support`），不靜默通過。

`_CROSS_UID_SKIP` 那一組是真正的跨 UID 功能驗收（producer 寫得進去／consumer
讀得到／seal 後 producer 改不動），需要 root 才能借到兩個身分；拿不到時同樣明確
skip。結構性的那一組（effective 權限）在任何支援 ACL 的環境都會跑。
"""

from __future__ import annotations

import os
import json
import shutil
import stat
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from paulsha_cortex.config import paths
from paulsha_cortex.coordinator import job_workspace, launcher, spool_slot
from paulsha_cortex.coordinator import review as foreign_review

# ---------------------------------------------------------------------------
# POSIX ACL 的最小實作（設定與讀取都走 xattr，不依賴 setfacl/getfacl 二進位）
# ---------------------------------------------------------------------------

_ACL_USER_OBJ = 0x01
_ACL_USER = 0x02
_ACL_GROUP_OBJ = 0x04
_ACL_MASK = 0x10
_ACL_OTHER = 0x20
_ACL_UNDEFINED_ID = 0xFFFFFFFF

_ACL_DEFAULT_XATTR = "system.posix_acl_default"

_R, _W, _X = 4, 2, 1

#: 借給 producer／consumer 的兩個 uid。跨 UID 那一組只在 root 底下跑，此時 uid
#: 不必真的存在於 `/etc/passwd`——核心的權限判定只看數字。
_PRODUCER_UID = 60001
_CONSUMER_UID = 60002

_KEY = "job-638-0001"


def _acl_blob(entries: list[tuple[int, int, int]]) -> bytes:
    """組出 `system.posix_acl_*` 的 payload（version 2 ＋ 逐筆 tag/perm/id）。

    條目順序必須是 USER_OBJ → USER* → GROUP_OBJ → GROUP* → MASK → OTHER，否則
    核心會回 EINVAL。
    """

    payload = struct.pack("<I", 2)
    for tag, perm, ident in entries:
        payload += struct.pack("<HHI", tag, perm, ident)
    return payload


def _producer_acl(producer_uid: int) -> bytes:
    """實機那一格的形狀：owner 全權、producer **`wx` 無 `r`**、group／other 全關。"""

    return _acl_blob(
        [
            (_ACL_USER_OBJ, _R | _W | _X, _ACL_UNDEFINED_ID),
            (_ACL_USER, _W | _X, producer_uid),
            (_ACL_GROUP_OBJ, 0, _ACL_UNDEFINED_ID),
            (_ACL_MASK, _R | _W | _X, _ACL_UNDEFINED_ID),
            (_ACL_OTHER, 0, _ACL_UNDEFINED_ID),
        ]
    )


@dataclass(frozen=True)
class _AccessAcl:
    """一份 access ACL 的解讀結果。`mask is None` ⇒ 這一項沒有具名條目。"""

    mask: int | None
    named_users: dict[int, int]

    def effective(self, uid: int) -> int:
        """具名條目經 mask 套用後**實際生效**的權限（`getfacl` 的 `#effective:`）。"""

        granted = self.named_users.get(uid)
        if granted is None:
            return 0
        if self.mask is None:
            return granted
        return granted & self.mask


def _read_access_acl(path: Path) -> _AccessAcl:
    raw = os.getxattr(path, spool_slot.ACCESS_ACL_XATTR)
    named: dict[int, int] = {}
    mask: int | None = None
    for offset in range(4, len(raw), 8):
        tag, perm, ident = struct.unpack("<HHI", raw[offset : offset + 8])
        if tag == _ACL_USER:
            named[ident] = perm
        elif tag == _ACL_MASK:
            mask = perm
    return _AccessAcl(mask=mask, named_users=named)


def _require_acl_support(root: Path) -> None:
    """檔案系統不支援 POSIX ACL 時**明確** skip（附理由），不靜默通過。"""

    probe = root / ".acl-probe"
    probe.mkdir()
    try:
        os.setxattr(probe, _ACL_DEFAULT_XATTR, _producer_acl(_PRODUCER_UID))
    except OSError as exc:  # pragma: no cover - 取決於執行環境的檔案系統
        pytest.skip(
            f"此檔案系統不支援 POSIX ACL（設定 {_ACL_DEFAULT_XATTR} 失敗：{exc}）；"
            "#638 驗的是 ACL mask 的行為，沒有 ACL 就沒有可驗的語意——刻意 skip 而非空過"
        )
    child = probe / "inherit"
    child.mkdir()
    try:
        os.getxattr(child, spool_slot.ACCESS_ACL_XATTR)
    except OSError as exc:  # pragma: no cover - 取決於執行環境的檔案系統
        pytest.skip(
            f"此檔案系統不繼承 default ACL（讀取 {spool_slot.ACCESS_ACL_XATTR} 失敗：{exc}）；"
            "#638 驗的正是繼承來的具名條目，沒有繼承就沒有可驗的語意——刻意 skip 而非空過"
        )
    shutil.rmtree(probe)


# ---------------------------------------------------------------------------
# 兩個 spool 的共用參數化（同一組不變式必須同時涵蓋兩邊）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Spool:
    name: str
    container: str
    #: coordinator_root → producer 應該寫出來的那個成果檔路徑（其 parent 即那一格）
    prepare: Callable[[Path], Path]
    #: 成果檔路徑 → None
    seal: Callable[[Path], None]
    artifact: str


_VERDICT_SPOOL = _Spool(
    name="review-verdict-spool",
    container=paths.REVIEW_VERDICT_SPOOL_DIRNAME,
    prepare=lambda root: foreign_review.prepare_review_verdict_spool(
        reviewer_job_id=_KEY, coordinator_root=root
    ),
    seal=foreign_review.seal_review_verdict_spool,
    artifact=spool_slot.REVIEW_VERDICT_FILENAME,
)

_COMMIT_SPOOL = _Spool(
    name="commit-spool",
    container=paths.COMMIT_SPOOL_DIRNAME,
    prepare=lambda root: job_workspace.prepare_commit_spool(
        spool_key=_KEY, coordinator_root=root
    ),
    seal=job_workspace.seal_commit_spool,
    artifact=spool_slot.COMMIT_BUNDLE_FILENAME,
)

_SPOOLS = [_VERDICT_SPOOL, _COMMIT_SPOOL]
_SPOOL_IDS = [spool.name for spool in _SPOOLS]


@pytest.fixture()
def acl_root(tmp_path: Path):
    """一棵可用的暫存樹；不支援 ACL 時 skip。

    刻意用 `tempfile.mkdtemp()` 而不是 `tmp_path` 本身：跨 UID 那一組需要借來的
    uid **traverse 得進來**，而 pytest 的 tmp 根鏈不保證是可穿越的。
    """

    root = Path(tempfile.mkdtemp(prefix="psc-638-"))
    os.chmod(root, 0o755)
    try:
        _require_acl_support(root)
        yield root
    finally:
        _force_rmtree(root)


def _force_rmtree(root: Path) -> None:
    """清掉暫存樹，包含已經 seal 成 `0500` 的那些格。"""

    for current, dirs, _files in os.walk(root):
        for name in [current, *(str(Path(current) / d) for d in dirs)]:
            try:
                os.chmod(name, stat.S_IMODE(os.stat(name).st_mode) | 0o700)
            except OSError:
                pass
    shutil.rmtree(root, ignore_errors=True)


def _make_container(root: Path, spool: _Spool, *, owner_uid: int | None = None) -> Path:
    """建出實機那一格的容器：`0700` owner-only ＋ producer 的 per-account ACL。

    access ACL 讓 producer traverse 得進去，default ACL 讓**每一格**都繼承同一份
    授權——這正是 permgen 依 R1 登記表在實機做的事（`review-verdict-spool` 與
    `commit-spool` 兩個資產的形態刻意相同）。
    """

    container = root / spool.container
    container.mkdir(parents=True)
    acl = _producer_acl(_PRODUCER_UID)
    os.setxattr(container, spool_slot.ACCESS_ACL_XATTR, acl)
    os.setxattr(container, _ACL_DEFAULT_XATTR, acl)
    if owner_uid is not None:
        os.chown(container, owner_uid, -1)
    return container


# ---------------------------------------------------------------------------
# 1. 缺陷 1：per-job 目錄建立不得遮掉繼承來的具名條目
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spool", _SPOOLS, ids=_SPOOL_IDS)
def test_per_job_slot_keeps_the_inherited_acl_effective(acl_root: Path, spool: _Spool) -> None:
    """那一格建立之後，producer 的具名條目**不得**被 mask 遮掉。

    斷言的是 **effective 權限**而不是 mode：mode 在單 UID 下看起來一直都是對的，
    真正決定 producer 進不進得來的是 `mask & 具名條目`。
    """

    _make_container(acl_root, spool)

    artifact = spool.prepare(acl_root)
    slot = artifact.parent

    acl = _read_access_acl(slot)
    assert acl.named_users.get(_PRODUCER_UID) == _W | _X, "繼承來的具名條目不見了"
    assert acl.mask != 0, f"mask 被壓成 --- ⇒ producer 的授權整條失效（{spool.name}）"
    assert acl.effective(_PRODUCER_UID) == _W | _X, (
        f"{spool.name}：具名條目被 mask 遮成 #effective:--- "
        "（這就是實機 `Permission denied` 的成因）"
    )


@pytest.mark.parametrize("spool", _SPOOLS, ids=_SPOOL_IDS)
def test_the_old_explicit_mode_shape_would_still_fail_this_fixture(
    acl_root: Path, spool: _Spool
) -> None:
    """突變驗證：修法前的形狀（`mkdir(mode=0o700)`）在同一個 fixture 下必須是紅的。

    沒有這一條，上面那條測試「綠」有可能只是因為 fixture 根本沒建出 ACL 樹——
    #638 要修的三個缺陷全部是「測試環境測不出來」造成的，這裡先把測試環境本身
    釘住。
    """

    container = _make_container(acl_root, spool)

    legacy = container / "legacy-shape"
    legacy.mkdir(mode=0o700)

    acl = _read_access_acl(legacy)
    assert acl.named_users.get(_PRODUCER_UID) == _W | _X
    assert acl.mask == 0, "明確 mode 應該把 mask 壓成 ---；沒壓掉代表 fixture 沒建出 ACL 樹"
    assert acl.effective(_PRODUCER_UID) == 0


@pytest.mark.parametrize("spool", _SPOOLS, ids=_SPOOL_IDS)
def test_per_job_slot_never_grants_other(acl_root: Path, spool: _Spool) -> None:
    """「不比 0700 更鬆」保住的那一半：`other` 位一律收掉，即使 default ACL 給了它。

    `other::` 與 mask 無關，收窄它不影響任何具名條目——所以這一半是無條件的。
    `group` 位（＝有 ACL 時的 mask）刻意不動，理由見
    `spool_slot.narrow_inherited_mode()`。
    """

    container = _make_container(acl_root, spool)
    loose = _acl_blob(
        [
            (_ACL_USER_OBJ, _R | _W | _X, _ACL_UNDEFINED_ID),
            (_ACL_USER, _W | _X, _PRODUCER_UID),
            (_ACL_GROUP_OBJ, 0, _ACL_UNDEFINED_ID),
            (_ACL_MASK, _R | _W | _X, _ACL_UNDEFINED_ID),
            (_ACL_OTHER, _R | _X, _ACL_UNDEFINED_ID),
        ]
    )
    os.setxattr(container, _ACL_DEFAULT_XATTR, loose)

    slot = spool.prepare(acl_root).parent

    assert stat.S_IMODE(os.stat(slot).st_mode) & 0o007 == 0
    # 收窄 other **不得**連帶把 mask 壓掉——那正是缺陷 1。
    assert _read_access_acl(slot).effective(_PRODUCER_UID) == _W | _X


def test_slot_without_any_acl_is_exactly_owner_only(tmp_path: Path) -> None:
    """無 ACL 的部署（含所有既有測試環境）下「不比 0700 更鬆」逐字成立。

    `mkdir()` 不給 mode 之後初始權限由 umask 決定，因此這條同時釘住「結果與
    operator 的 umask 無關」。
    """

    previous = os.umask(0o002)
    try:
        for spool in _SPOOLS:
            root = tmp_path / spool.name
            root.mkdir()
            slot = spool.prepare(root).parent
            assert stat.S_IMODE(os.stat(slot).st_mode) == 0o700, spool.name
    finally:
        os.umask(previous)


def test_commit_spool_retry_reopens_the_slot_with_the_acl_restored(acl_root: Path) -> None:
    """同一個 key 重跑：解封之後 producer 的具名條目必須**真的**回來。

    `commit-spool` 是唯一有解封需求的那一個（retry 用同一個 slice_id）。做法是整格
    重建而不是 `chmod` 回去——seal 把 mask 收成 `---`，而正確的 mask 只有讓 default
    ACL 重新繼承一次才拿得到（`chmod` 只能猜一個值）。
    """

    _make_container(acl_root, _COMMIT_SPOOL)
    bundle = _COMMIT_SPOOL.prepare(acl_root)
    bundle.write_bytes(b"harvested")
    _COMMIT_SPOOL.seal(bundle)
    assert _read_access_acl(bundle.parent).effective(_PRODUCER_UID) == 0

    again = _COMMIT_SPOOL.prepare(acl_root)

    assert again == bundle
    assert not bundle.exists(), "上一輪的殘留必須清掉"
    assert _read_access_acl(again.parent).effective(_PRODUCER_UID) == _W | _X


def test_review_verdict_slot_keeps_the_pre_seed_guard(acl_root: Path) -> None:
    """ACL 樹上 pre-seed 守衛語意一個位元組沒變：那一格已存在即拒絕派工。"""

    _make_container(acl_root, _VERDICT_SPOOL)
    _VERDICT_SPOOL.prepare(acl_root)

    with pytest.raises(RuntimeError, match="preseeded review verdict spool"):
        _VERDICT_SPOOL.prepare(acl_root)


# ---------------------------------------------------------------------------
# 2. 缺陷 3：seal 封的是目錄，且真的收掉 producer 的授權
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spool", _SPOOLS, ids=_SPOOL_IDS)
def test_seal_revokes_the_named_acl_entry_for_both_spools(acl_root: Path, spool: _Spool) -> None:
    """seal 之後 producer 的具名條目 effective 必須是 `---`。

    這就是「seal 後 producer 改不動」的**機制本身**：producer 進得去那一格的唯一
    路徑是具名條目（容器是 `0700 <consumer>`、`other::---`），mask 一收成 `---`
    它連 traverse 都做不到，既有的檔也再打不開。修法前 seal 是
    `chmod(<producer 擁有的檔>, 0o444)`，那一步在三分下必定 `PermissionError`
    且刻意不 raise ⇒ 無聲失敗。
    """

    _make_container(acl_root, spool)
    artifact = spool.prepare(acl_root)
    artifact.write_bytes(b'{"schema_version": 1, "findings": []}')

    spool.seal(artifact)

    slot = artifact.parent
    assert stat.S_IMODE(os.stat(slot).st_mode) == spool_slot.SEALED_SLOT_MODE
    acl = _read_access_acl(slot)
    assert acl.mask == 0
    assert acl.effective(_PRODUCER_UID) == 0, f"{spool.name}：seal 沒有收掉 producer 的授權"
    # 證據仍讀得到：consumer 是目錄 owner，`0500` 保留了 `r-x`。
    assert artifact.read_bytes() != b""


@pytest.mark.parametrize("spool", _SPOOLS, ids=_SPOOL_IDS)
@pytest.mark.skipif(os.geteuid() == 0, reason="root 不受目錄權限限制，測不到封口")
def test_seal_closes_the_slot_for_new_entries(tmp_path: Path, spool: _Spool) -> None:
    """封口的可見後果：那一格再也建不了新檔（兩個 spool 同一組不變式）。"""

    root = tmp_path / spool.name
    root.mkdir()
    artifact = spool.prepare(root)
    artifact.write_bytes(b"x")

    spool.seal(artifact)

    with pytest.raises(PermissionError):
        (artifact.parent / "smuggled").write_bytes(b"x")


# ---------------------------------------------------------------------------
# 3. 缺陷 2：producer 寫完自己放寬，consumer 才讀得到
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spool", _SPOOLS, ids=_SPOOL_IDS)
def test_producer_publishes_its_artifact_for_the_consumer(tmp_path: Path, spool: _Spool) -> None:
    """降權 unit 常帶 `UMask=0077`；成果若停在 `0600` producer-owned，consumer 就讀不到。"""

    root = tmp_path / spool.name
    root.mkdir()
    artifact = spool.prepare(root)
    previous = os.umask(0o077)
    try:
        artifact.write_bytes(b"produced")
    finally:
        os.umask(previous)
    assert stat.S_IMODE(os.stat(artifact).st_mode) & 0o044 == 0

    assert spool_slot.publish_file(artifact) is True

    assert stat.S_IMODE(os.stat(artifact).st_mode) == spool_slot.PUBLISHED_FILE_MODE


def test_wrapper_publishes_the_verdict_after_the_model_and_before_the_sentinel(
    tmp_path: Path,
) -> None:
    """verdict 的 producer 是模型本身——它不會自己 chmod，所以 wrapper 必須補上。

    真的跑一次 `bash`：模型以 `UMask=0077` 寫出 verdict、exit 3，驗完成之後
    verdict 是 consumer 讀得到的 `0644`，而 script 的 exit code 仍是模型的 3
    （#604：降權模式下 unit 的 exit code 就是它，被污染會讓失敗記成成功）。
    """

    spool_dir = tmp_path / "review-verdicts" / _KEY
    spool_dir.mkdir(parents=True)
    verdict = spool_dir / spool_slot.REVIEW_VERDICT_FILENAME
    sentinel = tmp_path / "r.exit"
    script = launcher.build_wrapper_script(
        inner_argv=["bash", "-c", f"umask 0077; printf '{{}}' > {verdict}; exit 3"],
        sentinel=str(sentinel),
        ledger=str(tmp_path / "r.ledger"),
        worktree=str(tmp_path),
        repo_root=None,
        run_gates=False,
        verdict_file=str(verdict),
    )

    assert script.index("chmod") < script.index(str(sentinel)), "發表段必須排在 sentinel 之前"

    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)

    assert proc.returncode == 3, proc.stderr
    assert sentinel.read_text() == "3"
    assert stat.S_IMODE(os.stat(verdict).st_mode) == spool_slot.PUBLISHED_FILE_MODE


def test_wrapper_verdict_segment_is_silent_when_the_reviewer_wrote_nothing(
    tmp_path: Path,
) -> None:
    """reviewer 失敗、沒產出 verdict：發表段不得吵、也不得改變 exit code。"""

    spool_dir = tmp_path / "review-verdicts" / _KEY
    spool_dir.mkdir(parents=True)
    verdict = spool_dir / spool_slot.REVIEW_VERDICT_FILENAME
    script = launcher.build_wrapper_script(
        inner_argv=["bash", "-c", "exit 9"],
        sentinel=str(tmp_path / "r.exit"),
        ledger=str(tmp_path / "r.ledger"),
        worktree=str(tmp_path),
        repo_root=None,
        run_gates=False,
        write_sentinel=False,
        verdict_file=str(verdict),
    )

    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)

    assert proc.returncode == 9
    assert proc.stderr == ""
    assert not verdict.exists()


def test_wrapper_publishes_atomic_last_message_before_manager_control(
    tmp_path: Path,
) -> None:
    """Codex temp+rename output is readable before Manager records completion."""

    last_message = tmp_path / "job.last.json"
    temporary = tmp_path / "job.last.json.tmp"
    sentinel = tmp_path / "job.exit"
    script = launcher.build_wrapper_script(
        inner_argv=[
            "bash",
            "-c",
            f"umask 0077; printf '{{\"ok\":true}}' > {temporary}; mv -f {temporary} {last_message}; exit 5",
        ],
        sentinel=str(sentinel),
        ledger=str(tmp_path / "job.gates.json"),
        worktree=str(tmp_path),
        repo_root=None,
        run_gates=False,
        last_message_path=str(last_message),
    )

    assert script.index("job.last.json") < script.index("job.exit")
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)

    assert proc.returncode == 5
    assert json.loads(last_message.read_text(encoding="utf-8")) == {"ok": True}
    assert stat.S_IMODE(last_message.stat().st_mode) == spool_slot.PUBLISHED_FILE_MODE
    assert sentinel.read_text(encoding="utf-8") == "5"


def test_wrapper_is_byte_identical_when_neither_spool_is_in_play(tmp_path: Path) -> None:
    """planner／既有測試路徑：沒有任何發表段時 script 逐字與改動前相同。"""

    kwargs = dict(
        inner_argv=["codex", "exec", "prompt"],
        sentinel=str(tmp_path / "s.exit"),
        ledger=str(tmp_path / "s.ledger"),
        worktree=str(tmp_path),
        repo_root=None,
        run_gates=False,
    )

    assert launcher.build_wrapper_script(**kwargs) == (
        f"codex exec prompt; printf %s \"$?\" > {tmp_path / 's.exit'}"
    )


def test_both_spools_publish_with_the_same_mode() -> None:
    """兩個 spool 的發表 mode 必須同源——這正是 #638 要求收斂成一套 helper 的原因。"""

    bundle_command = job_workspace.build_bundle_command(workspace="/w", bundle="/s/b")
    verdict_command = spool_slot.publish_file_command("/s/verdict.json")
    literal = f"{spool_slot.PUBLISHED_FILE_MODE:04o}"

    assert f"chmod {literal} " in bundle_command
    assert f"chmod {literal} " in verdict_command


# ---------------------------------------------------------------------------
# 4. 跨 UID 的功能驗收（正向＋反向）——需要 root 才借得到兩個身分
# ---------------------------------------------------------------------------

_CROSS_UID_SKIP = (
    "跨 UID 驗收需要 root 才能以 producer／consumer 兩個身分實跑；"
    "非 root 環境下同一組不變式由上面的 effective-ACL 斷言承擔（刻意 skip 而非空過）"
)


def _run_as(uid: int, fn: Callable[[], None]) -> int:
    """在 fork 出來的子進程裡以 `uid` 執行 `fn`；回傳 exit code。

    0＝成功、1＝`PermissionError`（預期內的拒絕）、2＝其他例外。
    """

    pid = os.fork()
    if pid == 0:  # pragma: no cover - 子進程
        code = 2
        try:
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
            fn()
            code = 0
        except PermissionError:
            code = 1
        except OSError as exc:
            code = 1 if exc.errno in {1, 13} else 2
        except BaseException:
            code = 2
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    return os.WEXITSTATUS(status)


@pytest.mark.skipif(os.geteuid() != 0, reason=_CROSS_UID_SKIP)
@pytest.mark.parametrize("spool", _SPOOLS, ids=_SPOOL_IDS)
def test_cross_uid_lifecycle_for_both_spools(acl_root: Path, spool: _Spool) -> None:
    """完整的一輪：consumer 建格 → producer 寫 → consumer 讀 → seal → producer 改不動。

    容器由 **consumer** 擁有（實機的 `cortex-manager`），producer 只有 per-account
    的 `wx` 無 `r`——與 permgen 依 R1 登記表產出的形狀相同。
    """

    _make_container(acl_root, spool, owner_uid=_CONSUMER_UID)
    holder: dict[str, Path] = {}

    def _prepare() -> None:
        spool.prepare(acl_root)

    assert _run_as(_CONSUMER_UID, _prepare) == 0, "consumer 建不出那一格"
    artifact = acl_root / spool.container / _KEY / spool.artifact
    holder["artifact"] = artifact

    def _produce() -> None:
        os.umask(0o077)
        with open(artifact, "wb") as handle:
            handle.write(b"produced-by-the-job")
        assert spool_slot.publish_file(artifact) is True

    # 正向 1：producer 寫得進去（缺陷 1 修好之前這一步就是實機的 `Permission denied`）
    assert _run_as(_PRODUCER_UID, _produce) == 0, "producer 寫不進那一格（缺陷 1 復發）"

    # 正向 2：consumer 讀得到 producer 寫的內容（缺陷 2）
    def _consume() -> None:
        assert artifact.read_bytes() == b"produced-by-the-job"

    assert _run_as(_CONSUMER_UID, _consume) == 0, "consumer 讀不到 producer 的成果（缺陷 2 復發）"

    # seal 由 consumer 執行——修法前它 chmod 的是 producer 擁有的檔，必定無聲失敗
    def _seal() -> None:
        spool.seal(artifact)

    assert _run_as(_CONSUMER_UID, _seal) == 0

    # 反向：seal 之後 producer 改不動自己剛寫的東西（缺陷 3）
    def _tamper() -> None:
        with open(artifact, "wb") as handle:
            handle.write(b"TAMPERED")

    assert _run_as(_PRODUCER_UID, _tamper) == 1, "seal 之後 producer 仍改得動（缺陷 3 復發）"

    # consumer 讀到的仍是 producer 當初交付的那一份
    assert holder["artifact"].read_bytes() == b"produced-by-the-job"
