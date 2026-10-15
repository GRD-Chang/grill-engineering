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
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]|\r(?!\n)")
_JSON_CONTAINER_START = re.compile(r"[\[{]")
_NON_JSON_NUMBER = re.compile(r"[^0-9eE+.\- \t\r\n]")
# Every labelled pattern contains one of these words; matching remains below.
_CREDENTIAL_LABEL = re.compile(
    r"(?i)authorization|token|secret|key|password"
)


def bounded_error(value: str) -> str:
    """Redact common credential forms and retain at most 8 KiB of UTF-8 text."""

    return _truncate(redact_credentials(value))


def redact_credentials(value: str) -> str:
    """Redact common credential forms without changing the caller's size bound."""

    json_value = _redact_json_credentials(value)
    if json_value is not None:
        value = json_value
    else:
        value = _redact_embedded_json_credentials(value)
    return _redact_text(value)


def _redact_text(value: str) -> str:
    # Credential forms and control characters all require non-alphanumeric text.
    # Decoded JSON is inspected before this textual pass.
    if value.isalnum():
        return value
    # Every labelled credential pattern requires one of these separators.
    # Keep the regexes themselves authoritative, including Unicode IGNORECASE.
    has_separator = ":" in value or "=" in value
    # The private-key branches require a line break or a PEM-style marker.
    private_key_redacted = (
        _MULTILINE_PRIVATE_KEY.sub("private_key=[REDACTED]", value)
        if has_separator and ("\n" in value or "-----" in value) else value
    )
    redacted = _CONTROL_CHARACTERS.sub("", private_key_redacted)
    # Removing control characters can join a previously interrupted scheme.
    if "://" in redacted:
        redacted = _URL_USERINFO.sub(r"\1[REDACTED]@", redacted)
    redacted = _KNOWN_BARE_TOKEN.sub("[REDACTED]", redacted)
    # Match after control removal, which can also join a credential label.
    if not has_separator or not _CREDENTIAL_LABEL.search(redacted):
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


class _CredentialJSONDecoder(json.JSONDecoder):
    """Detect shadowed values that must not survive credential inspection."""

    def __init__(self) -> None:
        self.hidden_credentials = False
        super().__init__(object_pairs_hook=self._object)

    def _object(self, pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result and _redact_decoded_json({key: result[key]}) is not None:
                self.hidden_credentials = True
            result[key] = item
        return result


def _redact_json_credentials(value: str) -> str | None:
    # Avoid constructing JSONDecodeError for ordinary status/identity strings.
    # Python's JSON decoder also accepts NaN and Infinity by default.
    leading = value.lstrip(" \t\n\r")
    if not leading or leading[0] not in '{["-0123456789tfnNI':
        return None
    if leading[0] in "tfnNI":
        if leading.rstrip(" \t\n\r") not in {"true", "false", "null", "NaN", "Infinity"}:
            return None
    elif leading[0] in "-0123456789":
        if leading.rstrip(" \t\n\r") != "-Infinity" and _NON_JSON_NUMBER.search(leading):
            return None
    try:
        decoder = _CredentialJSONDecoder()
        decoded: object = decoder.decode(value)
    except json.JSONDecodeError:
        return None
    return _redact_json_fragment(
        value, decoded, hidden_credentials=decoder.hidden_credentials,
    )


def _redact_embedded_json_credentials(value: str) -> str:
    """Inspect embedded JSON without reformatting credential-free fragments."""

    decoder: _CredentialJSONDecoder | None = None
    fragments: list[str] = []
    cursor = 0
    while match := _JSON_CONTAINER_START.search(value, cursor):
        cursor = match.start()
        if decoder is None:
            decoder = _CredentialJSONDecoder()
        decoder.hidden_credentials = False
        try:
            decoded, end = decoder.raw_decode(value, cursor)
        except json.JSONDecodeError:
            cursor += 1
            continue
        fragments.append(value[:cursor])
        fragments.append(
            _redact_json_fragment(
                value[cursor:end], decoded, hidden_credentials=decoder.hidden_credentials,
            )
        )
        value = value[end:]
        cursor = 0
    fragments.append(value)
    return "".join(fragments)


def _redact_json_fragment(
    original: str, decoded: object, *, hidden_credentials: bool = False,
) -> str:
    sanitized = _redact_decoded_json(decoded)
    if sanitized is not None:
        return sanitized
    if hidden_credentials:
        # A duplicate key discarded a secret-bearing value while decoding.
        return json.dumps(decoded, ensure_ascii=False, separators=(",", ":"))
    return original


def _redact_decoded_json(decoded: object) -> str | None:
    # Inspect decoded text too: escaped labels and bare tokens must not evade
    # redaction. JSON reserialization is necessary only when content changes.
    normalized = json.dumps(decoded, ensure_ascii=False, separators=(",", ":"))
    sanitized = _redact_text(json.dumps(
        _redact_json_value(decoded), ensure_ascii=False, separators=(",", ":"),
    ))
    return sanitized if sanitized != normalized else None


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
