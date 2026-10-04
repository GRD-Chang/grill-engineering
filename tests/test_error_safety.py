from __future__ import annotations

import pytest

from agent_run.error_safety import bounded_error, redact_credentials


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ticket-reviewer-1", "ticket-reviewer-1"),
        ("failed", "failed"),
        ("0123456789abcdef", "0123456789abcdef"),
        ("中文ſecretKey", "中文ſecretKey"),
        ("01", "01"),
        ("1e2", "1e2"),
        ('false {"token":"credential"}', 'false {"token":"[REDACTED]"}'),
        ('123 {"password":"credential"}', '123 {"password":"[REDACTED]"}'),
        ("Infinity token=credential", "Infinity token=[REDACTED]"),
        ("中文状态 — completed", "中文状态 — completed"),
        ("ghp_1234567890abcdef", "[REDACTED]"),
        ("sk-1234567890abcdef", "[REDACTED]"),
        ("gh\x00p_1234567890abcdef", "[REDACTED]"),
        ("sk\x00-1234567890abcdef", "[REDACTED]"),
        ("token=credential", "token=[REDACTED]"),
        ("to\x00ken=credential", "token=[REDACTED]"),
        ("client_secret=credential", "client_secret=[REDACTED]"),
        ("ſecret=credential", "ſecret=[REDACTED]"),
        ('api_Key="credential words"', 'api_Key="[REDACTED]"'),
        ("authorızation: Bearer credential", "authorızation: [REDACTED]"),
        ("authorİzation: credential", "authorİzation: [REDACTED]"),
        (
            "proxy-authorization: Basic credential",
            "proxy-authorization: [REDACTED]",
        ),
        (
            "private_key=-----begin test key-----body-----end test key-----",
            "private_key=[REDACTED]",
        ),
        ("prıvate_Key=first\nsecond", "private_key=[REDACTED]"),
        ("private\x00_key=first\nsecond", "private_key=[REDACTED]"),
        (
            "https:\x00//user:credential@example.invalid",
            "https://[REDACTED]@example.invalid",
        ),
        ("Kttps://user:credential@host", "Kttps://[REDACTED]@host"),
        (
            '{"to\\u006ben": "credential", "ok": true}',
            '{"token":"[REDACTED]","ok":true}',
        ),
        (
            'prefix [{"password":"credential"}] suffix',
            'prefix [{"password":"[REDACTED]"}] suffix',
        ),
        (
            'invalid { then {"token":"credential"}',
            'invalid { then {"token":"[REDACTED]"}',
        ),
        (
            'first {"token":"one"} then {"secret":"two"}',
            'first {"token":"[REDACTED]"} then {"secret":"[REDACTED]"}',
        ),
        (" \t42\r\n", " \t42\r\n"),
        (" 1e2 ", " 1e2 "),
        (" true ", " true "),
        (" false ", " false "),
        (" null ", " null "),
        (" NaN ", " NaN "),
        (" Infinity ", " Infinity "),
        (" -Infinity ", " -Infinity "),
    ],
)
def test_credential_redaction_preserves_text_and_json_semantics(
    value: str, expected: str,
) -> None:
    assert redact_credentials(value) == expected


def test_control_filter_preserves_tabs_newlines_and_non_ascii_text() -> None:
    controls = "".join(chr(code) for code in range(32))
    assert redact_credentials(f"before{controls}\x7f中文after") == "before\t\n\x7f中文after"


def test_error_bound_applies_after_redacting_embedded_credentials() -> None:
    value = 'prefix {"password":"credential"} ' + "中文" * 5000
    result = bounded_error(value)
    assert result.startswith('prefix {"password":"[REDACTED]"} ')
    assert "credential" not in result
    assert len(result.encode("utf-8")) <= 8192


@pytest.mark.parametrize("prefix", ["", "payload "])
@pytest.mark.parametrize(("value", "expected"), [
    ('{"x":"\\u0067hp_123456789abcdef", "x":"safe"}', '{"x":"safe"}'),
    ('{"x":{"password":"private-value"}, "x":"safe"}', '{"x":"safe"}'),
    ('{"nested":{"x":"\\u0067hp_123456789abcdef", "x":"safe"}}',
     '{"nested":{"x":"safe"}}'),
])
def test_duplicate_json_keys_do_not_hide_escaped_credentials(
    prefix: str, value: str, expected: str,
) -> None:
    assert redact_credentials(prefix + value) == prefix + expected


def test_embedded_json_redaction_keeps_adjacent_harmless_fragments() -> None:
    value = '{"x":"\\u0067hp_123456789abcdef", "x":"safe"} then { "x": 1, "x": 2 } - [ ]'
    assert redact_credentials(value) == '{"x":"safe"} then { "x": 1, "x": 2 } - [ ]'
