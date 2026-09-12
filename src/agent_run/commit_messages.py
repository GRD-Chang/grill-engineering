from __future__ import annotations


def commit_messages_match(actual: object, expected: object) -> bool:
    """Compare all content, allowing Git to terminate an unterminated message.

    Explicit terminal newlines in the publication text remain part of its
    content. No body whitespace, paragraph or extra blank line is discarded.
    """
    if not isinstance(actual, str) or not isinstance(expected, str) or not expected:
        return False
    return actual == expected or (
        not expected.endswith("\n") and actual == expected + "\n"
    )
