"""Canonical GitHub owner/repository identity, independent of transport spelling."""
import re


def normalize_repository(repository: str) -> str:
    parts = repository.split("/")
    if (len(parts) != 2
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", parts[0])
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", parts[1])
            or parts[1] in {".", ".."}):
        raise ValueError("Expected a GitHub owner/repository identity")
    return repository.lower()


def same_repository(observed: str | None, expected: str) -> bool:
    if observed is None:
        return False
    try:
        return normalize_repository(observed) == normalize_repository(expected)
    except ValueError:
        return False
