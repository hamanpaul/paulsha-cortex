"""#742：reviewer sandbox 的交接（#710 的 reviewer lane 版）。

`review-sandboxes` 是 verify 首走才被 mkdir 出來的 pool（不在部署清單），且
Manager unit 掛 `UMask=0077`，default ACL 的繼承會被 mask 歸零（#736 同族交互）
——`inherited-default-acl` 的 reach 模型對它不成立。修法：容器 `0701`（traverse
不可列，`dispatch-worktree-pool` 先例）＋ per-job 那一格由 owner 顯式
`setfacl -R`（走 #710 的 `grant_workspace_acl`）。本檔釘：帳號存在才授、不存在
零動作（direct 模式零回歸）、grants 形狀、容器 mode。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paulsha_cortex.coordinator import job_workspace, manager


class ContainerTests(unittest.TestCase):
    def test_container_is_created_with_traverse_only_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "review-sandboxes"
            manager._prepare_reviewer_sandbox_container(parent)
            self.assertTrue(parent.is_dir())
            self.assertEqual(parent.stat().st_mode & 0o777, 0o701)

    def test_container_mode_is_converged_on_existing_dirs(self) -> None:
        """已存在的 0700 容器（實機現況）也要收斂到 0701，不只新建的。"""

        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "review-sandboxes"
            parent.mkdir(mode=0o700)
            manager._prepare_reviewer_sandbox_container(parent)
            self.assertEqual(parent.stat().st_mode & 0o777, 0o701)


class GrantTests(unittest.TestCase):
    def test_grant_targets_the_sandbox_with_rwx_named_acl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp) / "s"
            sandbox.mkdir()
            captured: dict[str, object] = {}

            def fake_grant(workspace, grants):
                captured["workspace"] = Path(workspace)
                captured["grants"] = grants
                return "setfacl (captured)"

            with mock.patch.object(
                manager.job_runner, "resolve_job_account", return_value="cortex-reviewer-planner"
            ), mock.patch.object(
                manager.pwd, "getpwnam", return_value=object()
            ), mock.patch.object(
                manager.job_workspace, "grant_workspace_acl", side_effect=fake_grant
            ):
                result = manager._grant_reviewer_sandbox_access(sandbox)
            self.assertEqual(result, "setfacl (captured)")
            self.assertEqual(captured["workspace"], sandbox)
            (grant,) = captured["grants"]
            self.assertEqual(grant.account, "cortex-reviewer-planner")
            self.assertEqual(grant.access_perms, "rwX")
            self.assertEqual(grant.default_perms, "rwX")

    def test_missing_account_is_a_named_no_op(self) -> None:
        """direct／單 UID 模式：帳號不在 passwd ⇒ 零動作（同 UID 本就可達）。"""

        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp) / "s"
            sandbox.mkdir()
            with mock.patch.object(
                manager.job_runner, "resolve_job_account", return_value="cortex-reviewer-planner"
            ), mock.patch.object(
                manager.pwd, "getpwnam", side_effect=KeyError("absent")
            ), mock.patch.object(
                manager.job_workspace, "grant_workspace_acl"
            ) as grant:
                result = manager._grant_reviewer_sandbox_access(sandbox)
            self.assertIsNone(result)
            grant.assert_not_called()

    def test_grant_uses_the_710_validated_shape(self) -> None:
        """grants 必須過 #710 的驗證（帳號名／perms 白名單）——形狀走同一支。"""

        job_workspace._validate_grants(
            (
                job_workspace.WorkspaceAclGrant(
                    account="cortex-reviewer-planner",
                    access_perms="rwX",
                    default_perms="rwX",
                ),
            )
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
