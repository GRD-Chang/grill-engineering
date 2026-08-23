"""Shared validation for an installed Runner runtime tree."""

from __future__ import annotations

from pathlib import Path


class RuntimeTreeError(ValueError):
    """The candidate does not contain one isolated runtime tree."""


def find_runtime_package(candidate: Path) -> Path:
    """Return the only in-candidate ``agent_run`` package."""

    matches = sorted(
        path
        for path in candidate.glob("lib*/python*/site-packages/agent_run")
        if path.is_dir() and not path.is_symlink()
    )
    unique: list[Path] = []
    resolved: set[Path] = set()
    for match in matches:
        real = match.resolve()
        if real not in resolved:
            resolved.add(real)
            unique.append(match)
    if len(unique) != 1:
        raise RuntimeTreeError("candidate Runner has an invalid runtime tree")

    package = unique[0]
    try:
        package.resolve().relative_to(candidate.resolve())
    except ValueError as error:
        raise RuntimeTreeError("candidate Runner runtime escapes its Snapshot") from error
    for path in package.rglob("*"):
        if path.is_symlink():
            try:
                path.resolve().relative_to(candidate.resolve())
            except ValueError as error:
                raise RuntimeTreeError(
                    "candidate Runner runtime contains a symlink outside its Snapshot"
                ) from error
    return package
