"""Subprocess driver for installer lifecycle tests.

The production installer build seam is intentionally replaced here. Tests still
exercise the real installer transaction, candidate probe, activation, rollback,
cleanup, locking, and signal handling without creating a pip-enabled venv and
building the package for every state-machine case.
"""

from __future__ import annotations

import shutil
import sys
import venv
from pathlib import Path


sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parent
PACKAGE_SOURCE = SOURCE / "src"
if not PACKAGE_SOURCE.is_dir():
    PACKAGE_SOURCE = SOURCE.parents[1] / "src"
sys.path.insert(0, str(PACKAGE_SOURCE))

from agent_run import runner_installer  # noqa: E402


def build_candidate(candidate: Path, source: Path) -> tuple[str, str]:
    if (source / ".agent-run-test-build-failure").exists():
        raise runner_installer.InstallerError(
            "Python package build or non-editable installation failed"
        )

    venv.EnvBuilder(with_pip=False, clear=True, symlinks=True).create(candidate)
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    site_packages = candidate / "lib" / f"python{python_version}" / "site-packages"
    package = site_packages / "agent_run"
    site_packages.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source / "src" / "agent_run",
        package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )

    entry = candidate / "bin" / "agent-run"
    entry.write_text(
        f"#!{candidate / 'bin' / 'python'}\n"
        "from agent_run.cli import main\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )
    entry.chmod(0o755)
    return runner_installer._runtime_identity(package), python_version


if __name__ == "__main__":
    runner_installer._build_candidate = build_candidate
    raise SystemExit(runner_installer.main())
