"""Detailed History retains real global facts before the first Attempt."""

from support.repository_fixture import create_kernel
from dataclasses import replace
from io import StringIO
import json

import pytest

from agent_run.cli.render import render
from agent_run.core.kernel import Kernel
from agent_run.model import DeliveryKind, ParentIssue, Requirements
from agent_run.queries.run import read
from support.offline import OfflinePort


def test_history_details_keep_failure_before_any_attempt_and_original_stop(tmp_path):
    port = OfflinePort.create(tmp_path / "physical")
    spec, _ = port.prepare(DeliveryKind.PARENT_ONLY)
    kernel = create_kernel(tmp_path / "state" / "kernel.sqlite3", spec,
                               requirements=Requirements(ParentIssue(1, "Parent", "Scope"), ()))
    execution = kernel.claim_executor("original-history-owner")
    original = OSError("ORIGINAL-GLOBAL-FAILURE before first Attempt")
    with pytest.raises(OSError) as raised:
        with kernel.executor_scope(execution) as close_admission:
            close_admission(original)
            raise original
    assert raised.value is original
    assert kernel.control("stop", "original-stop") == "applied"
    document = read(kernel.storage.path, 'history', storage=kernel.storage)
    assert document.data["history"]["attempts"] == []
    failed, = [event for event in document.data["history"]["events"] if event["kind"] == "executor_failed"]
    assert failed["evidence"]["message"] == str(original)
    original_json = json.dumps(document.data, ensure_ascii=False, sort_keys=True)
    for language in ("zh", "en"):
        for style in ("plain", "rich"):
            output = StringIO()
            render(replace(document, language=language), style=style, details=True, stream=output)
            assert str(original) in output.getvalue()
            assert "OSError" in output.getvalue() and "original-stop" in output.getvalue()
    page = read(kernel.storage.path, 'history', after=failed['position'] - 1, limit=1, storage=kernel.storage)
    assert page.data["history"]["attempts"] == []
    output = StringIO()
    render(page, style="plain", details=True, stream=output)
    assert str(original) in output.getvalue()
    assert json.dumps(document.data, ensure_ascii=False, sort_keys=True) == original_json
