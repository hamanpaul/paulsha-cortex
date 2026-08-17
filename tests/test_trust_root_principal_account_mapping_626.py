"""#626：principal→真實 OS 帳號的對應外部化 ＋ 未對應時 fail-closed。

Phase 2b 實機症狀：`permissions three-way --commands --paths` 為 `operator` 與
`cortex-outbox` 兩個**抽象角色名**印出 `setfacl -m u:operator:rX …`。它們不是真實
帳號，`setfacl` 回 `Invalid argument near character 3`，而 runbook 第 2b 步是
`sudo sh -e /tmp/p2b-permissions.sh`——`sh -e` 遇到第一條就**中止整份 script**，留下
一棵半套用的權限樹（前段已 chown/chmod、後段完全沒動），錯誤訊息還看不出是
「帳號不存在」。#624 的 traverse ACL 讓這條更嚴重（多產了幾條同樣的 phantom ACL）。

驗收（對應 issue 的四條）：
- (a) 未指定對應時 fail-closed，且訊息含 principal 名與指定方式；
- (b) 指定後輸出的每個 `u:<name>:` 都可解析（都在方案宣告的帳號集合內）；
- (c) **不變式**：輸出中不得出現任何不在帳號集合內的字面值——這條要能擋住未來
      新增 principal 時再犯同一個錯；
- (d) 兩個 scheme 都測。
"""
from __future__ import annotations

import re

import pytest

from paulsha_cortex.trust_root import permgen
from paulsha_cortex.trust_root.__main__ import main
from paulsha_cortex.trust_root.permgen import (
    ABSENT_ACCOUNT,
    PRINCIPAL_ACCOUNT_OPTIONS,
    THREE_WAY_SCHEME,
    TWO_WAY_SCHEME,
    UidScheme,
    UnknownAccountInOutputError,
    UnresolvedPrincipalError,
    generate_plan,
)
from paulsha_cortex.trust_root.registry import ASSET_REGISTRY, Principal

ALL_SCHEMES = [TWO_WAY_SCHEME, THREE_WAY_SCHEME]

#: issue 已確認的 phantom 集合——登記表裡沒有第三個。這兩個字面值是**角色名**，
#: 實機上不存在對應帳號，任何一個漏進輸出都會讓 `sh -e` 中止整份 script。
PHANTOM_LITERALS = ("cortex-outbox", "operator")

DEPLOYMENT_ACCOUNTS = {
    Principal.OPERATOR: "cortex-ops",
    Principal.EXTERNAL: "cortex-outbox-reader",
}

#: 命令字串裡的 ACL 帳號（與產生器自我檢查同一條 regex 的行為）。
ACL_ACCOUNT_RE = re.compile(r"(?<![\w-])u:([^:\s]+):")


def _resolved(scheme: UidScheme) -> UidScheme:
    return scheme.with_principal_accounts(DEPLOYMENT_ACCOUNTS)


def _commands(scheme: UidScheme) -> list[str]:
    return permgen.plan_to_commands(generate_plan(scheme), path_of=permgen.asset_paths())


# ---------------------------------------------------------------------------
# 根因：對應表缺項，不是填錯——模組層方案不得把角色名當帳號
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_shipped_schemes_do_not_hardcode_a_deployment_decision(scheme) -> None:
    """`operator` 對應到誰是部署決定，程式碼裡不得有預設值（#626 的根因）。"""
    for opt in PRINCIPAL_ACCOUNT_OPTIONS:
        assert getattr(scheme, opt.field_name) is None, opt.field_name
        assert scheme.resolve(opt.principal) is None, opt.principal


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_phantom_literals_are_not_declared_accounts(scheme) -> None:
    """兩個角色名都不在方案宣告的帳號集合內——它們從來就不是帳號。"""
    declared = scheme.declared_accounts()
    for literal in PHANTOM_LITERALS:
        assert literal not in declared, literal


def test_deployment_principals_may_not_hide_in_account_of() -> None:
    """把 `operator` 塞回 `account_of` 會被靜默忽略——第二份真相正是 #626 的成因，
    因此直接拒絕建構。"""
    with pytest.raises(ValueError) as exc:
        UidScheme(
            scheme_id="bad",
            account_of={
                Principal.MANAGER: "cortex-manager",
                Principal.OPERATOR: "operator",
            },
            durable_state_owner="cortex-manager",
        )
    assert "operator" in str(exc.value)
    assert "account_of" in str(exc.value)


