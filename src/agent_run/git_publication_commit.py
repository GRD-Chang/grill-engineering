"""准确 publication Git 对象的写前记录与写后核验。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import subprocess
from typing import Any, Protocol

from agent_run.git_errors import GitError


class PublicationCommitGit(Protocol):
    def _run_in(
        self, directory: Path, *arguments: str, input: str | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


def install_publication_commit(
    git: PublicationCommitGit,
    checkout: Path,
    *,
    tree: str,
    parents: list[str],
    message: str,
    intent: dict[str, Any],
    save_intent: Callable[[], None],
) -> str:
    """Persist exact bytes/SHA before writing; read back before replay or reset."""

    def read(*arguments: str, input: str | None = None) -> str:
        result = git._run_in(checkout, *arguments, input=input)
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or "could not verify publication commit")
        return result.stdout

    body = message if message.endswith("\n") else message + "\n"
    boundary = [f"tree {tree}", *(f"parent {parent}" for parent in parents)]
    if not intent:
        author = read("var", "GIT_AUTHOR_IDENT").strip()
        committer = read("var", "GIT_COMMITTER_IDENT").strip()
        encoding_result = git._run_in(checkout, "config", "--get", "i18n.commitEncoding")
        if encoding_result.returncode not in {0, 1}:
            raise GitError("could not read publication commit encoding")
        encoding = encoding_result.stdout.strip()
        headers = [*boundary, f"author {author}", f"committer {committer}"]
        if encoding and encoding.lower().replace("-", "") != "utf8":
            headers.append(f"encoding {encoding}")
        raw = "\n".join(headers) + "\n\n" + body
        expected_sha = read("hash-object", "-t", "commit", "--stdin", input=raw).strip()
        intent.update({
            "tree_sha": tree, "parents": parents, "message": message,
            "raw_commit": raw, "expected_sha": expected_sha,
        })
        save_intent()
    raw = intent.get("raw_commit", "")
    expected_sha = intent.get("expected_sha", "")
    if not (
        set(intent) == {"tree_sha", "parents", "message", "raw_commit", "expected_sha"}
        and intent.get("tree_sha") == tree
        and intent.get("parents") == parents
        and intent.get("message") == message
        and isinstance(raw, str)
        and isinstance(expected_sha, str)
    ):
        raise GitError("publication commit intent no longer matches the accepted boundary")
    header, separator, stored_body = raw.partition("\n\n")
    headers = header.splitlines()
    identity_headers = headers[len(boundary):]
    if not (
        separator and stored_body == body and headers[:len(boundary)] == boundary
        and len(identity_headers) in {2, 3}
        and identity_headers[0].startswith("author ")
        and identity_headers[1].startswith("committer ")
        and (len(identity_headers) == 2 or identity_headers[2].startswith("encoding "))
        and read("hash-object", "-t", "commit", "--stdin", input=raw).strip() == expected_sha
    ):
        raise GitError("publication commit intent contains inconsistent object bytes")
    observed = read("cat-file", "--batch-check", input=expected_sha + "\n").strip()
    if observed == f"{expected_sha} missing":
        written = read("hash-object", "-w", "-t", "commit", "--stdin", input=raw).strip()
        if written != expected_sha:
            raise GitError("publication commit write returned an unexpected object identity")
    elif not observed.startswith(f"{expected_sha} commit "):
        raise GitError("publication commit object identity could not be verified")
    if read("cat-file", "commit", expected_sha) != raw:
        raise GitError("publication commit object differs from its persisted intent")
    if read("rev-parse", "HEAD").strip() != expected_sha:
        read("reset", "--hard", expected_sha)
    return expected_sha
