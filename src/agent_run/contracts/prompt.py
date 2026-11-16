"""Select and validate paired internal text before freezing it into a Run."""
from __future__ import annotations


from pathlib import Path
import runpy
from string import Formatter

from agent_run.contracts.copy import validate_language
from agent_run.contracts.text_context import TEXTS as CONTEXT
from agent_run.contracts.text_development import TEXTS as DEVELOPMENT
from agent_run.contracts.text_publication import TEXTS as PUBLICATION
from agent_run.contracts.text_review import TEXTS as REVIEW


def _placeholders(text: str) -> list[tuple[str, str, str]]:
    return sorted(
        (field, spec or "", conversion or "")
        for _, field, spec, conversion in Formatter().parse(text)
        if field is not None
    )


def validate_bilingual_texts(texts: dict[str, dict[str, str]]) -> None:
    """Reject missing translations and differing format contracts; never fall back."""
    for key, pair in texts.items():
        if not isinstance(pair, dict) or set(pair) != {"zh", "en"}:
            raise ValueError(f"Internal prompt {key} must provide zh and en")
        if any(not isinstance(value, str) or not value.strip() for value in pair.values()):
            raise ValueError(f"Internal prompt {key} must contain non-empty text")
        if _placeholders(pair["zh"]) != _placeholders(pair["en"]):
            raise ValueError(f"Internal prompt {key} has different bilingual placeholders")


def internal_resources(language: str, package_root: Path | None = None) -> dict[str, str]:
    """Read this runtime's text, or the candidate package used by installer probes."""
    language = validate_language(language)
    groups = (CONTEXT, DEVELOPMENT, REVIEW, PUBLICATION)
    if package_root is not None:
        # These are candidate-owned Python source files, like the candidate runner
        # itself. Keep their paired literals intact instead of copying probe text.
        groups = tuple(
            runpy.run_path(str(package_root / "contracts" / f"text_{name}.py"))["TEXTS"]
            for name in ("context", "development", "review", "publication")
        )
    resources: dict[str, str] = {}
    for group in groups:
        validate_bilingual_texts(group)
        if resources.keys() & group.keys():
            raise ValueError("Duplicate internal prompt identity")
        resources.update({key: pair[language] for key, pair in group.items()})
    return resources


INTERNAL_KEYS = tuple(internal_resources("zh"))


METHOD_NAMES = ("development", "repair", "acceptance", "publishing")


MAX_RESOURCE_BYTES = 512 * 1024


MARKDOWN_KEYS = tuple(f"methods/{name}" for name in METHOD_NAMES)



RESOURCE_KEYS = MARKDOWN_KEYS + INTERNAL_KEYS


from agent_run.errors import error_message

def validate_resources(snapshot: object) -> None:
    if not isinstance(snapshot, dict) or set(snapshot) != set(RESOURCE_KEYS):
        raise ValueError(error_message(
            "prompts.error.identities",
            audit="Incompatible prompt resource snapshot: required resource identities differ",
        ))
    if any(not isinstance(value, str) or not value.strip() for value in snapshot.values()):
        raise ValueError(error_message(
            "prompts.error.content", audit="Invalid prompt resource snapshot: content must be non-empty text"
        ))
    if sum(len(value.encode("utf-8")) for value in snapshot.values()) > MAX_RESOURCE_BYTES:
        raise ValueError(error_message(
            "prompts.error.snapshot_size", audit="Prompt resource snapshot exceeds 512 KiB"
        ))