# ---------------------------------------------------------------------------
# (a) 未指定對應時 fail-closed，訊息可操作
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_unresolved_principals_are_reported_before_any_output(scheme) -> None:
    unresolved = scheme.unresolved_principals()
    assert {p.value for p in unresolved} == {"external", "operator"}
    assert generate_plan(scheme).unresolved_principals == unresolved


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_commands_fail_closed_when_mapping_is_missing(scheme) -> None:
    """未指定對應時**一行都不輸出**——半套用的權限樹比「產生器拒絕產出」危險得多。"""
    with pytest.raises(UnresolvedPrincipalError) as exc:
        _commands(scheme)
    assert {p.value for p in exc.value.unresolved} == {"external", "operator"}


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_fail_closed_message_names_the_principal_and_how_to_specify_it(scheme) -> None:
    """訊息必須可操作：是哪個 principal、走哪個旗標／env、以及怎麼確認帳號存在。"""
    with pytest.raises(UnresolvedPrincipalError) as exc:
        _commands(scheme)
    message = str(exc.value)
    assert scheme.scheme_id in message
    for opt in PRINCIPAL_ACCOUNT_OPTIONS:
        assert f"`{opt.principal.value}`" in message, opt.principal
        assert opt.cli_flag in message, opt.cli_flag
        assert opt.env_var in message, opt.env_var
    assert "getent passwd" in message


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_partial_mapping_still_fails_closed(scheme) -> None:
    """只補一半也不放行——剩下那個仍會印出 phantom ACL。"""
    half = scheme.with_principal_accounts({Principal.OPERATOR: "cortex-ops"})
    with pytest.raises(UnresolvedPrincipalError) as exc:
        _commands(half)
    assert {p.value for p in exc.value.unresolved} == {"external"}


# ---------------------------------------------------------------------------
# (b) 指定後輸出的每個 u:<name>: 都可解析
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_every_acl_account_resolves_after_mapping_is_supplied(scheme) -> None:
    lines = _commands(_resolved(scheme))
    declared = _resolved(scheme).declared_accounts()
    found = {m.group(1) for line in lines for m in ACL_ACCOUNT_RE.finditer(line)}
    assert found, "應該要有跨帳號 ACL"
    assert found <= declared, sorted(found - declared)


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_supplied_accounts_actually_appear_in_the_output(scheme) -> None:
    """注入的帳號要真的被用上——否則「指定了」與「有效」是兩回事。"""
    joined = "\n".join(_commands(_resolved(scheme)))
    for account in DEPLOYMENT_ACCOUNTS.values():
        assert f"u:{account}:" in joined, account


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_phantom_role_names_never_appear_after_the_fix(scheme) -> None:
    """issue 貼出的那幾行，逐字不得再出現。"""
    joined = "\n".join(_commands(_resolved(scheme)))
    for literal in PHANTOM_LITERALS:
        assert f"u:{literal}:" not in joined, literal
    assert "setfacl -m u:operator:rX" not in joined


# ---------------------------------------------------------------------------
# (c) 不變式：輸出不得出現任何不在帳號集合內的字面值
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_output_contains_no_account_outside_the_declared_set(scheme) -> None:
    """`u:<name>:` 與 `chown <owner>:<group>` 的每個名字都必須是宣告過的帳號。

    這是**擋未來**的那條：新增 principal 而忘了進對應表時，字面角色名一漏進命令
    字串就會被抓出來。
    """
    resolved = _resolved(scheme)
    lines = _commands(resolved)
    assert permgen.unknown_accounts_in(lines, resolved) == ()
    permgen.assert_output_accounts_known(lines, resolved)


def test_self_check_catches_a_future_principal_that_slips_through() -> None:
    """模擬「未來新增一個 principal 卻沒進對應表」：自我檢查必須攔下。

    直接偽造一行帶未知帳號的輸出——不依賴任何未來才存在的 principal，因此這條
    不會因為登記表變動而失效。
    """
    resolved = _resolved(THREE_WAY_SCHEME)
    lines = _commands(resolved) + ["setfacl -m u:future-role:rX /var/lib/cortex/x"]
    assert permgen.unknown_accounts_in(lines, resolved) == ("future-role",)
    with pytest.raises(UnknownAccountInOutputError) as exc:
        permgen.assert_output_accounts_known(lines, resolved)
    assert "future-role" in str(exc.value)


