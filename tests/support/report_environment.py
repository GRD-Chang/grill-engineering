"""Emit bounded, credential-free diagnostics for local and CI test runs."""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from pathlib import Path


def command(*args: str) -> dict[str, object]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": str(exc)}
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def read_limit(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def package_versions() -> dict[str, str]:
    # Names only: pip freeze can reveal authenticated direct-install URLs.
    names = {"pip"}
    for line in Path("tests/dev-requirements.txt").read_text().splitlines():
        requirement = line.strip().split("==", 1)
        if len(requirement) == 2 and not requirement[0].startswith("#"):
            names.add(requirement[0])
    versions = {}
    for name in sorted(names):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not installed"
    return versions


def memfd_capability() -> dict[str, object]:
    try:
        fd = os.memfd_create("test-report", os.MFD_CLOEXEC)
    except (AttributeError, OSError) as exc:
        return {"available": False, "error": str(exc)}
    os.close(fd)
    return {"available": True}


def main() -> None:
    report = {
        "python": {"version": sys.version, "executable": sys.executable},
        "platform": platform.platform(),
        "os_release": platform.freedesktop_os_release(),
        "cpu_count": os.cpu_count(),
        "cpu_affinity_count": len(os.sched_getaffinity(0)),
        "memory": {
            line.split(":", 1)[0]: line.split(":", 1)[1].strip()
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith(("MemTotal:", "MemAvailable:"))
        },
        "cgroup_cpu_max": read_limit("/sys/fs/cgroup/cpu.max"),
        "cgroup_memory_max": read_limit("/sys/fs/cgroup/memory.max"),
        "git": command("git", "--version"),
        "commit": command("git", "rev-parse", "HEAD"),
        "packages": package_versions(),
        "bubblewrap": command("bwrap", "--version"),
        "memfd": memfd_capability(),
        "pidfd": {"available": hasattr(os, "pidfd_open")},
        "user_namespace": {
            "unprivileged_userns_clone": read_limit(
                "/proc/sys/kernel/unprivileged_userns_clone"
            ),
            "max_user_namespaces": read_limit("/proc/sys/user/max_user_namespaces"),
            "bubblewrap_probe": command(
                "bwrap", "--unshare-user", "--ro-bind", "/", "/", "--", "/usr/bin/true"
            ),
        },
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
