"""Shared bounded subprocess execution for setup and doctor diagnostics."""
from __future__ import annotations

import subprocess
import time

from agent_run.runtime.process_io import CompleteOutput, pump
from agent_run.runtime.process_cleanup import capture_process_scope, child_subreaper, terminate_process_group


def run_bounded_probe(
    arguments: list[str], *, input_data: bytes | None = None,
    timeout_seconds: float = 3, max_output_bytes: int | None = 4096, merge_stderr: bool = False,
) -> tuple[int | None, bool, bytes]:
    """Bound output, deadline and descendants for one read-only host command."""
    with child_subreaper():
        class ProbeOutput(CompleteOutput):
            def feed(self, chunk: bytes) -> None:
                super().feed(chunk)
                if self.overflow:
                    raise OverflowError("Diagnostic probe output exceeds its limit")

        process: subprocess.Popen[bytes] | None = None
        output = ProbeOutput(max_output_bytes) if max_output_bytes is not None else None
        adopted_baseline = capture_process_scope()
        try:
            process = subprocess.Popen(
                arguments, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE if output is not None else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.monotonic() + timeout_seconds

            def check() -> bool:
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(arguments, timeout_seconds)
                return True

            pump(process, stdout=output, check=check, input_data=input_data)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(arguments, timeout_seconds)
            returncode = process.wait(timeout=remaining)
            return returncode, False, output.finish() if output is not None else b""
        except subprocess.TimeoutExpired:
            return None, True, b""
        except (OSError, subprocess.SubprocessError, OverflowError):
            return None, False, b""
        finally:
            if process is not None:
                try:
                    terminate_process_group(process, adopted_baseline=adopted_baseline)
                finally:
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()
