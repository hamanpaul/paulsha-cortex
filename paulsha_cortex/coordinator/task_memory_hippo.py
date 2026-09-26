"""Optional subprocess bridge to Hippo's public task-memory CLI contract.

This module deliberately imports no Hippo package. It treats the CLI as an
untrusted JSON boundary and keeps stdout, stderr, stdin, and execution time
bounded.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import time
from collections.abc import Mapping
from typing import Any

from .task_memory import (
    TaskMemoryContext,
    TaskMemoryError,
    TaskMemoryProviderError,
    _UnsupportedSchema,
    _validate_payload,
)


HIPPO_COMMAND_ENV = "PSC_TASK_MEMORY_HIPPO_CMD"
HIPPO_TIMEOUT_ENV = "PSC_TASK_MEMORY_HIPPO_TIMEOUT_SECONDS"
HIPPO_STDIN_MAX_BYTES = 64 * 1024
HIPPO_STDOUT_MAX_BYTES = 512 * 1024
HIPPO_DEFAULT_TIMEOUT_SECONDS = 10.0
HIPPO_MIN_TIMEOUT_SECONDS = 0.05
HIPPO_MAX_TIMEOUT_SECONDS = 60.0
_STDERR_LINE_MAX_BYTES = 256
_STDERR_CODE_RE = re.compile(r"^hippo-task-memory: ([a-z][a-z0-9-]{0,63})$")

_EXIT_CODES = {
    10: "permission-denied",
    11: "timeout",
    12: "scope-mismatch",
    13: "hash-mismatch",
    14: "unsupported-schema",
    15: "manifest-mismatch",
    16: "invalid-request",
    17: "size-limit",
    18: "provider-error",
}
_CODE_REASONS = {
    "scope-mismatch": "scope-mismatch",
    "hash-mismatch": "content-hash-mismatch",
    "unsupported-schema": "unsupported-schema-major",
    "manifest-mismatch": "manifest-mismatch",
}


class _HippoPermissionDenied(PermissionError):
    provider_code = "permission-denied"


class _HippoTimeout(TimeoutError):
    provider_code = "timeout"


def resolve_hippo_command(environ: Mapping[str, str] | None = None) -> tuple[str, ...] | None:
    """解析 `PSC_TASK_MEMORY_HIPPO_CMD` 的 JSON argv array 或 shlex command。

    MAJOR 修復（issue #857 對抗審查）：啟用 task-memory 時必須由操作者明示
    這個環境變數，且第一個 argv 元素必須是絕對路徑；未設、空白字串或非絕對
    路徑一律視為 provider 缺席（回傳 None，沿用既有 fail-closed 行為），
    不再對第一個元素做 PATH 搜尋——否則 `PSC_TASK_MEMORY_ENABLED=1` 可能
    意外執行 PATH 上第一個叫 `hippo` 的任意 binary。不呼叫 shell，避免注入。
    """

    env = os.environ if environ is None else environ
    raw = env.get(HIPPO_COMMAND_ENV)
    if raw is None or not raw.strip():
        return None
    value = raw.strip()
    try:
        if value.startswith("["):
            decoded = json.loads(value)
            if not isinstance(decoded, list) or any(
                not isinstance(item, str) or not item or "\x00" in item for item in decoded
            ):
                return None
            argv = decoded
        else:
            argv = shlex.split(value)
    except (json.JSONDecodeError, ValueError):
        return None
    if not argv or not os.path.isabs(argv[0]):
        return None
    executable = shutil.which(argv[0])
    if executable is None:
        return None
    argv[0] = executable
    return tuple(argv)


def _configured_timeout(environ: Mapping[str, str] | None) -> float | None:
    env = os.environ if environ is None else environ
    raw = env.get(HIPPO_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return HIPPO_DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return None
    if not HIPPO_MIN_TIMEOUT_SECONDS <= timeout <= HIPPO_MAX_TIMEOUT_SECONDS:
        return None
    return timeout


def _validation_provider_error(error: TaskMemoryError) -> TaskMemoryProviderError:
    code = {
        "scope-mismatch": "scope-mismatch",
        "manifest-mismatch": "manifest-mismatch",
        "content-hash-mismatch": "hash-mismatch",
    }.get(error.reason, "provider-error")
    return TaskMemoryProviderError(code, reason=error.reason)


class HippoTaskMemoryClient:
    """Bounded provider/fetch callbacks scoped to one Cortex task context."""

    def __init__(
        self,
        command_prefix: tuple[str, ...],
        *,
        timeout_seconds: float = HIPPO_DEFAULT_TIMEOUT_SECONDS,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._command_prefix = command_prefix
        self._timeout_seconds = timeout_seconds
        self._environ = dict(environ) if environ is not None else None

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "HippoTaskMemoryClient | None":
        timeout = _configured_timeout(environ)
        command = resolve_hippo_command(environ)
        if timeout is None or command is None:
            return None
        return cls(command, timeout_seconds=timeout, environ=environ)

    def callbacks(self, context: TaskMemoryContext):
        """Return provider and manifest-bound fetch closures for one task."""

        binding: dict[str, Any] = {}

        def provide(envelope: Mapping[str, Any]) -> Mapping[str, Any]:
            if (
                not isinstance(envelope, Mapping)
                or envelope.get("task_id") != context.task_id
                or envelope.get("project") != context.project
            ):
                raise TaskMemoryProviderError("invalid-request", reason="task-id-mismatch")
            delivery = envelope.get("delivery")
            mode = delivery.get("mode") if isinstance(delivery, Mapping) else None
            if mode not in {"note_fetch", "snapshot", "inline"}:
                raise TaskMemoryProviderError("invalid-request", reason="malformed-payload")
            payload = self._invoke("provide", envelope)
            if not isinstance(payload, Mapping):
                raise TaskMemoryProviderError("provider-error", reason="malformed-payload")
            try:
                validated = _validate_payload(
                    payload,
                    context=context,
                    requested_mode=mode,
                )
            except _UnsupportedSchema as exc:
                raise TaskMemoryProviderError(
                    "unsupported-schema", reason="unsupported-schema-major"
                ) from exc
            except TaskMemoryError as exc:
                raise _validation_provider_error(exc) from exc
            binding["envelope"] = _json_copy(dict(envelope))
            binding["payload"] = _json_copy(validated)
            binding["mode"] = mode
            return validated

        def fetch_note(task_id: str, note_id: str) -> str:
            envelope = binding.get("envelope")
            payload = binding.get("payload")
            mode = binding.get("mode")
            if not isinstance(envelope, Mapping) or not isinstance(payload, Mapping):
                raise TaskMemoryProviderError("invalid-request", reason="provider-unavailable")
            if task_id != context.task_id or task_id != envelope.get("task_id"):
                raise TaskMemoryProviderError("invalid-request", reason="task-id-mismatch")
            if mode != "note_fetch":
                raise TaskMemoryProviderError("invalid-request", reason="mode-mismatch")
            try:
                validated = _validate_payload(payload, context=context, requested_mode=mode)
            except _UnsupportedSchema as exc:
                raise TaskMemoryProviderError(
                    "unsupported-schema", reason="unsupported-schema-major"
                ) from exc
            except TaskMemoryError as exc:
                raise _validation_provider_error(exc) from exc
            candidate_map = {
                candidate["note_id"]: candidate for candidate in validated["candidates"]
            }
            candidate = candidate_map.get(note_id)
            manifest = validated["delivery"]["manifest"]
            manifest_entry = next(
                (
                    entry
                    for entry in manifest["entries"]
                    if isinstance(entry, Mapping) and entry.get("note_id") == note_id
                ),
                None,
            )
            if (
                candidate is None
                or not isinstance(manifest_entry, Mapping)
                or manifest_entry.get("content_hash") != candidate.get("content_hash")
                or manifest_entry.get("content_version") != candidate.get("content_version")
            ):
                raise TaskMemoryProviderError(
                    "manifest-mismatch", reason="note-not-in-manifest"
                )
            wrapper = {
                "envelope": dict(envelope),
                "manifest": dict(manifest),
                "note_id": note_id,
            }
            response = self._invoke("fetch", wrapper)
            if not isinstance(response, Mapping) or not isinstance(response.get("content"), str):
                raise TaskMemoryProviderError("provider-error", reason="malformed-payload")
            return response["content"]

        return provide, fetch_note

    def invoke_provide(self, envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        """Run provide without binding callbacks, for scope-negative canaries."""

        payload = self._invoke("provide", envelope)
        if not isinstance(payload, Mapping):
            raise TaskMemoryProviderError("provider-error", reason="malformed-payload")
        return payload

    def _invoke(self, operation: str, request: Mapping[str, Any]) -> Any:
        try:
            encoded = json.dumps(
                dict(request), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise TaskMemoryProviderError("invalid-request", reason="malformed-payload") from exc
        if len(encoded) > HIPPO_STDIN_MAX_BYTES:
            raise TaskMemoryProviderError("size-limit")
        try:
            returncode, stdout, stderr_code = _run_bounded_process(
                (*self._command_prefix, "task-memory", operation),
                encoded,
                timeout_seconds=self._timeout_seconds,
                stdout_limit=HIPPO_STDOUT_MAX_BYTES,
                environ=self._environ,
            )
        except FileNotFoundError as exc:
            raise TaskMemoryProviderError("provider-error", reason="provider-unavailable") from exc
        except PermissionError as exc:
            raise TaskMemoryProviderError("provider-error", reason="provider-unavailable") from exc
        if returncode != 0:
            code = _EXIT_CODES.get(returncode, "provider-error")
            if stderr_code == code:
                code = stderr_code
            if code == "permission-denied":
                raise _HippoPermissionDenied(code)
            if code == "timeout":
                raise _HippoTimeout(code)
            reason = _CODE_REASONS.get(code, "provider-error")
            raise TaskMemoryProviderError(code, reason=reason)
        try:
            return json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise TaskMemoryProviderError("provider-error", reason="malformed-payload") from exc


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise TaskMemoryProviderError("invalid-request", reason="malformed-payload") from exc


def _parse_first_stderr_code(value: bytes) -> str | None:
    first = value.split(b"\n", 1)[0]
    if len(first) > _STDERR_LINE_MAX_BYTES:
        return None
    try:
        line = first.decode("ascii")
    except UnicodeDecodeError:
        return None
    match = _STDERR_CODE_RE.fullmatch(line)
    return match.group(1) if match else None


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (AttributeError, ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _run_bounded_process(
    argv: tuple[str, ...],
    stdin: bytes,
    *,
    timeout_seconds: float,
    stdout_limit: int,
    environ: Mapping[str, str] | None,
) -> tuple[int, bytes, str | None]:
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(environ) if environ is not None else None,
        bufsize=0,
        start_new_session=True,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    streams = {"stdin": process.stdin, "stdout": process.stdout, "stderr": process.stderr}
    for stream in streams.values():
        os.set_blocking(stream.fileno(), False)
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr_first_line = bytearray()
    stderr_line_done = False
    stderr_line_overflow = False
    offset = 0
    timed_out = False
    stdout_overflow = False
    deadline = time.monotonic() + timeout_seconds

    def close_stream(name: str) -> None:
        stream = streams.get(name)
        if stream is None:
            return
        try:
            selector.unregister(stream)
        except (KeyError, ValueError):
            pass
        try:
            stream.close()
        except OSError:
            pass
        streams[name] = None

    try:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        if stdin:
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            close_stream("stdin")
        while True:
            if process.poll() is not None and not selector.get_map():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _kill_process(process)
                break
            if selector.get_map():
                ready = selector.select(min(remaining, 0.1))
            else:
                time.sleep(min(remaining, 0.02))
                ready = ()
            for key, _mask in ready:
                name = key.data
                stream = key.fileobj
                try:
                    if name == "stdin":
                        if offset >= len(stdin):
                            close_stream("stdin")
                            continue
                        offset += os.write(stream.fileno(), stdin[offset : offset + 8192])
                        if offset >= len(stdin):
                            close_stream("stdin")
                    else:
                        read_limit = 4096
                        if name == "stdout":
                            read_limit = min(read_limit, max(1, stdout_limit + 1 - len(stdout)))
                        data = os.read(stream.fileno(), read_limit)
                except (BlockingIOError, InterruptedError):
                    continue
                except BrokenPipeError:
                    close_stream("stdin")
                    continue
                if name != "stdin":
                    if not data:
                        close_stream(name)
                        continue
                    if name == "stdout":
                        if len(stdout) + len(data) > stdout_limit:
                            stdout_overflow = True
                            _kill_process(process)
                            break
                        stdout.extend(data)
                    elif not stderr_line_done and not stderr_line_overflow:
                        newline = data.find(b"\n")
                        head = data if newline < 0 else data[:newline]
                        if len(stderr_first_line) + len(head) > _STDERR_LINE_MAX_BYTES:
                            stderr_line_overflow = True
                        else:
                            stderr_first_line.extend(head)
                        if newline >= 0:
                            stderr_line_done = True
            if timed_out or stdout_overflow:
                break
            if process.poll() is not None and "stdin" in streams and streams["stdin"] is not None:
                close_stream("stdin")
        if timed_out or stdout_overflow:
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        else:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_process(process)
                process.wait()
    finally:
        selector.close()
        for name in tuple(streams):
            close_stream(name)
    if timed_out:
        raise _HippoTimeout("timeout")
    if stdout_overflow:
        raise TaskMemoryProviderError("size-limit")
    stderr_code = (
        None
        if stderr_line_overflow
        else _parse_first_stderr_code(bytes(stderr_first_line))
    )
    return int(process.returncode or 0), bytes(stdout), stderr_code
