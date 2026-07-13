from __future__ import annotations

import subprocess
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from paulsha_cortex.coordinator import verification


def _git_ok(stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _git_fail(stderr: str = "git failed", returncode: int = 1) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)


def _proc_ok(stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)


def _proc_fail(returncode: int = 1, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _persona_catalog(*, builder_paths: list[str]) -> str:
    return (
        "roles:\n"
        "  manager:\n"
        "    role: manager\n"
        "    version: v1\n"
        "    summary: manager\n"
        "    allowed_phases: [define, plan, build, verify, review, ship]\n"
        "    write_paths: [\"**\"]\n"
        "    allowed_tools: [bash]\n"
        "  builder:\n"
        "    role: builder\n"
        "    version: v1\n"
        "    summary: builder\n"
        f"    write_paths: [{', '.join(f'\"{path}\"' for path in builder_paths)}]\n"
        "    allowed_phases: [build, verify]\n"
        "    allowed_tools: [bash]\n"
        "  reviewer:\n"
        "    role: reviewer\n"
        "    version: v1\n"
        "    summary: reviewer\n"
        "    allowed_phases: [review]\n"
        "    write_paths: [\"**\"]\n"
        "    allowed_tools: [bash]\n"
    )


def _contract(
    *,
    docs_class: str = "code",
    required_artifacts: list[dict] | None = None,
    checks: list[dict] | None = None,
    tests: list[dict] | None = None,
    full_suite: dict | None = None,
) -> dict:
    return {
        "docs_class": docs_class,
        "review_policy": "required" if docs_class in {"code", "normative"} else "not-required",
        "required_artifacts": list(required_artifacts or []),
        "checks": list(
            checks
            or [
                {"kind": "persona-scope"},
                {
                    "kind": "command",
                    "name": "policy",
                    "argv": ["python3", "-m", "pytest", "-q", "tests/policy.py"],
                    "cwd": ".",
                    "timeout_seconds": 30,
                },
            ]
        ),
        "tests": list(tests or []),
        "full_suite": full_suite
        or {
            "argv": ["python3", "-m", "pytest", "-q"],
            "cwd": ".",
            "timeout_seconds": 60,
            "baseline": "no-regression",
        },
    }


def _write_spec_and_plan(root: Path, slice_id: str, contract: dict) -> tuple[Path, Path]:
    plan_path = root / "docs" / "superpowers" / "plans" / f"{slice_id}.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# plan\n", encoding="utf-8")
    spec_path = root / "specs" / f"{slice_id}.md"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(
        (
            "---\n"
            "dispatch: auto\n"
            f"slice_id: {slice_id}\n"
            f"plan: docs/superpowers/plans/{slice_id}.md\n"
            "target_branch: main\n"
            "verification:\n"
            f"  docs_class: {contract['docs_class']}\n"
            "  required_artifacts:\n"
            + (
                "".join(
                    f"    - path: {artifact['path']}\n"
                    f"      must_change: {'true' if artifact['must_change'] else 'false'}\n"
                    for artifact in contract["required_artifacts"]
                )
                or "    []\n"
            )
            + "  checks:\n"
            + "".join(
                (
                    "    - kind: persona-scope\n"
                    if check["kind"] == "persona-scope"
                    else "    - kind: command\n"
                    f"      name: {check['name']}\n"
                    f"      argv: [{', '.join(check['argv'])}]\n"
                    f"      cwd: {check['cwd']}\n"
                    f"      timeout_seconds: {check['timeout_seconds']}\n"
                )
                for check in contract["checks"]
            )
            + (
                "  tests:\n"
                + "".join(
                    "    - argv: [{argv}]\n"
                    "      cwd: {cwd}\n"
                    "      timeout_seconds: {timeout}\n".format(
                        argv=", ".join(test["argv"]),
                        cwd=test["cwd"],
                        timeout=test["timeout_seconds"],
                    )
                    for test in contract["tests"]
                )
                if contract["tests"]
                else "  tests: []\n"
            )
            + "  full_suite:\n"
            f"    argv: [{', '.join(contract['full_suite']['argv'])}]\n"
            f"    cwd: {contract['full_suite']['cwd']}\n"
            f"    timeout_seconds: {contract['full_suite']['timeout_seconds']}\n"
            f"    baseline: {contract['full_suite']['baseline']}\n"
            "---\n"
        ),
        encoding="utf-8",
    )
    return spec_path, plan_path


