"""The one-shot Structured Outputs check used by the source installer."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

try:
    from agent_run.process_cleanup import (
        capture_process_scope,
        child_subreaper,
        terminate_process_group,
    )
    from agent_run.runner_runtime import RuntimeTreeError, find_runtime_package
    from agent_run.prompt_resources import read_builtin_resource
except ModuleNotFoundError:  # pragma: no cover - used by the source-tree script
    from process_cleanup import (  # type: ignore[import-not-found, no-redef]
        capture_process_scope,
        child_subreaper,
        terminate_process_group,
    )
    from prompt_resources import read_builtin_resource  # type: ignore[import-not-found, no-redef]
    from runner_runtime import (  # type: ignore[import-not-found, no-redef]
        RuntimeTreeError,
        find_runtime_package,
    )


PROBE_TIMEOUT_SECONDS = 120.0
MAX_FINAL_OUTPUT_BYTES = 16 * 1024

PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"status": {"type": "string", "enum": ["ok"]}},
    "required": ["status"],
}


class RunnerProbeError(RuntimeError):
    """A candidate cannot be activated because its Codex check failed."""


class RunnerProbeBackend:
    """Run a minimal Codex request without Worker or lifecycle dependencies."""

    def __init__(
        self,
        executable: str = "codex",
        *,
        timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
    ) -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            raise RunnerProbeError("Codex executable is not available on PATH")
        self.executable = resolved
        self.timeout_seconds = timeout_seconds

    def check(self, candidate: Path) -> dict[str, str]:
        with child_subreaper():
            return self._check(candidate)

    def _check(self, candidate: Path) -> dict[str, str]:
        package = _require_runtime_package(candidate)
        with tempfile.TemporaryDirectory(prefix="agent-run-probe-") as temporary_name:
            temporary = Path(temporary_name)
            empty_directory = temporary / "empty"
            empty_directory.mkdir()
            schema_path = temporary / "schema.json"
            output_path = temporary / "last-message.json"
            schema_path.write_text(
                json.dumps(PROBE_SCHEMA, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            command = [
                self.executable,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--color",
                "never",
                "--cd",
                str(empty_directory),
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-",
            ]
            try:
                prompt = read_builtin_resource("internal/probe", package)
            except ValueError as error:
                raise RunnerProbeError(str(error)) from error
            process: subprocess.Popen[str] | None = None
            adopted_baseline = capture_process_scope()
            try:
                process = subprocess.Popen(
                    command,
                    cwd=empty_directory,
                    env=os.environ.copy(),
                    text=True,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=(
                        os.environ.get("AGENT_RUN_PROBE_INHERIT_PROCESS_GROUP") != "1"
                    ),
                )
                try:
                    process.communicate(input=prompt, timeout=self.timeout_seconds)
                except subprocess.TimeoutExpired as error:
                    raise RunnerProbeError("Codex Compatibility Check timed out") from error
                if process.returncode != 0:
                    raise RunnerProbeError("Codex Compatibility Check returned a non-zero exit")
                if not output_path.is_file():
                    raise RunnerProbeError("Codex Compatibility Check produced no final output")
                try:
                    output_size = output_path.stat().st_size
                except OSError as error:
                    raise RunnerProbeError(
                        "Codex Compatibility Check produced no final output"
                    ) from error
                if output_size > MAX_FINAL_OUTPUT_BYTES:
                    raise RunnerProbeError("Codex Compatibility Check final output is too large")
                with output_path.open("rb") as output_file:
                    final_output = output_file.read(MAX_FINAL_OUTPUT_BYTES + 1)
                if len(final_output) > MAX_FINAL_OUTPUT_BYTES:
                    raise RunnerProbeError("Codex Compatibility Check final output is too large")
                try:
                    loaded: object = json.loads(final_output.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise RunnerProbeError("Codex Compatibility Check returned invalid JSON") from error
                if loaded != {"status": "ok"}:
                    raise RunnerProbeError("Codex Compatibility Check rejected the required schema")
            except subprocess.TimeoutExpired as error:
                raise RunnerProbeError("Codex Compatibility Check timed out") from error
            except OSError as error:
                raise RunnerProbeError("could not start Codex Compatibility Check") from error
            finally:
                if process is not None:
                    terminate_process_group(
                        process,
                        adopted_baseline=adopted_baseline,
                        same_process_group=(
                            os.environ.get("AGENT_RUN_PROBE_INHERIT_PROCESS_GROUP") == "1"
                        ),
                    )
        return {"result": "passed"}


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent_run.runner_probe",
        description="运行候选 Runner Snapshot 的 Compatibility Check",
    )
    parser.add_argument("candidate", help="候选 Snapshot 或 staging 环境路径")
    parsed = parser.parse_args(list(arguments) if arguments is not None else None)
    try:
        result = RunnerProbeBackend().check(Path(parsed.candidate).resolve())
    except RunnerProbeError as error:
        print(f"runner probe: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _require_runtime_package(candidate: Path) -> Path:
    try:
        return find_runtime_package(candidate)
    except RuntimeTreeError as error:
        raise RunnerProbeError(str(error)) from error


if __name__ == "__main__":
    raise SystemExit(main())
