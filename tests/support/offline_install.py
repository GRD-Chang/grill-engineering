"""Use verified local build wheels without changing the real installer."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path


def prepare_offline_wheels(source: Path, destination: Path, requirements: Path) -> None:
    """Copy only pinned universal wheels; stale cache entries cannot affect pip."""
    destination.mkdir(parents=True)
    for line in requirements.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        requirement, digest_option = line.split()
        name, version = requirement.split("==")
        wheel = source / f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
        if not wheel.is_file():
            raise ValueError(f"Missing test build wheel {wheel.name}; run make test-prepare")
        expected = digest_option.removeprefix("--hash=sha256:")
        actual = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"Invalid test build wheel {wheel.name}; run make test-prepare")
        shutil.copy2(wheel, destination / wheel.name)


def offline_pip_environment(wheelhouse: Path) -> dict[str, str]:
    return {
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
        "PIP_FIND_LINKS": wheelhouse.resolve().as_uri(),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