def _slice_row(root: Path, slice_id: str, contract: dict, *, dispatch_base: str, worktree: Path) -> dict:
    spec_path, plan_path = _write_spec_and_plan(root, slice_id, contract)
    return {
        "slice_id": slice_id,
        "spec": {
            "path": str(spec_path),
            "hash": verification.sha256_bytes(spec_path.read_bytes()),
        },
        "plan": {
            "path": str(plan_path),
            "hash": verification.sha256_bytes(plan_path.read_bytes()),
        },
        "target_branch": "main",
        "target_remote": "origin",
        "dispatch_base": dispatch_base,
        "builder_job_id": f"{slice_id}-1",
        "reviewer_job_id": None,
        "candidate": None,
        "verification": {
            "hash": verification.canonical_json_hash(contract),
            "contract": contract,
        },
        "current_evidence_refs": [],
        "current_evaluation_refs": [],
        "evidence_history": [],
        "evaluation_history": [],
        "actions": [],
        "state": "building",
        "gate_state": "pending",
        "worktree": str(worktree),
    }


def _job(slice_id: str, worktree: Path) -> dict:
    return {
        "job_id": f"{slice_id}-1",
        "task": slice_id,
        "persona": "builder",
        "branch": f"feature/{slice_id}",
        "worktree": str(worktree),
        "status": "exited",
        "exit_code": 0,
    }


class FakeGitRunner:
    def __init__(self, responses: dict[tuple[str, ...], object]) -> None:
        self._responses = {key: (list(value) if isinstance(value, list) else value) for key, value in responses.items()}
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]):
        self.calls.append(list(args))
        key = tuple(args)
        if key not in self._responses:
            if len(args) >= 5 and args[2:] == ["status", "--porcelain", "--untracked-files=all"]:
                return _git_ok("")
            raise AssertionError(f"unexpected git call: {args!r}")
        value = self._responses[key]
        if isinstance(value, list):
            if not value:
                raise AssertionError(f"script exhausted for git call: {args!r}")
            current = value.pop(0)
        else:
            current = value
        if isinstance(current, Exception):
            raise current
        return current


class FakeSubprocessRunner:
    def __init__(self, responses: dict[tuple[str, ...], object]) -> None:
        self._responses = {key: (list(value) if isinstance(value, list) else value) for key, value in responses.items()}
        self.calls: list[dict] = []

    def __call__(self, argv, *, shell, cwd, timeout, env, capture_output, text):
        self.calls.append(
            {
                "argv": list(argv),
                "shell": shell,
                "cwd": cwd,
                "timeout": timeout,
                "env": dict(env),
                "capture_output": capture_output,
                "text": text,
            }
        )
        key = tuple(argv)
        if key not in self._responses:
            raise AssertionError(f"unexpected subprocess call: {argv!r}")
        value = self._responses[key]
        if isinstance(value, list):
            if not value:
                raise AssertionError(f"script exhausted for subprocess call: {argv!r}")
            current = value.pop(0)
        else:
            current = value
        if isinstance(current, Exception):
            raise current
        return current


