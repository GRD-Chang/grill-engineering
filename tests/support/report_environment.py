"""Emit bounded, credential-free diagnostics for local and CI test runs."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path


def ci_identity() -> dict[str, str]:
    return {key: os.environ.get(key, '') for key in (
        'GITHUB_REPOSITORY', 'GITHUB_SHA', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_WORKFLOW_REF')}


def command(*args: str) -> dict[str, object]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except TimeoutError:
        raise
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
    except TimeoutError:
        raise
    except (AttributeError, OSError) as exc:
        return {"available": False, "error": str(exc)}
    os.close(fd)
    return {"available": True}


def pidfd_capability() -> dict[str, object]:
    try:
        fd = os.pidfd_open(os.getpid())
    except TimeoutError:
        raise
    except (AttributeError, OSError) as exc:
        return {"available": False, "error": str(exc)}
    os.close(fd)
    return {"available": True}


def save_clock_capability() -> dict[str, object]:
    """Use the real producer; failure means native verification is unavailable."""
    from agent_run.errors import KernelError
    from agent_run.storage.save_order import SaveClock
    try:
        clock = SaveClock()
        try:
            clock.require()
        except BaseException as primary:
            try:
                clock.close()
            except BaseException as cleanup:
                if isinstance(cleanup, TimeoutError) and not isinstance(primary, TimeoutError):
                    raise
                raise primary from cleanup
            raise
        else:
            clock.close()
    except TimeoutError:
        raise  # An execution watchdog remains a control failure.
    except (OSError, ValueError, KernelError) as error:
        return {"available": False, "error_type": type(error).__name__,
                "code": error.code if isinstance(error, KernelError) else getattr(error, 'errno', None)}
    return {"available": True}


def native_capabilities() -> dict[str, object]:
    """A capability gate, never evidence of completed business/native tests."""
    bubblewrap = command('bwrap', '--unshare-user', '--ro-bind', '/', '/', '--', '/usr/bin/true')
    checks = {"memfd": memfd_capability(), "pidfd": pidfd_capability(),
              "save_clock": save_clock_capability(),
              "user_namespace": {"available": bubblewrap.get('returncode') == 0,
                                 "returncode": bubblewrap.get('returncode')}}
    return {"stage": "native-capabilities", "commit": command('git', 'rev-parse', 'HEAD'),
            "ci_identity": ci_identity(),
            "status": "supported" if all(value['available'] for value in checks.values()) else "unsupported",
            "complete_product_pass": False, "checks": checks}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-native', action='store_true')
    args = parser.parse_args()
    if args.require_native:
        capabilities = native_capabilities()
        print(json.dumps(capabilities, indent=2))
        if capabilities['status'] != 'supported':
            print('Native capability unsupported or unproven; native tests remain UNRUN.', file=sys.stderr)
            raise SystemExit(2)
        return
    report = {
        "ci_identity": ci_identity(),
        "python": {"version": sys.version, "executable": sys.executable},
        "sqlite": {"version": sqlite3.sqlite_version},
        "platform": platform.platform(),
        "os_release": platform.freedesktop_os_release(),
        "cpu_count": os.cpu_count(),
        "cpu_affinity_count": len(os.sched_getaffinity(0)),
        "load_average": os.getloadavg(),
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
        "pidfd": pidfd_capability(),
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
