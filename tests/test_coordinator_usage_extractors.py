from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paulsha_cortex.coordinator.usage_aggregate import aggregate_usage_by_run
from paulsha_cortex.coordinator.usage_extractors import extract_usage


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )


class CodexExtractorTests(unittest.TestCase):
    def test_takes_last_turn_completed_usage_line(self) -> None:
        with TemporaryDirectory() as d:
            log_path = Path(d) / "codex.jsonl"
            _write_jsonl(
                log_path,
                [
                    {"type": "progress"},
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 10,
                            "cache_write_input_tokens": 5,
                            "output_tokens": 50,
                            "reasoning_output_tokens": 20,
                        },
                    },
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 300,
                            "cached_input_tokens": 30,
                            "cache_write_input_tokens": 15,
                            "output_tokens": 150,
                            "reasoning_output_tokens": 60,
                        },
                    },
                ],
            )
            result = extract_usage("codex", str(log_path))
            self.assertIsNotNone(result["usage"])
            self.assertEqual(result["usage"]["input_tokens"], 300)
            self.assertEqual(result["usage"]["output_tokens"], 150)
            self.assertEqual(result["usage"]["cached_input_tokens"], 30)
            self.assertEqual(result["usage"]["reasoning_output_tokens"], 60)
            self.assertEqual(result["usage"]["source"], "codex")
            self.assertIsNone(result["usage_reason"])
            self.assertEqual(result["usage_raw"]["cache_write_input_tokens"], 15)

    def test_no_turn_completed_usage_line_is_fail_soft(self) -> None:
        with TemporaryDirectory() as d:
            log_path = Path(d) / "codex.jsonl"
            _write_jsonl(log_path, [{"type": "progress"}])
            result = extract_usage("codex", str(log_path))
            self.assertIsNone(result["usage"])
            self.assertTrue(result["usage_reason"])


class ClaudeExtractorTests(unittest.TestCase):
    def test_prefers_top_level_result_usage(self) -> None:
        with TemporaryDirectory() as d:
            log_path = Path(d) / "claude.jsonl"
            _write_jsonl(
                log_path,
                [
                    {"type": "message", "message": {"usage": {"input_tokens": 10, "output_tokens": 5}}},
                    {
                        "type": "result",
                        "usage": {
                            "input_tokens": 1000,
                            "output_tokens": 400,
                            "cache_read_input_tokens": 200,
                            "cache_creation_input_tokens": 999,
                        },
                        "modelUsage": {"claude-x": {}},
                    },
                ],
            )
            result = extract_usage("claude", str(log_path))
            self.assertIsNotNone(result["usage"])
            self.assertEqual(result["usage"]["input_tokens"], 1000)
            self.assertEqual(result["usage"]["output_tokens"], 400)
            # cache_read_input_tokens（讀到）而非 cache_creation_input_tokens（寫入）
            self.assertEqual(result["usage"]["cached_input_tokens"], 200)
            self.assertIsNone(result["usage"]["reasoning_output_tokens"])
            self.assertIsNone(result["usage_reason"])

    def test_falls_back_to_accumulated_message_usage_when_result_usage_missing(self) -> None:
        with TemporaryDirectory() as d:
            log_path = Path(d) / "claude.jsonl"
            _write_jsonl(
                log_path,
                [
                    {
                        "type": "message",
                        "message": {
                            "usage": {
                                "input_tokens": 10,
                                "output_tokens": 5,
                                "cache_read_input_tokens": 1,
                            }
                        },
                    },
                    {
                        "type": "message",
                        "message": {
                            "usage": {
                                "input_tokens": 20,
                                "output_tokens": 15,
                                "cache_read_input_tokens": 2,
                            }
                        },
                    },
                    {"type": "result", "ok": True},
                ],
            )
            result = extract_usage("claude", str(log_path))
            self.assertIsNotNone(result["usage"])
            self.assertEqual(result["usage"]["input_tokens"], 30)
            self.assertEqual(result["usage"]["output_tokens"], 20)
            self.assertEqual(result["usage"]["cached_input_tokens"], 3)
            self.assertIn("fallback", result["usage_reason"])

    def test_no_usage_data_at_all_is_fail_soft(self) -> None:
        with TemporaryDirectory() as d:
            log_path = Path(d) / "claude.jsonl"
            _write_jsonl(log_path, [{"type": "result", "ok": True}])
            result = extract_usage("claude", str(log_path))
            self.assertIsNone(result["usage"])
            self.assertTrue(result["usage_reason"])


