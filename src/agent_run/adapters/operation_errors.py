"""Translate physical transport failures at operation boundaries."""
from __future__ import annotations
from collections.abc import Callable
from functools import wraps
import subprocess
from typing import ParamSpec, TypeVar
from agent_run.errors import GitHubReadError, bounded_error
from agent_run.core.operation_contract import ReadUnavailable

_Arguments = ParamSpec("_Arguments")
_Result = TypeVar("_Result")

class EffectReadError(GitHubReadError):
    """A typed read failure after an actual command's response was observed."""

    def __init__(self, error: GitHubReadError, response: subprocess.CompletedProcess[str]) -> None:
        super().__init__(error.code, error.message)
        self.write_returncode = response.returncode
        self.write_diagnostic = response.stderr[:4096]


def operation_errors(method: Callable[_Arguments, _Result]) -> Callable[_Arguments, _Result]:
    """Translate physical transport failures at the Core port boundary only."""
    @wraps(method)
    def invoke(*args: _Arguments.args, **kwargs: _Arguments.kwargs) -> _Result:
        try:
            return method(*args, **kwargs)
        except GitHubReadError as error:
            returncode, diagnostic = None, ""
            if isinstance(error, EffectReadError) and error.write_returncode != 0:
                returncode, diagnostic = error.write_returncode, bounded_error(error.write_diagnostic)
            raise ReadUnavailable(error.code, bounded_error(error.message),
                                  write_returncode=returncode, write_diagnostic=diagnostic) from error
        except OSError as error:
            # An interrupted read/transport supplies no observed write status.
            raise ReadUnavailable("operation_io_unavailable", bounded_error(str(error))) from error
    return invoke
