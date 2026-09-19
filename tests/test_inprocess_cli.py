from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_run import cli
from support.inprocess_cli import invoke_cli_inprocess


def test_inprocess_cli_keeps_parser_errors_and_restores_the_environment(
    tmp_path: Path,
) -> None:
    cwd, environment = Path.cwd(), os.environ.copy()
    stdout, stderr = sys.stdout, sys.stderr

    result = invoke_cli_inprocess(
        tmp_path,
        tmp_path / "unused.json",
        "status",
        "--unknown-option",
        extra_env={"CLI_TEST_OVERRIDE": "scoped"},
    )

    assert result.returncode == 2
    assert "无法识别的参数: --unknown-option" in result.stderr
    assert Path.cwd() == cwd
    assert dict(os.environ) == environment
    assert sys.stdout is stdout
    assert sys.stderr is stderr


def test_inprocess_cli_restores_mutated_globals_and_propagates_unexpected_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd, environment = Path.cwd(), os.environ.copy()
    stdout, stderr = sys.stdout, sys.stderr
    changed_directory = tmp_path / "changed-directory"
    changed_directory.mkdir()

    def failing_main(arguments: Sequence[str] | None = None) -> int:
        assert Path.cwd() == tmp_path
        assert os.environ["CLI_TEST_OVERRIDE"] == "scoped"
        os.environ.clear()
        os.environ["CLI_TEST_LEAK"] = "must be removed"
        os.chdir(changed_directory)
        print("captured output")
        print("captured error", file=sys.stderr)
        raise RuntimeError("unexpected command failure")

    monkeypatch.setattr(cli, "main", failing_main)
    with pytest.raises(RuntimeError, match="unexpected command failure"):
        invoke_cli_inprocess(
            tmp_path,
            tmp_path / "unused.json",
            "status",
            extra_env={"CLI_TEST_OVERRIDE": "scoped"},
        )

    assert Path.cwd() == cwd
    assert dict(os.environ) == environment
    assert sys.stdout is stdout
    assert sys.stderr is stderr