class CopilotExtractorTests(unittest.TestCase):
    def test_accumulates_assistant_message_output_tokens(self) -> None:
        with TemporaryDirectory() as d:
            log_path = Path(d) / "copilot.jsonl"
            _write_jsonl(
                log_path,
                [
                    {"type": "assistant.message", "data": {"outputTokens": 100}},
                    {"type": "assistant.message", "data": {"outputTokens": 250}},
                    {"type": "other", "data": {"outputTokens": 99999}},
                ],
            )
            result = extract_usage("copilot", str(log_path))
            self.assertIsNotNone(result["usage"])
            self.assertEqual(result["usage"]["output_tokens"], 350)
            self.assertIsNone(result["usage"]["input_tokens"])
            self.assertEqual(result["usage_reason"], "copilot: input tokens unavailable in log format")

    def test_result_line_usage_is_never_read_as_token_count(self) -> None:
        """驗證『result 行有 usage 欄位』不被誤讀——那其實是 session 層統計
        （premiumRequests/duration/codeChanges），不含 token 數。若實作改回
        優先讀 result.usage，這條測試應該失敗。"""
        with TemporaryDirectory() as d:
            log_path = Path(d) / "copilot.jsonl"
            _write_jsonl(
                log_path,
                [
                    {"type": "assistant.message", "data": {"outputTokens": 42}},
                    {
                        "type": "result",
                        "usage": {
                            "premiumRequests": 1,
                            "duration": 12345,
                            "codeChanges": 3,
                        },
                    },
                ],
            )
            result = extract_usage("copilot", str(log_path))
            self.assertIsNotNone(result["usage"])
            self.assertEqual(result["usage"]["output_tokens"], 42)
            self.assertNotEqual(result["usage"]["output_tokens"], 12345)
            self.assertNotEqual(result["usage"]["output_tokens"], 1)

    def test_no_assistant_message_events_is_fail_soft(self) -> None:
        with TemporaryDirectory() as d:
            log_path = Path(d) / "copilot.jsonl"
            _write_jsonl(log_path, [{"type": "result", "usage": {"premiumRequests": 1}}])
            result = extract_usage("copilot", str(log_path))
            self.assertIsNone(result["usage"])
            self.assertTrue(result["usage_reason"])


class AgyExtractorTests(unittest.TestCase):
    def test_always_unsupported(self) -> None:
        with TemporaryDirectory() as d:
            log_path = Path(d) / "agy.jsonl"
            _write_jsonl(log_path, [{"type": "turn.completed", "usage": {"input_tokens": 1}}])
            result = extract_usage("agy", str(log_path))
            self.assertIsNone(result["usage"])
            self.assertIn("unsupported", result["usage_reason"])

    def test_unsupported_even_when_log_missing(self) -> None:
        result = extract_usage("agy", "/nonexistent/agy.jsonl")
        self.assertIsNone(result["usage"])
        self.assertIn("unsupported", result["usage_reason"])


class FailSoftEdgeCaseTests(unittest.TestCase):
    def test_missing_log_path_is_fail_soft(self) -> None:
        result = extract_usage("codex", None)
        self.assertIsNone(result["usage"])
        self.assertTrue(result["usage_reason"])

    def test_nonexistent_log_file_is_fail_soft(self) -> None:
        result = extract_usage("codex", "/nonexistent/path/log.jsonl")
        self.assertIsNone(result["usage"])
        self.assertTrue(result["usage_reason"])

    def test_malformed_jsonl_is_fail_soft(self) -> None:
        with TemporaryDirectory() as d:
            log_path = Path(d) / "codex.jsonl"
            log_path.write_text("not json\n{also not json\n", encoding="utf-8")
            result = extract_usage("codex", str(log_path))
            self.assertIsNone(result["usage"])
            self.assertTrue(result["usage_reason"])

    def test_unknown_executor_is_fail_soft(self) -> None:
        with TemporaryDirectory() as d:
            log_path = Path(d) / "log.jsonl"
            _write_jsonl(log_path, [{"type": "turn.completed", "usage": {"input_tokens": 1}}])
            result = extract_usage("some-unknown-executor", str(log_path))
            self.assertIsNone(result["usage"])
            self.assertTrue(result["usage_reason"])

    def test_none_executor_is_fail_soft(self) -> None:
        result = extract_usage(None, None)
        self.assertIsNone(result["usage"])
        self.assertTrue(result["usage_reason"])


class AggregateUsageByRunTests(unittest.TestCase):
    def test_sums_usage_across_jobs_in_same_run(self) -> None:
        jobs = [
            {
                "workflow_run_id": "wf-1",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_input_tokens": 10,
                    "reasoning_output_tokens": 5,
                },
            },
            {
                "workflow_run_id": "wf-1",
                "usage": {
                    "input_tokens": 200,
                    "output_tokens": 75,
                    "cached_input_tokens": 20,
                    "reasoning_output_tokens": None,
                },
            },
        ]
        result = aggregate_usage_by_run(jobs, "wf-1")
        self.assertEqual(result["input_tokens"], 300)
        self.assertEqual(result["output_tokens"], 125)
        self.assertEqual(result["cached_input_tokens"], 30)
        self.assertEqual(result["reasoning_output_tokens"], 5)
        self.assertEqual(result["job_count"], 2)
        self.assertEqual(result["jobs_with_usage"], 2)

    def test_job_with_none_usage_does_not_disrupt_aggregation(self) -> None:
        jobs = [
            {
                "workflow_run_id": "wf-1",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_input_tokens": 10,
                    "reasoning_output_tokens": 5,
                },
            },
            {"workflow_run_id": "wf-1", "usage": None},
            {"workflow_run_id": "wf-2", "usage": {"input_tokens": 999}},
        ]
        result = aggregate_usage_by_run(jobs, "wf-1")
        self.assertEqual(result["input_tokens"], 100)
        self.assertEqual(result["job_count"], 2)
        self.assertEqual(result["jobs_with_usage"], 1)


if __name__ == "__main__":
    unittest.main()
