"""Bounded Feishu CLI calls; credentials remain exclusively managed by the CLI."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Literal, TypedDict

from .process_cleanup import terminate_process_group


SEND_TIMEOUT_SECONDS = 10.0
MAX_OUTPUT_BYTES = 64 * 1024


class SendResult(TypedDict):
    outcome: Literal["success", "failed", "unknown"]
    reason: str | None


def _result(outcome: Literal["success", "failed", "unknown"], reason: str | None = None) -> SendResult:
    return {"outcome": outcome, "reason": reason}


@dataclass(frozen=True)
class _CommandResult:
    code: int | None
    output: bytes = b""
    reason: str = ""


def _run(argv: list[str], deadline: float, cancel: Event) -> _CommandResult:
    if cancel.is_set() or time.monotonic() >= deadline:
        return _CommandResult(None, reason="通知发送已取消或超时")
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "agent_run.notification_process",
             str(max(0.0, deadline - time.monotonic())), *argv],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True,
            env={**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, (
                str(Path(__file__).resolve().parent.parent), os.environ.get("PYTHONPATH"),
            )))},
        )
    except OSError:
        return _CommandResult(None, reason="lark-cli 不可用或无法启动")
    assert process.stdout is not None
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map() or process.poll() is None:
                if cancel.is_set():
                    return _CommandResult(None, reason="通知发送已取消，送达结果未知")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return _CommandResult(None, reason="通知发送超时，送达结果未知")
                for key, _ in selector.select(min(0.05, remaining)):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif len(output) + len(chunk) > MAX_OUTPUT_BYTES:
                        return _CommandResult(None, reason="CLI 输出超限，送达结果未知")
                    else:
                        output.extend(chunk)
                if not selector.get_map() and process.poll() is None:
                    cancel.wait(min(0.01, remaining))
            return _CommandResult(process.returncode, bytes(output))
    finally:
        # Closing the owner pipe lets the supervisor reap the CLI tree. The
        # same EOF happens automatically if the Executor is killed abruptly.
        assert process.stdin is not None
        process.stdin.close()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            terminate_process_group(process, timeout=0.5)
        process.stdout.close()


def _object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def send(
    config: Mapping[str, Any],
    card: Mapping[str, Any],
    idempotency_key: str,
    cancel_event: Event,
) -> SendResult:
    """Send once, including identity validation, within a ten-second deadline.

    The caller owns asynchronous execution and retry policy. Reasons never
    persist CLI output, which can contain credentials or personal information.
    A successful process exit alone is not evidence that a message was sent.
    """
    if not config.get("enabled", False):
        return _result("failed", "通知已关闭")
    profile, open_id, app_id = (
        config.get("profile"), config.get("open_id"), config.get("app_id")
    )
    if not all(isinstance(value, str) and value.strip() for value in (profile, open_id, app_id)):
        return _result("failed", "通知配置不完整：需要 profile、open_id 与核对后的 app_id")
    assert isinstance(profile, str) and isinstance(open_id, str) and isinstance(app_id, str)
    if not open_id.startswith("ou_") or not idempotency_key or len(idempotency_key) > 50:
        return _result("failed", "通知接收人或幂等标识无效")
    deadline = time.monotonic() + SEND_TIMEOUT_SECONDS
    identity = _run(
        ["lark-cli", "--profile", profile, "whoami", "--as", "bot"],
        deadline, cancel_event,
    )
    if identity.code != 0:
        return _result("failed", identity.reason or "无法核验 CLI 应用与机器人身份")
    binding = _object(identity.output)
    if binding.get("appId") != app_id or binding.get("profile") != profile:
        return _result("failed", "CLI profile 的应用绑定已改变或无法核验；请重新核对 open_id 与 app_id")
    if binding.get("identity") != "bot" or binding.get("available") is not True:
        return _result("failed", "CLI 机器人身份不可用，请检查该 profile 的凭据")
    if cancel_event.is_set():
        return _result("failed", "通知发送前已取消")
    result = _run(
        [
            "lark-cli", "--profile", profile, "im", "+messages-send", "--as", "bot",
            "--user-id", open_id, "--msg-type", "interactive", "--content",
            json.dumps(dict(card), ensure_ascii=False, separators=(",", ":")),
            "--idempotency-key", idempotency_key, "--format", "json",
        ],
        deadline, cancel_event,
    )
    payload = _object(result.output)
    data = payload.get("data", payload)
    if result.code == 0 and isinstance(data, dict) and data.get("message_id"):
        return _result("success")
    if isinstance(payload.get("code"), int) and payload["code"] != 0:
        return _result("failed", f"飞书拒绝通知请求（错误码 {payload['code']}）")
    return _result("unknown", result.reason or "CLI 未返回送达凭据，送达结果未知")
