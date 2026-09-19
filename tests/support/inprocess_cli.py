from __future__ import annotations

import os
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from agent_run import cli


def invoke_cli_inprocess(
    repo: Path,
    fixture: Path,
    *arguments: str,
    extra_env: dict[str, str] | None = None,
) -> CompletedProcess[str]:
    """Exercise CLI rules without testing interpreter or process boundaries.

    Global state is scoped to this call; use subprocesses for signals, environment
    inheritance, lifecycle execution and cross-directory Locator behavior.
    """

    environment = os.environ.copy()
    environment.update(extra_env or {})
    command_arguments = [*arguments, "--github-fixture", str(fixture)]
    stdout, stderr = StringIO(), StringIO()
    with (
        pytest.MonkeyPatch.context() as scoped,
        patch.dict(os.environ, environment, clear=True),
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        scoped.chdir(repo)
        try:
            returncode = cli.main(command_arguments)
        except SystemExit as error:
            if error.code is None or isinstance(error.code, int):
                returncode = error.code or 0
            else:
                print(error.code, file=stderr)
                returncode = 1
    return CompletedProcess(
        command_arguments, returncode, stdout.getvalue(), stderr.getvalue()
    )