def test_self_check_also_covers_commented_out_per_job_lines() -> None:
    """per-job 資產以註解形式輸出——phantom 躲在 `#   setfacl …` 裡照樣會被複製執行。"""
    resolved = _resolved(THREE_WAY_SCHEME)
    lines = ["#   setfacl -m u:operator:rX /var/lib/cortex/worktree/<job-id>"]
    assert permgen.unknown_accounts_in(lines, resolved) == ("operator",)


def test_self_check_does_not_false_positive_on_names_ending_in_u() -> None:
    """`menu:x:` 不是 `u:` 條目——regex 不得把尾字為 `u` 的名字誤切。"""
    scheme = _resolved(THREE_WAY_SCHEME)
    assert permgen.unknown_accounts_in(["chown menu:menu /tmp/x"], scheme) == ("menu",)
    assert permgen.unknown_accounts_in(["# 說明：menu:x: 不是 ACL"], scheme) == ()


def test_chown_owner_is_checked_too() -> None:
    """ACL 之外，`chown` 的 owner／group 同樣必須是宣告過的帳號。"""
    scheme = _resolved(TWO_WAY_SCHEME)
    lines = ["chown operator:operator /var/lib/cortex/control "]
    assert permgen.unknown_accounts_in(lines, scheme) == ("operator",)


# ---------------------------------------------------------------------------
# 明示「本部署沒有這個角色」——與「忘了指定」嚴格區分
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_absent_account_is_a_decision_not_an_omission(scheme) -> None:
    """本機還沒有 outbox reader 的實體時，明示 absent：授權整組略去、不 fail。"""
    declared_absent = scheme.with_principal_accounts({
        Principal.OPERATOR: "cortex-ops",
        Principal.EXTERNAL: ABSENT_ACCOUNT,
    })
    assert declared_absent.unresolved_principals() == ()
    assert declared_absent.resolve(Principal.EXTERNAL) is None
    assert ABSENT_ACCOUNT not in declared_absent.declared_accounts()
    joined = "\n".join(_commands(declared_absent))
    assert ABSENT_ACCOUNT not in joined
    assert "u:cortex-ops:" in joined


def test_absent_external_reader_drops_exactly_that_principals_acls() -> None:
    """略去的只有那個 principal 的授權——別人的 ACL 一條都不能少。"""
    scheme = THREE_WAY_SCHEME.with_principal_accounts({
        Principal.OPERATOR: "cortex-ops",
        Principal.EXTERNAL: "cortex-outbox-reader",
    })
    absent = scheme.with_principal_accounts({Principal.EXTERNAL: ABSENT_ACCOUNT})
    def _executable(scheme_: UidScheme) -> set[str]:
        return {
            ln for ln in _commands(scheme_)
            if ln.strip() and not ln.lstrip().startswith("#")
        }

    full_lines = _executable(scheme)
    absent_lines = _executable(absent)
    dropped = full_lines - absent_lines
    assert dropped, "略去 external reader 應該少掉幾條 ACL"
    assert all("cortex-outbox-reader" in line for line in dropped), sorted(dropped)
    assert absent_lines - full_lines == set(), "不得因此**多**出任何命令"


# ---------------------------------------------------------------------------
# 帳號名的形狀（命令字串是逐字嵌入的，注入必須在產生階段就被擋下）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["x y", "op;rm -rf /", "$(id -u)", "Operator", ""])
def test_account_names_must_look_like_posix_accounts(bad) -> None:
    with pytest.raises(ValueError):
        THREE_WAY_SCHEME.with_principal_accounts({Principal.OPERATOR: bad})


def test_with_principal_accounts_rejects_non_deployment_principals() -> None:
    with pytest.raises(ValueError):
        THREE_WAY_SCHEME.with_principal_accounts({Principal.BUILDER: "cortex-builder"})


