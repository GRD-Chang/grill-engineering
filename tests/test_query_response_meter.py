"""响应计量与标准 JSON 的字节、失败边界和临时内存保持一致。"""
from __future__ import annotations

import json
import sys
import tracemalloc
from collections.abc import Callable
from typing import Any

import pytest

from agent_run import state_limits as limits


def json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


@pytest.mark.parametrize("state", [
    {},
    {"nested": [None, True, False, {}, [], (), {"pair": (1, "二")}]},
    {"引号\"\\\n": "汉字😀é\x00\b\f\n\r\t\x1f\"\\/\u2028\u2029"},
    {"numbers": [0, -1, 10**100, 0.0, -0.0, 1e-100, 1.7976931348623157e308]},
    {None: "空", False: "假", 17: "整数", -2.5: "小数", "true": "字符串"},
    {"rows": [{"name": f"票据-{index}", "facts": [index, "汉😀\n" * 40]} for index in range(128)]},
    {f"字段-{index}": "\x00😀汉" * 10 for index in range(256)},
])
def test_response_bytes_match_standard_json_at_exact_limit_and_neighbors(state: dict[Any, Any]) -> None:
    expected = json_size(state)
    assert limits.check_response(state, max_bytes=expected) == expected
    assert limits.check_response(state, max_bytes=expected + 1) == expected
    limits.require_response_limit(state, max_bytes=expected)
    limits.require_response_limit(state, max_bytes=expected + 1)
    with pytest.raises(limits.QueryLimitError, match="response exceeds"):
        limits.check_response(state, max_bytes=expected - 1)
    with pytest.raises(limits.QueryLimitError, match="response exceeds"):
        limits.require_response_limit(state, max_bytes=expected - 1)


def test_response_shares_tightest_query_limit_without_consuming_read_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"summary": "中文😀" * 10}
    expected = json_size(state)
    with limits.query_scope(max_bytes=expected + 1) as budget:
        limits.consume_query_bytes(expected)
        assert limits.check_response(state, max_bytes=expected) == expected
        limits.require_response_limit(state, max_bytes=expected)
        assert budget.read_bytes == expected
        with limits.query_scope(max_bytes=expected - 1):
            with pytest.raises(limits.QueryLimitError, match=f"{expected - 1} byte limit"):
                limits.check_response(state, max_bytes=expected + 1)
            with pytest.raises(limits.QueryLimitError, match=f"{expected - 1} byte limit"):
                limits.require_response_limit(state, max_bytes=expected + 1)
        with pytest.raises(limits.QueryLimitError, match="response"):
            limits.check_response(state)
    assert limits.check_response(state, max_bytes=expected) == expected
    monkeypatch.setattr(limits, "MAX_QUERY_BYTES", expected - 1)
    with pytest.raises(limits.QueryLimitError, match="response"):
        limits.check_response(state, max_bytes=expected + 1)
    with pytest.raises(limits.QueryLimitError, match="response"):
        limits.require_response_limit(state, max_bytes=expected + 1)


@pytest.mark.parametrize("validate", [limits.check_response, limits.require_response_limit])
@pytest.mark.parametrize("state", [
    {"invalid": float("nan")},
    {"invalid": [float("inf")]},
    {"invalid": float("-inf")},
    {float("nan"): "invalid key"},
    {float("inf"): "invalid key"},
    {("tuple",): "invalid key"},
    {"invalid": object()},
    {"invalid": b"bytes"},
    {"invalid": "\ud800"},
    {"\udfff": "invalid UTF-8 key"},
])
def test_response_preserves_standard_json_and_utf8_errors(
    state: dict[Any, Any], validate: Callable[..., Any],
) -> None:
    with pytest.raises((ValueError, TypeError, UnicodeEncodeError)) as reference:
        json_size(state)
    with pytest.raises(type(reference.value)):
        validate(state)


@pytest.mark.parametrize("container", ["dict", "list"])
def test_response_rejects_cycles_but_allows_shared_children(container: str) -> None:
    cyclic: Any = {} if container == "dict" else []
    if container == "dict":
        cyclic["self"] = cyclic
    else:
        cyclic.append(cyclic)
    with pytest.raises(ValueError, match="Circular reference"):
        limits.check_response({"cyclic": cyclic})
    with pytest.raises(ValueError, match="Circular reference"):
        limits.require_response_limit({"cyclic": cyclic})
    shared = {"summary": "同一个对象"}
    state = {"first": shared, "second": shared}
    assert limits.check_response(state) == json_size(state)
    limits.require_response_limit(state)


def test_response_keeps_interpreter_integer_digit_limit() -> None:
    digit_limit = sys.get_int_max_str_digits()
    if not digit_limit:
        pytest.skip("Interpreter decimal-digit limit is disabled")
    state = {"number": 10**digit_limit}
    with pytest.raises(ValueError, match="integer string conversion"):
        limits.check_response(state)
    with pytest.raises(ValueError, match="integer string conversion"):
        limits.require_response_limit(state)


def test_response_preserves_dictionary_subclass_item_order_and_duplicate_keys() -> None:
    class RepeatedItems(dict[str, Any]):
        def items(self) -> Any:
            return iter((("same", "第一次"), ("same", "第二次"), (False, 12)))

    state = RepeatedItems(stored=None)
    expected = json_size(state)
    assert limits.check_response(state, max_bytes=expected) == expected
    limits.require_response_limit(state, max_bytes=expected)
    with pytest.raises(limits.QueryLimitError, match="response"):
        limits.check_response(state, max_bytes=expected - 1)
    with pytest.raises(limits.QueryLimitError, match="response"):
        limits.require_response_limit(state, max_bytes=expected - 1)


