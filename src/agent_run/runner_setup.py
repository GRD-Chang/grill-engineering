"""Bounded probes for the shell bootstrap, using the existing doctor boundary."""
from __future__ import annotations

import json
from pathlib import Path
import signal
import shutil
import sys
import uuid

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_run.doctor import _run_bounded_output_probe
from agent_run.process_cleanup import child_subreaper


def _probe(arguments: list[str], *, timeout: float = 5) -> tuple[int | None, bytes]:
    if shutil.which(arguments[0]) is None:
        return 127, f"未找到探针工具：{arguments[0]}\n".encode()
    code, timed_out, output = _run_bounded_output_probe(
        arguments, input_data=b"", max_output_bytes=32 * 1024,
        timeout_seconds=timeout, merge_stderr=True,
    )
    if timed_out:
        return 124, b"probe timed out\n"
    return code, output


def _interrupted(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def _finish() -> int:
    unit = f"agent-run-setup-{uuid.uuid4().hex}.service"
    probe_ready = False
    try:
        code, output = _probe([
            "systemd-run", "--user", "--wait", "--collect", f"--unit={unit}",
            "--property=Type=exec", "--property=RuntimeMaxSec=10", "/bin/true",
        ], timeout=15)
        probe_ready = code == 0
        if not probe_ready:
            print("user systemd transient unit 不可用：" + output.decode("utf-8", "replace"), file=sys.stderr)
    finally:
        if not probe_ready:
            for operation in ("stop", "reset-failed"):
                _probe(["systemctl", "--user", operation, unit])
    entry = Path.home() / ".local/bin/agent-run"
    code, output = _probe([str(entry), "doctor", "--json"], timeout=90)
    text = output.decode("utf-8", "replace")
    print(text, end="")
    try:
        report = json.loads(text)
        ready = probe_ready and code == 0 and isinstance(report, dict) and report.get("status") == "ready"
    except ValueError:
        ready = False
    if ready:
        print("结果：安装及执行条件均满足。")
    else:
        print("结果：Runner 已安装但执行环境未就绪；按 doctor 原因处理，重新打开登录 shell 后运行 agent-run doctor，或重跑 sh setup.sh。")
    return 0 if ready else 2


def main(arguments: list[str] | None = None) -> int:
    args = sys.argv[1:] if arguments is None else arguments
    previous = signal.signal(signal.SIGTERM, _interrupted)
    try:
        with child_subreaper():
            if args:
                code, output = _probe(args)
                sys.stdout.buffer.write(output)
                return code if code is not None and code >= 0 else 1
            return _finish()
    except KeyboardInterrupt:
        print("Setup 检查已中断；本次探针已清理，请重跑 setup.sh。", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