class ResultVerificationTests(unittest.TestCase):
    def test_fails_when_required_artifact_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            contract = _contract(required_artifacts=[{"path": "artifact.txt", "must_change": False}])
            slice_row = _slice_row(root, "slice-a", contract, dispatch_base="a" * 40, worktree=worktree)
            job = _job("slice-a", worktree)
            git_runner = FakeGitRunner(
                {
                    ("-C", str(root), "rev-parse", job["branch"]): _git_ok("b" * 40),
                    ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok("b" * 40),
                    ("-C", str(root), "merge-base", "--is-ancestor", "a" * 40, "b" * 40): _git_ok(""),
                }
            )
            proc_runner = FakeSubprocessRunner(
                {
                    ("python3", "-m", "pytest", "-q", "tests/policy.py"): _proc_ok(),
                    ("python3", "-m", "pytest", "-q"): [_proc_ok(), _proc_ok()],
                }
            )

            evidence = verification.run_result_verification(
                slice_row=slice_row,
                job=job,
                repo_root=root,
                coordinator_root=root / "coordinator",
                git_runner=git_runner,
                subprocess_runner=proc_runner,
            )

            self.assertEqual(evidence["payload"]["status"], "needs_human")
            self.assertEqual(evidence["payload"]["summary"], "required-artifact-missing")
            self.assertEqual(proc_runner.calls, [])

    def test_fails_when_must_change_artifact_is_absent_from_diff(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            (worktree / "artifact.txt").write_text("present\n", encoding="utf-8")
            base_catalog = _persona_catalog(builder_paths=["artifact.txt"])
            contract = _contract(required_artifacts=[{"path": "artifact.txt", "must_change": True}])
            slice_row = _slice_row(root, "slice-a", contract, dispatch_base="a" * 40, worktree=worktree)
            job = _job("slice-a", worktree)
            git_runner = FakeGitRunner(
                {
                    ("-C", str(root), "rev-parse", job["branch"]): _git_ok("b" * 40),
                    ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok("b" * 40),
                    ("-C", str(root), "merge-base", "--is-ancestor", "a" * 40, "b" * 40): _git_ok(""),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + ".." + "b" * 40): _git_ok(""),
                    ("-C", str(root), "show", "a" * 40 + ":paulsha_cortex/persona/personas.yaml"): _git_ok(base_catalog),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + "..." + "b" * 40): _git_ok("artifact.txt\n"),
                }
            )
            proc_runner = FakeSubprocessRunner({})

            evidence = verification.run_result_verification(
                slice_row=slice_row,
                job=job,
                repo_root=root,
                coordinator_root=root / "coordinator",
                git_runner=git_runner,
                subprocess_runner=proc_runner,
            )

            self.assertEqual(evidence["payload"]["status"], "needs_human")
            self.assertEqual(evidence["payload"]["summary"], "required-artifact-unchanged")

    def test_required_artifact_diff_failure_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            (worktree / "artifact.txt").write_text("present\n", encoding="utf-8")
            contract = _contract(required_artifacts=[{"path": "artifact.txt", "must_change": True}])
            slice_row = _slice_row(root, "slice-a", contract, dispatch_base="a" * 40, worktree=worktree)
            job = _job("slice-a", worktree)
            git_runner = FakeGitRunner(
                {
                    ("-C", str(root), "rev-parse", job["branch"]): _git_ok("b" * 40),
                    ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok("b" * 40),
                    ("-C", str(worktree), "status", "--porcelain", "--untracked-files=all"): _git_ok(""),
                    ("-C", str(root), "merge-base", "--is-ancestor", "a" * 40, "b" * 40): _git_ok(""),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + ".." + "b" * 40): _git_fail("diff failed"),
                }
            )

            evidence = verification.run_result_verification(
                slice_row=slice_row,
                job=job,
                repo_root=root,
                coordinator_root=root / "coordinator",
                git_runner=git_runner,
                subprocess_runner=FakeSubprocessRunner({}),
            )

            self.assertEqual(evidence["payload"]["status"], "needs_human")
            self.assertEqual(evidence["payload"]["summary"], "required-artifact-diff-error")

    def test_persona_scope_uses_dispatch_base_catalog_hash_even_if_candidate_edits_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worktree = root / "candidate"
            (worktree / "paulsha_cortex" / "persona").mkdir(parents=True)
            (worktree / "src").mkdir(parents=True)
            (worktree / "paulsha_cortex" / "persona" / "personas.yaml").write_text(
                _persona_catalog(builder_paths=["src/**"]),
                encoding="utf-8",
            )
            base_catalog = _persona_catalog(builder_paths=["docs/**"])
            contract = _contract()
            slice_row = _slice_row(root, "slice-a", contract, dispatch_base="a" * 40, worktree=worktree)
            job = _job("slice-a", worktree)
            git_runner = FakeGitRunner(
                {
                    ("-C", str(root), "rev-parse", job["branch"]): _git_ok("b" * 40),
                    ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok("b" * 40),
                    ("-C", str(root), "merge-base", "--is-ancestor", "a" * 40, "b" * 40): _git_ok(""),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + ".." + "b" * 40): _git_ok(""),
                    (
                        "-C",
                        str(root),
                        "show",
                        "a" * 40 + ":paulsha_cortex/persona/personas.yaml",
                    ): _git_ok(base_catalog),
                    (
                        "-C",
                        str(root),
                        "-c",
                        "core.quotepath=false",
                        "diff",
                        "--name-only",
                        "a" * 40 + "..." + "b" * 40,
                    ): _git_ok("src/code.py\npaulsha_cortex/persona/personas.yaml\n"),
                }
            )
            proc_runner = FakeSubprocessRunner({})

            evidence = verification.run_result_verification(
                slice_row=slice_row,
                job=job,
                repo_root=root,
                coordinator_root=root / "coordinator",
                git_runner=git_runner,
                subprocess_runner=proc_runner,
            )

            self.assertEqual(evidence["payload"]["status"], "needs_human")
            self.assertEqual(evidence["payload"]["summary"], "persona-scope-violation")
            self.assertEqual(
                evidence["payload"]["details"]["persona_catalog"]["hash"],
                sha256(base_catalog.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(
                evidence["payload"]["details"]["scope"]["violations"][0]["path"],
                "src/code.py",
            )

    def test_persona_scope_always_uses_builder_role(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            base_catalog = _persona_catalog(builder_paths=["docs/**"])
            contract = _contract()
            slice_row = _slice_row(root, "slice-a", contract, dispatch_base="a" * 40, worktree=worktree)
            job = _job("slice-a", worktree)
            job["persona"] = "reviewer"
            git_runner = FakeGitRunner(
                {
                    ("-C", str(root), "rev-parse", job["branch"]): _git_ok("b" * 40),
                    ("-C", str(worktree), "rev-parse", "HEAD"): [_git_ok("b" * 40), _git_ok("b" * 40)],
                    ("-C", str(worktree), "status", "--porcelain", "--untracked-files=all"): [_git_ok(""), _git_ok("")],
                    ("-C", str(root), "merge-base", "--is-ancestor", "a" * 40, "b" * 40): _git_ok(""),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + ".." + "b" * 40): _git_ok(""),
                    ("-C", str(root), "show", "a" * 40 + ":paulsha_cortex/persona/personas.yaml"): _git_ok(base_catalog),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + "..." + "b" * 40): _git_ok("src/code.py\n"),
                }
            )
            proc_runner = FakeSubprocessRunner({})

            evidence = verification.run_result_verification(
                slice_row=slice_row,
                job=job,
                repo_root=root,
                coordinator_root=root / "coordinator",
                git_runner=git_runner,
                subprocess_runner=proc_runner,
            )

            self.assertEqual(evidence["payload"]["status"], "needs_human")
            self.assertEqual(evidence["payload"]["summary"], "persona-scope-violation")

    def test_command_failures_are_fail_closed(self) -> None:
        cases = {
            "missing": FileNotFoundError("no command"),
            "non-zero": _proc_fail(returncode=2, stderr="bad"),
            "timeout": subprocess.TimeoutExpired(cmd=["python3"], timeout=30),
            "runner-error": RuntimeError("boom"),
            "string-partial": "ok-but-untyped",
            "partial-evidence": object(),
        }
        for expected, command_result in cases.items():
            with self.subTest(expected=expected):
                with tempfile.TemporaryDirectory() as d:
                    root = Path(d)
                    worktree = root / "candidate"
                    worktree.mkdir()
                    base_catalog = _persona_catalog(builder_paths=["**"])
                    contract = _contract(
                        full_suite={
                            "argv": ["python3", "-m", "pytest", "-q"],
                            "cwd": ".",
                            "timeout_seconds": 60,
                            "baseline": "no-regression",
                        }
                    )
                    slice_row = _slice_row(root, "slice-a", contract, dispatch_base="a" * 40, worktree=worktree)
                    job = _job("slice-a", worktree)
                    git_runner = FakeGitRunner(
                        {
                            ("-C", str(root), "rev-parse", job["branch"]): _git_ok("b" * 40),
                            ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok("b" * 40),
                            ("-C", str(root), "merge-base", "--is-ancestor", "a" * 40, "b" * 40): _git_ok(""),
                            ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + ".." + "b" * 40): _git_ok(""),
                            ("-C", str(root), "show", "a" * 40 + ":paulsha_cortex/persona/personas.yaml"): _git_ok(base_catalog),
                            ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + "..." + "b" * 40): _git_ok(""),
                        }
                    )
                    proc_runner = FakeSubprocessRunner(
                        {
                            ("python3", "-m", "pytest", "-q", "tests/policy.py"): command_result,
                        }
                    )

                    evidence = verification.run_result_verification(
                        slice_row=slice_row,
                        job=job,
                        repo_root=root,
                        coordinator_root=root / "coordinator",
                        git_runner=git_runner,
                        subprocess_runner=proc_runner,
                    )

                    self.assertEqual(evidence["payload"]["status"], "needs_human")
                    expected_status = "partial-evidence" if expected == "string-partial" else expected
                    self.assertEqual(evidence["payload"]["details"]["checks"][0]["status"], expected_status)

    def test_invalid_cwd_escape_in_contract_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            contract = _contract(
                checks=[
                    {
                        "kind": "command",
                        "name": "policy",
                        "argv": ["python3", "-m", "pytest", "-q"],
                        "cwd": "../outside",
                        "timeout_seconds": 30,
                    }
                ]
            )
            slice_row = _slice_row(root, "slice-a", contract, dispatch_base="a" * 40, worktree=worktree)
            job = _job("slice-a", worktree)

            evidence = verification.run_result_verification(
                slice_row=slice_row,
                job=job,
                repo_root=root,
                coordinator_root=root / "coordinator",
                git_runner=FakeGitRunner({}),
                subprocess_runner=FakeSubprocessRunner({}),
            )

            self.assertEqual(evidence["payload"]["status"], "needs_human")
            self.assertEqual(evidence["payload"]["summary"], "verification-contract-invalid")
            self.assertIn("cwd", evidence["payload"]["details"]["contract_error"]["field"])

    def test_full_suite_runs_base_and_candidate_with_same_argv_cwd_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            base_catalog = _persona_catalog(builder_paths=["**"])
            contract = _contract(docs_class="code")
            slice_row = _slice_row(root, "slice-a", contract, dispatch_base="a" * 40, worktree=worktree)
            job = _job("slice-a", worktree)
            git_runner = FakeGitRunner(
                {
                    ("-C", str(root), "rev-parse", job["branch"]): [_git_ok("b" * 40), _git_ok("b" * 40)],
                    ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok("b" * 40),
                    ("-C", str(root), "merge-base", "--is-ancestor", "a" * 40, "b" * 40): _git_ok(""),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + ".." + "b" * 40): _git_ok(""),
                    ("-C", str(root), "show", "a" * 40 + ":paulsha_cortex/persona/personas.yaml"): _git_ok(base_catalog),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + "..." + "b" * 40): _git_ok(""),
                    (
                        "-C",
                        str(root),
                        "worktree",
                        "add",
                        "--detach",
                        str(root / ".psc-verification-worktrees" / "slice-a-aaaaaaaaaaaa"),
                        "a" * 40,
                    ): _git_ok(""),
                    (
                        "-C",
                        str(root),
                        "worktree",
                        "remove",
                        "--force",
                        str(root / ".psc-verification-worktrees" / "slice-a-aaaaaaaaaaaa"),
                    ): _git_ok(""),
                }
            )
            proc_runner = FakeSubprocessRunner(
                {
                    ("python3", "-m", "pytest", "-q", "tests/policy.py"): _proc_ok(),
                    ("python3", "-m", "pytest", "-q"): [_proc_fail(returncode=1), _proc_ok()],
                }
            )

            evidence = verification.run_result_verification(
                slice_row=slice_row,
                job=job,
                repo_root=root,
                coordinator_root=root / "coordinator",
                git_runner=git_runner,
                subprocess_runner=proc_runner,
            )

            self.assertEqual(evidence["payload"]["status"], "reviewing")
            self.assertEqual(evidence["payload"]["details"]["full_suite"]["comparison"], "improved")
            base_call = proc_runner.calls[1]
            candidate_call = proc_runner.calls[2]
            self.assertEqual(base_call["argv"], candidate_call["argv"])
            self.assertEqual(base_call["env"], candidate_call["env"])
            self.assertNotEqual(base_call["cwd"], candidate_call["cwd"])
            self.assertTrue(base_call["cwd"].endswith("slice-a-aaaaaaaaaaaa"))
            self.assertEqual(candidate_call["cwd"], str(worktree))

    def test_both_full_suite_runs_non_zero_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            base_catalog = _persona_catalog(builder_paths=["**"])
            contract = _contract()
            slice_row = _slice_row(root, "slice-a", contract, dispatch_base="a" * 40, worktree=worktree)
            job = _job("slice-a", worktree)
            git_runner = FakeGitRunner(
                {
                    ("-C", str(root), "rev-parse", job["branch"]): [_git_ok("b" * 40), _git_ok("b" * 40)],
                    ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok("b" * 40),
                    ("-C", str(root), "merge-base", "--is-ancestor", "a" * 40, "b" * 40): _git_ok(""),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + ".." + "b" * 40): _git_ok(""),
                    ("-C", str(root), "show", "a" * 40 + ":paulsha_cortex/persona/personas.yaml"): _git_ok(base_catalog),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + "..." + "b" * 40): _git_ok(""),
                    (
                        "-C",
                        str(root),
                        "worktree",
                        "add",
                        "--detach",
                        str(root / ".psc-verification-worktrees" / "slice-a-aaaaaaaaaaaa"),
                        "a" * 40,
                    ): _git_ok(""),
                    (
                        "-C",
                        str(root),
                        "worktree",
                        "remove",
                        "--force",
                        str(root / ".psc-verification-worktrees" / "slice-a-aaaaaaaaaaaa"),
                    ): _git_ok(""),
                }
            )
            proc_runner = FakeSubprocessRunner(
                {
                    ("python3", "-m", "pytest", "-q", "tests/policy.py"): _proc_ok(),
                    ("python3", "-m", "pytest", "-q"): [_proc_fail(returncode=1), _proc_fail(returncode=2)],
                }
            )

            evidence = verification.run_result_verification(
                slice_row=slice_row,
                job=job,
                repo_root=root,
                coordinator_root=root / "coordinator",
                git_runner=git_runner,
                subprocess_runner=proc_runner,
            )

            self.assertEqual(evidence["payload"]["status"], "needs_human")
            self.assertEqual(evidence["payload"]["summary"], "full-suite-both-non-zero")
            self.assertIsNotNone(evidence["payload"]["details"]["full_suite"]["cleanup"])

    def test_success_status_depends_on_docs_class(self) -> None:
        for docs_class, expected in (("informational", "verified"), ("trivial", "verified"), ("code", "reviewing")):
            with self.subTest(docs_class=docs_class):
                with tempfile.TemporaryDirectory() as d:
                    root = Path(d)
                    worktree = root / "candidate"
                    worktree.mkdir()
                    base_catalog = _persona_catalog(builder_paths=["**"])
                    contract = _contract(docs_class=docs_class)
                    slice_row = _slice_row(root, "slice-a", contract, dispatch_base="a" * 40, worktree=worktree)
                    job = _job("slice-a", worktree)
                    git_runner = FakeGitRunner(
                        {
                            ("-C", str(root), "rev-parse", job["branch"]): [_git_ok("b" * 40), _git_ok("b" * 40)],
                            ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok("b" * 40),
                            ("-C", str(root), "merge-base", "--is-ancestor", "a" * 40, "b" * 40): _git_ok(""),
                            ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + ".." + "b" * 40): _git_ok(""),
                            ("-C", str(root), "show", "a" * 40 + ":paulsha_cortex/persona/personas.yaml"): _git_ok(base_catalog),
                            ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + "..." + "b" * 40): _git_ok(""),
                            (
                                "-C",
                                str(root),
                                "worktree",
                                "add",
                                "--detach",
                                str(root / ".psc-verification-worktrees" / "slice-a-aaaaaaaaaaaa"),
                                "a" * 40,
                            ): _git_ok(""),
                            (
                                "-C",
                                str(root),
                                "worktree",
                                "remove",
                                "--force",
                                str(root / ".psc-verification-worktrees" / "slice-a-aaaaaaaaaaaa"),
                            ): _git_ok(""),
                        }
                    )
                    proc_runner = FakeSubprocessRunner(
                        {
                            ("python3", "-m", "pytest", "-q", "tests/policy.py"): _proc_ok(),
                            ("python3", "-m", "pytest", "-q"): [_proc_ok(), _proc_ok()],
                        }
                    )

                    evidence = verification.run_result_verification(
                        slice_row=slice_row,
                        job=job,
                        repo_root=root,
                        coordinator_root=root / "coordinator",
                        git_runner=git_runner,
                        subprocess_runner=proc_runner,
                    )

                    self.assertEqual(evidence["payload"]["status"], expected)

    def test_dirty_candidate_worktree_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            base_catalog = _persona_catalog(builder_paths=["**"])
            contract = _contract()
            slice_row = _slice_row(root, "slice-a", contract, dispatch_base="a" * 40, worktree=worktree)
            job = _job("slice-a", worktree)
            git_runner = FakeGitRunner(
                {
                    ("-C", str(root), "rev-parse", job["branch"]): _git_ok("b" * 40),
                    ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok("b" * 40),
                    ("-C", str(worktree), "status", "--porcelain", "--untracked-files=all"): _git_ok("dirty.py\n"),
                    ("-C", str(root), "merge-base", "--is-ancestor", "a" * 40, "b" * 40): _git_ok(""),
                }
            )

            evidence = verification.run_result_verification(
                slice_row=slice_row,
                job=job,
                repo_root=root,
                coordinator_root=root / "coordinator",
                git_runner=git_runner,
                subprocess_runner=FakeSubprocessRunner({}),
            )

            self.assertEqual(evidence["payload"]["status"], "needs_human")
            self.assertEqual(evidence["payload"]["summary"], "candidate-worktree-dirty")

    def test_worktree_dirty_after_verification_commands_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            base_catalog = _persona_catalog(builder_paths=["**"])
            contract = _contract()
            slice_row = _slice_row(root, "slice-a", contract, dispatch_base="a" * 40, worktree=worktree)
            job = _job("slice-a", worktree)
            git_runner = FakeGitRunner(
                {
                    ("-C", str(root), "rev-parse", job["branch"]): [_git_ok("b" * 40), _git_ok("b" * 40)],
                    ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok("b" * 40),
                    ("-C", str(worktree), "status", "--porcelain", "--untracked-files=all"): [
                        _git_ok(""),
                        _git_ok("dirty-after.py\n"),
                    ],
                    ("-C", str(root), "merge-base", "--is-ancestor", "a" * 40, "b" * 40): _git_ok(""),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + ".." + "b" * 40): _git_ok(""),
                    ("-C", str(root), "show", "a" * 40 + ":paulsha_cortex/persona/personas.yaml"): _git_ok(base_catalog),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + "..." + "b" * 40): _git_ok(""),
                    (
                        "-C",
                        str(root),
                        "worktree",
                        "add",
                        "--detach",
                        str(root / ".psc-verification-worktrees" / "slice-a-aaaaaaaaaaaa"),
                        "a" * 40,
                    ): _git_ok(""),
                    (
                        "-C",
                        str(root),
                        "worktree",
                        "remove",
                        "--force",
                        str(root / ".psc-verification-worktrees" / "slice-a-aaaaaaaaaaaa"),
                    ): _git_ok(""),
                }
            )
            proc_runner = FakeSubprocessRunner(
                {
                    ("python3", "-m", "pytest", "-q", "tests/policy.py"): _proc_ok(),
                    ("python3", "-m", "pytest", "-q"): [_proc_ok(), _proc_ok()],
                }
            )

            evidence = verification.run_result_verification(
                slice_row=slice_row,
                job=job,
                repo_root=root,
                coordinator_root=root / "coordinator",
                git_runner=git_runner,
                subprocess_runner=proc_runner,
            )

            self.assertEqual(evidence["payload"]["status"], "needs_human")
            self.assertEqual(evidence["payload"]["summary"], "candidate-worktree-dirty-after-verification")

    def test_worktree_head_change_after_verification_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            base_catalog = _persona_catalog(builder_paths=["**"])
            contract = _contract()
            slice_row = _slice_row(root, "slice-a", contract, dispatch_base="a" * 40, worktree=worktree)
            job = _job("slice-a", worktree)
            git_runner = FakeGitRunner(
                {
                    ("-C", str(root), "rev-parse", job["branch"]): [_git_ok("b" * 40), _git_ok("b" * 40)],
                    ("-C", str(worktree), "rev-parse", "HEAD"): [_git_ok("b" * 40), _git_ok("c" * 40)],
                    ("-C", str(worktree), "status", "--porcelain", "--untracked-files=all"): [_git_ok(""), _git_ok("")],
                    ("-C", str(root), "merge-base", "--is-ancestor", "a" * 40, "b" * 40): _git_ok(""),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + ".." + "b" * 40): _git_ok(""),
                    ("-C", str(root), "show", "a" * 40 + ":paulsha_cortex/persona/personas.yaml"): _git_ok(base_catalog),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + "..." + "b" * 40): _git_ok(""),
                    (
                        "-C",
                        str(root),
                        "worktree",
                        "add",
                        "--detach",
                        str(root / ".psc-verification-worktrees" / "slice-a-aaaaaaaaaaaa"),
                        "a" * 40,
                    ): _git_ok(""),
                    (
                        "-C",
                        str(root),
                        "worktree",
                        "remove",
                        "--force",
                        str(root / ".psc-verification-worktrees" / "slice-a-aaaaaaaaaaaa"),
                    ): _git_ok(""),
                }
            )
            proc_runner = FakeSubprocessRunner(
                {
                    ("python3", "-m", "pytest", "-q", "tests/policy.py"): _proc_ok(),
                    ("python3", "-m", "pytest", "-q"): [_proc_ok(), _proc_ok()],
                }
            )

            evidence = verification.run_result_verification(
                slice_row=slice_row,
                job=job,
                repo_root=root,
                coordinator_root=root / "coordinator",
                git_runner=git_runner,
                subprocess_runner=proc_runner,
            )

            self.assertEqual(evidence["payload"]["status"], "needs_human")
            self.assertEqual(evidence["payload"]["summary"], "candidate-worktree-moved-after-verification")

    def test_base_full_suite_abnormal_status_takes_precedence_over_candidate_non_zero(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            base_catalog = _persona_catalog(builder_paths=["**"])
            contract = _contract()
            slice_row = _slice_row(root, "slice-a", contract, dispatch_base="a" * 40, worktree=worktree)
            job = _job("slice-a", worktree)
            git_runner = FakeGitRunner(
                {
                    ("-C", str(root), "rev-parse", job["branch"]): [_git_ok("b" * 40), _git_ok("b" * 40)],
                    ("-C", str(worktree), "rev-parse", "HEAD"): [_git_ok("b" * 40), _git_ok("b" * 40)],
                    ("-C", str(worktree), "status", "--porcelain", "--untracked-files=all"): [_git_ok(""), _git_ok("")],
                    ("-C", str(root), "merge-base", "--is-ancestor", "a" * 40, "b" * 40): _git_ok(""),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + ".." + "b" * 40): _git_ok(""),
                    ("-C", str(root), "show", "a" * 40 + ":paulsha_cortex/persona/personas.yaml"): _git_ok(base_catalog),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", "a" * 40 + "..." + "b" * 40): _git_ok(""),
                    (
                        "-C",
                        str(root),
                        "worktree",
                        "add",
                        "--detach",
                        str(root / ".psc-verification-worktrees" / "slice-a-aaaaaaaaaaaa"),
                        "a" * 40,
                    ): _git_ok(""),
                    (
                        "-C",
                        str(root),
                        "worktree",
                        "remove",
                        "--force",
                        str(root / ".psc-verification-worktrees" / "slice-a-aaaaaaaaaaaa"),
                    ): _git_ok(""),
                }
            )

            def proc_runner(argv, *, shell, cwd, timeout, env, capture_output, text):
                if tuple(argv) == ("python3", "-m", "pytest", "-q", "tests/policy.py"):
                    return _proc_ok()
                if cwd.endswith("slice-a-aaaaaaaaaaaa"):
                    raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
                return _proc_fail(returncode=2)

            evidence = verification.run_result_verification(
                slice_row=slice_row,
                job=job,
                repo_root=root,
                coordinator_root=root / "coordinator",
                git_runner=git_runner,
                subprocess_runner=proc_runner,
            )

            self.assertEqual(evidence["payload"]["status"], "needs_human")
            self.assertEqual(evidence["payload"]["summary"], "base-full-suite-timeout")


if __name__ == "__main__":
    unittest.main()