def test_response_stops_before_visiting_a_tail_after_an_over_limit_prefix() -> None:
    class UnreadTail(dict[str, Any]):
        def items(self) -> Any:
            yield "summary", "汉😀\n" * 8192
            raise AssertionError("Response meter read beyond its exhausted budget")

    with pytest.raises(limits.QueryLimitError, match="response exceeds 64 byte limit"):
        limits.check_response(UnreadTail(stored=None), max_bytes=64)
    with pytest.raises(limits.QueryLimitError, match="response"):
        limits.check_response({"summary": "x" * 1024, "invalid_tail": object()}, max_bytes=64)


def test_response_checks_large_string_keys_before_visiting_values() -> None:
    state = {"汉😀\n" * 8192: object()}
    with pytest.raises(limits.QueryLimitError, match="response"):
        limits.check_response(state, max_bytes=64)


def test_response_admission_rechecks_current_projection_values() -> None:
    from agent_run.work_projection import (
        JobProjection, JobHistoryAppend, RepairBudgetProjection, RepairBudgetScope, ResumeHistory, ResumeScope,
    )

    budget = RepairBudgetProjection({"1": {"used": 2}}, scope=RepairBudgetScope("run", "job", frozenset({"1"})))
    job = JobProjection({"phase": "reviewing", "summary": "汉字😀\n", "repair_integration_budgets": budget},
                        job_id="job", revision=1, archives={})
    state = {
        "active_ticket_job": job,
        "ticket_jobs": {"2": job},
        "history": JobHistoryAppend(["当前"], base_bytes=2, base_count=0),
        "resumes": ResumeHistory([], scope=ResumeScope("run", 0, None, frozenset())),
    }
    limits.require_response_limit(state)
    size = json_size(state)
    limits.require_response_limit(state, max_bytes=size)
    with pytest.raises(limits.QueryLimitError, match="response"):
        limits.require_response_limit(state, max_bytes=size - 1)
    job["summary"] = "\ud800"
    with pytest.raises(UnicodeEncodeError):
        limits.require_response_limit(state)
    job["summary"] = "x" * 1024
    with pytest.raises(limits.QueryLimitError, match="response"):
        limits.require_response_limit(state, max_bytes=64)


def test_response_admission_keeps_custom_iteration_and_ordered_failures() -> None:
    class RepeatedItems(dict[str, Any]):
        def items(self) -> Any:
            yield "same", "first"
            yield "same", "\udfff"
            raise AssertionError("Advanced after invalid UTF-8")

    with pytest.raises(UnicodeEncodeError):
        limits.require_response_limit(RepeatedItems(stored=None))
    with pytest.raises(limits.QueryLimitError, match="response"):
        limits.require_response_limit({"prefix": "x" * 1024, "tail": object()}, max_bytes=64)


def test_response_admission_fallback_keeps_large_input_memory_bounded() -> None:
    state = {"summary": "汉😀\n" * (1024 * 1024)}
    tracemalloc.start()
    try:
        with pytest.raises(limits.QueryLimitError, match="response"):
            limits.require_response_limit(state, max_bytes=1024)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 128 * 1024


@pytest.mark.parametrize("invalid, error", [
    (float("nan"), ValueError),
    (object(), TypeError),
    ("\ud800", UnicodeEncodeError),
])
def test_response_reports_invalid_scalar_before_advancing_custom_iterator(
    invalid: Any, error: type[Exception],
) -> None:
    class UnreadTail(dict[str, Any]):
        def items(self) -> Any:
            yield "prefix", ["", "汉😀", 17, -0.0, None, True]
            yield "invalid", invalid
            raise AssertionError("Response meter advanced past an invalid scalar")

    with pytest.raises(error):
        limits.check_response(UnreadTail(stored=None))


def test_response_rejects_a_dense_scalar_prefix_before_visiting_an_invalid_tail() -> None:
    prefix = ["", "汉😀", 17, -0.0, None, True] * 512
    state = {"prefix": prefix, "invalid_tail": object()}
    with pytest.raises(limits.QueryLimitError, match="response"):
        limits.check_response(state, max_bytes=json_size({"prefix": prefix}) - 1)


def test_response_checks_string_opening_quote_before_an_invalid_utf8_body() -> None:
    prefix = len(b'{"invalid":')
    with pytest.raises(limits.QueryLimitError, match="response"):
        limits.check_response({"invalid": "\ud800"}, max_bytes=prefix)
    with pytest.raises(UnicodeEncodeError):
        limits.check_response({"invalid": "\ud800"}, max_bytes=prefix + 1)


def test_response_native_blocks_and_peak_memory_stay_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {
        "rows": [{"ordinal": index, "value": "\x00😀汉" * 32} for index in range(4096)],
        "body": "\x00😀汉" * 65_536,
        "scalars": (["😀"] + [""] * 3000 + [None, True, 17, -0.0]) * 4,
    }
    expected = json_size(state)
    original_encode = json.JSONEncoder.encode
    largest = 0

    def checked_encode(encoder: json.JSONEncoder, value: Any) -> str:
        nonlocal largest
        encoded = original_encode(encoder, value)
        largest = max(largest, len(encoded.encode("utf-8")))
        assert largest <= 16 * 1024
        return encoded

    monkeypatch.setattr(json.JSONEncoder, "encode", checked_encode)
    tracemalloc.start()
    try:
        assert limits.check_response(state, max_bytes=expected) == expected
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 128 * 1024
    assert largest > 0


def test_response_rejects_large_string_without_allocating_its_json_copy() -> None:
    state = {"summary": "\x00😀汉" * 262_144}
    tracemalloc.start()
    try:
        with pytest.raises(limits.QueryLimitError, match="response"):
            limits.check_response(state, max_bytes=64)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 128 * 1024