def test_with_principal_accounts_does_not_mutate_the_shipped_scheme() -> None:
    _resolved(THREE_WAY_SCHEME)
    assert THREE_WAY_SCHEME.operator_account is None
    assert THREE_WAY_SCHEME.external_reader_account is None


# ---------------------------------------------------------------------------
# (d) CLI：旗標與 env 兩條注入管道，未給時 stdout 全空
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme_id", ["two-way", "three-way"])
def test_cli_fail_closed_prints_nothing_to_stdout(scheme_id, capsys) -> None:
    """重導成 script 時，檔案必須是**空的**，而不是一份跑到一半會中止的半套 script。"""
    assert main([
        "permissions", scheme_id, "--commands", "--paths",
    ]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--operator-account" in captured.err
    assert "operator" in captured.err


@pytest.mark.parametrize("scheme_id", ["two-way", "three-way"])
def test_cli_flag_supplies_the_mapping(scheme_id, capsys) -> None:
    assert main([
        "permissions", scheme_id, "--commands", "--paths",
        "--operator-account", "cortex-ops",
        "--external-reader-account", "cortex-outbox-reader",
    ]) == 0
    out = capsys.readouterr().out
    assert "u:cortex-ops:" in out
    assert "u:operator:" not in out
    assert "u:cortex-outbox:" not in out


def test_cli_flag_accepts_equals_form(capsys) -> None:
    assert main([
        "permissions", "--commands", "--paths",
        "--operator-account=cortex-ops", "--external-reader-account=none",
    ]) == 0
    assert "u:cortex-ops:" in capsys.readouterr().out


def test_cli_env_supplies_the_mapping(monkeypatch, capsys) -> None:
    monkeypatch.setenv("PSC_OPERATOR_ACCOUNT", "cortex-ops")
    monkeypatch.setenv("PSC_EXTERNAL_READER_ACCOUNT", "none")
    assert main(["permissions", "--commands", "--paths"]) == 0
    out = capsys.readouterr().out
    assert "u:cortex-ops:" in out
    assert "u:operator:" not in out


def test_cli_flag_wins_over_env(monkeypatch, capsys) -> None:
    monkeypatch.setenv("PSC_OPERATOR_ACCOUNT", "from-env")
    monkeypatch.setenv("PSC_EXTERNAL_READER_ACCOUNT", "none")
    assert main([
        "permissions", "--commands", "--paths", "--operator-account", "from-flag",
    ]) == 0
    out = capsys.readouterr().out
    assert "u:from-flag:" in out
    assert "from-env" not in out


def test_cli_rejects_an_illegal_account_name(capsys) -> None:
    assert main([
        "permissions", "--commands", "--paths",
        "--operator-account", "op; rm -rf /",
        "--external-reader-account", "none",
    ]) == 2
    assert capsys.readouterr().out == ""


def test_cli_rejects_a_dangling_account_flag(capsys) -> None:
    assert main(["permissions", "--commands", "--operator-account"]) == 2
    assert capsys.readouterr().out == ""


def test_cli_json_mode_surfaces_unresolved_principals(capsys) -> None:
    """JSON 是診斷模式：不 fail-closed，但「少了誰的授權」必須看得見。"""
    assert main(["permissions", "three-way"]) == 0
    captured = capsys.readouterr()
    assert '"unresolved_principals"' in captured.out
    assert '"operator"' in captured.out
    assert "--operator-account" in captured.err


# ---------------------------------------------------------------------------
# 登記表側：phantom 集合恰好是這兩個，沒有第三個
# ---------------------------------------------------------------------------

def test_registry_needs_exactly_the_two_deployment_decisions() -> None:
    """登記表用到的 principal 中，需要部署決定的恰好是 operator 與 external。

    多一個就代表對應表又缺了一項，這條會直接紅——正是 #626 想擋住的復發路徑。
    """
    used: set[Principal] = set()
    for asset in ASSET_REGISTRY:
        used.update(asset.writers)
        used.update(asset.readers)
    covered = {opt.principal for opt in PRINCIPAL_ACCOUNT_OPTIONS}
    fully_mapped = _resolved(THREE_WAY_SCHEME)
    unmapped = {
        p for p in used
        if p is not Principal.ANY_SAME_UID and fully_mapped.resolve(p) is None
    }
    assert unmapped == set(), sorted(p.value for p in unmapped)
    assert covered <= used
