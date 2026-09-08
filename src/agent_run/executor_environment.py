"""Bounded, one-shot environment handoff for detached Executors."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from agent_run.executor_host import ExecutorSpec

DEFAULT_MAX_ENVIRONMENT_BYTES = 1024 * 1024
DEFAULT_CARRIER_TTL_SECONDS = 30.0
_MAX_CARRIER_METADATA_BYTES = 4096
_CARRIER_VERSION = 1


class EnvironmentCarrierError(RuntimeError):
    """The terminal environment cannot be handed to an exact Executor."""


@dataclass(frozen=True)
class CapturedExecutorEnvironment:
    environment: dict[str, str]
    command: tuple[str, ...]


def default_executor_runtime_directory(
    environment: Mapping[str, str] | None = None,
) -> Path:
    selected = os.environ if environment is None else environment
    runtime = selected.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime).expanduser() / "agent-run" / "executor"
    return Path("/tmp") / f"agent-run-{os.geteuid()}" / "executor"


def ensure_executor_runtime_directory(
    environment: Mapping[str, str] | None = None,
) -> Path:
    path = default_executor_runtime_directory(environment)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def capture_executor_environment(
    environment: Mapping[str, str],
    *,
    command: Sequence[str],
    max_bytes: int = DEFAULT_MAX_ENVIRONMENT_BYTES,
) -> CapturedExecutorEnvironment:
    """Validate and size the terminal snapshot before Action admission."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    normalized_environment: dict[str, str] = {}
    for key, value in environment.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise EnvironmentCarrierError("Executor 环境必须只包含字符串")
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise EnvironmentCarrierError("Executor 环境包含无效变量")
        normalized_environment[key] = value
    normalized_command = tuple(command)
    if not normalized_command or any(
        not isinstance(argument, str) or "\0" in argument
        for argument in normalized_command
    ):
        raise EnvironmentCarrierError("Executor 命令无效")
    payload = {
        "version": _CARRIER_VERSION,
        "environment": normalized_environment,
        "command": list(normalized_command),
    }
    encoded_size = len(_encode(payload))
    metadata_reserve = min(_MAX_CARRIER_METADATA_BYTES, max_bytes // 4)
    if encoded_size > max_bytes - metadata_reserve:
        raise EnvironmentCarrierError(
            f"发起终端环境过大（{encoded_size} bytes，限制 {max_bytes} bytes）"
        )
    return CapturedExecutorEnvironment(
        environment=normalized_environment,
        command=normalized_command,
    )


def write_environment_carrier(
    runtime_directory: Path,
    spec: ExecutorSpec,
    captured: CapturedExecutorEnvironment,
    *,
    ttl_seconds: float = DEFAULT_CARRIER_TTL_SECONDS,
) -> Path:
    """Atomically create one private carrier bound to the exact generation."""

    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    runtime_directory = Path(runtime_directory)
    runtime_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        runtime_directory.chmod(0o700)
    except OSError as error:
        raise EnvironmentCarrierError("无法保护 Executor 环境载体目录") from error
    current = time.time()
    payload = {
        "version": _CARRIER_VERSION,
        "binding": _binding(spec),
        "expires_at": current + ttl_seconds,
        "environment": captured.environment,
        "command": list(captured.command),
    }
    encoded = _encode(payload)
    if len(encoded) > DEFAULT_MAX_ENVIRONMENT_BYTES:
        raise EnvironmentCarrierError("Executor 环境载体过大")
    descriptor, temporary = tempfile.mkstemp(
        prefix="environment-", suffix=".tmp", dir=runtime_directory
    )
    temporary_path = Path(temporary)
    destination = runtime_directory / (
        f"environment-{spec.task.fingerprint[:16]}-{spec.action_id}-"
        f"{spec.generation}.json"
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as carrier:
            carrier.write(encoded)
            carrier.flush()
            os.fsync(carrier.fileno())
        os.replace(temporary_path, destination)
        destination.chmod(0o600)
        return destination
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise


def consume_environment_carrier(
    path: Path,
    spec: ExecutorSpec,
    *,
    now: float | None = None,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Consume a carrier once; mismatched bindings leave it for its owner."""

    path = Path(path)
    try:
        with path.open("rb") as carrier:
            stat = os.fstat(carrier.fileno())
            if stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
                raise EnvironmentCarrierError("Executor 环境载体权限不安全")
            raw = carrier.read(DEFAULT_MAX_ENVIRONMENT_BYTES + 1)
        if len(raw) > DEFAULT_MAX_ENVIRONMENT_BYTES:
            raise EnvironmentCarrierError("Executor 环境载体过大")
        value = json.loads(raw)
    except FileNotFoundError as error:
        raise EnvironmentCarrierError("Executor 环境载体不存在或已经消费") from error
    except EnvironmentCarrierError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EnvironmentCarrierError("Executor 环境载体损坏") from error
    if not isinstance(value, dict) or value.get("version") != _CARRIER_VERSION:
        raise EnvironmentCarrierError("Executor 环境载体版本无效")
    if value.get("binding") != _binding(spec):
        raise EnvironmentCarrierError("Executor 环境载体 binding 不匹配")
    expires_at = value.get("expires_at")
    current = time.time() if now is None else now
    if not isinstance(expires_at, (int, float)) or current > float(expires_at):
        path.unlink(missing_ok=True)
        raise EnvironmentCarrierError("Executor 环境载体已经过期")
    environment = value.get("environment")
    command = value.get("command")
    if not isinstance(environment, dict) or not isinstance(command, list):
        raise EnvironmentCarrierError("Executor 环境载体内容无效")
    if any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in environment.items()
    ) or any(not isinstance(argument, str) for argument in command):
        raise EnvironmentCarrierError("Executor 环境载体内容无效")
    claimed = _claimed_path(path)
    try:
        os.replace(path, claimed)
    except FileNotFoundError as error:
        raise EnvironmentCarrierError(
            "Executor 环境载体不存在或已经消费"
        ) from error
    try:
        return dict(environment), tuple(command)
    finally:
        claimed.unlink(missing_ok=True)


def cleanup_expired_carriers(
    runtime_directory: Path, *, now: float | None = None
) -> None:
    """Best-effort bounded cleanup for abandoned startup carriers."""

    current = time.time() if now is None else now
    try:
        candidates = Path(runtime_directory).glob("environment-*.json")
    except OSError:
        return
    for path in candidates:
        try:
            with path.open("rb") as carrier:
                raw = carrier.read(DEFAULT_MAX_ENVIRONMENT_BYTES + 1)
            if len(raw) > DEFAULT_MAX_ENVIRONMENT_BYTES:
                path.unlink(missing_ok=True)
                continue
            value = json.loads(raw)
            expires_at = value.get("expires_at") if isinstance(value, dict) else None
            if not isinstance(expires_at, (int, float)) or current > float(expires_at):
                path.unlink(missing_ok=True)
        except (OSError, UnicodeError, json.JSONDecodeError):
            path.unlink(missing_ok=True)


def _binding(spec: ExecutorSpec) -> dict[str, object]:
    return {
        "task": spec.task.fingerprint,
        "action_id": spec.action_id,
        "run_id": spec.run_id,
        "generation": spec.generation,
        "state_root": str(spec.state_root) if spec.state_root is not None else None,
    }


def claimed_environment_carrier_path(path: Path) -> Path:
    return _claimed_path(Path(path))


def _claimed_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.claimed{path.suffix}")


def _encode(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
