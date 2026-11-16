"""Raw synthetic API inputs and recorded calls; no Core decisions or results."""
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
import subprocess
from typing import Any

import pytest

from agent_run.errors import GitHubReadError
from support.offline_api import ExternalObjects, SyntheticGitHub


def pr_author_read(monkeypatch: pytest.MonkeyPatch, database: Path,
                   number: int, author: str | None) -> Callable[[], None]:
    read = SyntheticGitHub._pr

    def observed(api: SyntheticGitHub, requested: int) -> dict[str, Any]:
        data = read(api, requested)
        if api.database == database and requested == number:
            return {**data, "user": {} if author is None else {"login": author}}
        return data

    monkeypatch.setattr(SyntheticGitHub, "_pr", observed)
    return lambda: monkeypatch.setattr(SyntheticGitHub, "_pr", read)


def lose_api_reply(monkeypatch: pytest.MonkeyPatch, database: Path,
                   method: str, endpoint: str) -> None:
    complete = SyntheticGitHub._complete
    lost = False

    def response(api: SyntheticGitHub, path: str, verb: str,
                 body: Mapping[str, Any], result: Mapping[str, Any],
                 **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal lost
        actual = complete(api, path, verb, body, result, **kwargs)
        if api.database == database and (verb, path) == (method, endpoint) and not lost:
            lost = True
            raise GitHubReadError("write_response_unavailable", f"{method} {endpoint} response lost")
        return actual

    monkeypatch.setattr(SyntheticGitHub, "_complete", response)


def api_writes(objects: ExternalObjects, endpoints: Collection[str]) -> list[tuple[str, str]]:
    with objects._connect() as db:
        return [(method, endpoint) for method, endpoint in db.execute(
            "SELECT method,endpoint FROM api_requests WHERE method!='GET' ORDER BY position")
            if endpoint in endpoints]
