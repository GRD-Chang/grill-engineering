"""Bounded, credential-safe error text for durable audit records."""

from __future__ import annotations

import json
import re

_AUTHORIZATION = re.compile(
    r"(?i)\b(authorization|proxy-authorization)"
    r"([\"']?\s*[:=]\s*[\"']?)(?:(?:bearer|basic)\s+)?"
    r"([^\s,;\"'}]+)"
)
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")
_KNOWN_BARE_TOKEN = re.compile(
    r"\b(?:sk-[a-zA-Z0-9_-]{8,}|gh[pousr]_[a-zA-Z0-9_]{8,})\b"
)
_QUOTED_AUTHORIZATION = re.compile(
    r"(?is)\b(?P<key>authorization|proxy-authorization)"
    r"(?P<key_close>[\"']?)(?P<separator>\s*[:=]\s*)(?:(?:bearer|basic)\s+)?"
    r"(?P<quote>[\"'])(?:\\.|(?!(?P=quote)).)*(?P=quote)"
)
_SECRET = re.compile(
    r"(?i)\b(access[_-]?token|client[_-]?secret|token|api[_-]?key|secret|password|private[ _-]?key)"
    r"([\"']?\s*[:=]\s*[\"']?)(?:(?:bearer|basic)\s+)?"
    r"(?!\[REDACTED\])([^\s,;\"'}]+)"
)
_QUOTED_SECRET = re.compile(
    r"(?is)\b(?P<secret_key>access[_-]?token|client[_-]?secret|token|api[_-]?key|secret|password|private[ _-]?key)\b"
    r"(?P<secret_key_close>[\"']?)(?P<secret_separator>\s*[:=]\s*)"
    r"(?P<secret_quote>[\"'])(?:\\.|(?!(?P=secret_quote)).)*(?P=secret_quote)"
)
_CONTINUED_SECRET = re.compile(
    r"(?is)\b(access[_-]?token|client[_-]?secret|token|api[_-]?key|secret|password|private[ _-]?key)\b[\"']?"
    r"\s*[:=]\s*(?![\"'])(?:[^\r\n]+)(?:\r?\n[^\r\n]*)+"
)
_MULTILINE_PRIVATE_KEY = re.compile(
    r"(?is)\bprivate[ _-]?key\b[\"']?\s*[:=]\s*"
    r"(?:[\"']?-----BEGIN .*?-----END [^-]*-----|[^\r\n]*(?:\r?\n[^\r\n]*)+)"
)
_MAX_ERROR_BYTES = 8 * 1024
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f]")
_JSON_CONTAINER_START = re.compile(r"[\[{]")


def bounded_error(value: str) -> str:
    """Redact common credential forms and retain at most 8 KiB of UTF-8 text."""

    return _truncate(redact_credentials(value))


def redact_credentials(value: str) -> str:
    """Redact common credential forms without changing the caller's size bound."""

    json_value = _redact_json_credentials(value)
    if json_value is not None:
        value = json.dumps(json_value, ensure_ascii=False, separators=(",", ":"))
    else:
        value = _redact_embedded_json_credentials(value)
    return _redact_text(value)


def _redact_text(value: str) -> str:
    # Every labelled credential pattern requires one of these separators.
    # Keep the regexes themselves authoritative, including Unicode IGNORECASE.
    has_separator = ":" in value or "=" in value
    private_key_redacted = (
        _MULTILINE_PRIVATE_KEY.sub("private_key=[REDACTED]", value)
        if has_separator else value
    )
    redacted = _CONTROL_CHARACTERS.sub("", private_key_redacted)
    # Removing control characters can join a previously interrupted scheme.
    if "://" in redacted:
        redacted = _URL_USERINFO.sub(r"\1[REDACTED]@", redacted)
    redacted = _KNOWN_BARE_TOKEN.sub("[REDACTED]", redacted)
    if not has_separator:
        return redacted
    redacted = _QUOTED_AUTHORIZATION.sub(
        r"\g<key>\g<key_close>\g<separator>\g<quote>[REDACTED]\g<quote>",
        redacted,
    )
    redacted = _AUTHORIZATION.sub(r"\1\2[REDACTED]", redacted)
    redacted = _QUOTED_SECRET.sub(
        r"\g<secret_key>\g<secret_key_close>\g<secret_separator>"
        r"\g<secret_quote>[REDACTED]\g<secret_quote>",
        redacted,
    )
    redacted = _CONTINUED_SECRET.sub(r"\1=[REDACTED]", redacted)
    redacted = _SECRET.sub(r"\1\2[REDACTED]", redacted)
    return redacted


def _redact_json_credentials(value: str) -> object | None:
    # Avoid constructing JSONDecodeError for ordinary status/identity strings.
    # Python's JSON decoder also accepts NaN and Infinity by default.
    leading = value.lstrip(" \t\n\r")
    if not leading or leading[0] not in '{["-0123456789tfnNI':
        return None
    try:
        decoded: object = json.loads(value)
    except json.JSONDecodeError:
        return None
    return _redact_json_value(decoded)


def _redact_embedded_json_credentials(value: str) -> str:
    """Normalize every complete JSON object or array embedded in error text."""

    decoder: json.JSONDecoder | None = None
    fragments: list[str] = []
    cursor = 0
    while match := _JSON_CONTAINER_START.search(value, cursor):
        cursor = match.start()
        if decoder is None:
            decoder = json.JSONDecoder()
        try:
            decoded, end = decoder.raw_decode(value, cursor)
        except json.JSONDecodeError:
            cursor += 1
            continue
        fragments.append(value[:cursor])
        fragments.append(
            json.dumps(_redact_json_value(decoded), ensure_ascii=False, separators=(",", ":"))
        )
        value = value[end:]
        cursor = 0
    fragments.append(value)
    return "".join(fragments)


def _redact_json_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_secret_key(key) else _redact_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    return value


def _is_secret_key(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.casefold().replace("_", "").replace("-", "").replace(" ", "")
    return normalized in {
        "authorization",
        "proxyauthorization",
        "token",
        "accesstoken",
        "clientsecret",
        "apikey",
        "secret",
        "password",
        "privatekey",
    }


def _truncate(value: str) -> str:
    return value.encode("utf-8")[:_MAX_ERROR_BYTES].decode(
        "utf-8", errors="ignore"
    )
