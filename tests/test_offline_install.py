from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from support.offline_install import offline_pip_environment, prepare_offline_wheels


def test_offline_build_uses_only_verified_pins(tmp_path: Path) -> None:
    source = tmp_path / "cache"
    source.mkdir()
    wheel = source / "example-1.0-py3-none-any.whl"
    wheel.write_bytes(b"pinned build dependency")
    (source / "example-2.0-py3-none-any.whl").write_bytes(b"stale unrelated cache")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "# test pins\nexample==1.0 --hash=sha256:"
        + hashlib.sha256(wheel.read_bytes()).hexdigest() + "\n"
    )
    destination = tmp_path / "local wheels"

    prepare_offline_wheels(source, destination, requirements)

    assert [p.name for p in destination.iterdir()] == [wheel.name]
    assert (destination / wheel.name).read_bytes() == wheel.read_bytes()
    environment = offline_pip_environment(destination)
    assert environment["PIP_NO_INDEX"] == "1"
    assert environment["PIP_FIND_LINKS"] == destination.as_uri()
    assert environment["PIP_CONFIG_FILE"] == "/dev/null"


@pytest.mark.parametrize("contents", [None, b"corrupt wheel"])
def test_offline_build_rejects_missing_or_changed_artifacts(
    tmp_path: Path, contents: bytes | None,
) -> None:
    source = tmp_path / "cache"
    source.mkdir()
    if contents is not None:
        (source / "example-1.0-py3-none-any.whl").write_bytes(contents)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "example==1.0 --hash=sha256:"
        + hashlib.sha256(b"approved").hexdigest() + "\n"
    )
    destination = tmp_path / "wheels"

    with pytest.raises(ValueError, match="run make test-prepare"):
        prepare_offline_wheels(source, destination, requirements)

    assert not list(destination.iterdir())
