"""The shared fake gh must answer the requested PR and commit, never its first PR."""
from __future__ import annotations

import sys

import pytest

from support.github_checks import CHECK_ROLLUP_FAKE, LATEST_CHECK_FAKE


HEAD_A = "a" * 40
HEAD_B = "b" * 40


def _responses():
    namespace = {"sys": sys}
    exec(CHECK_ROLLUP_FAKE + LATEST_CHECK_FAKE, namespace)
    return namespace["current_checks"], namespace["latest_check_response"]


def _data(*, reverse=False):
    prs = [{"number": 5, "head": {"sha": HEAD_A}}, {"number": 93, "head": {"sha": HEAD_B}}]
    return {"prs": list(reversed(prs)) if reverse else prs,
            "checks": [{"name": "quality", "bucket": "pass"}]}


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(("number", "head"), [(5, HEAD_A), (93, HEAD_B)])
def test_rollup_selects_the_requested_pr_independently_of_list_order(reverse, number, head):
    current, _ = _responses()
    pr = current(_data(reverse=reverse), {"number": str(number)})["data"]["repository"]["pullRequest"]
    assert pr["number"] == number
    assert pr["headRefOid"] == head
    commit = pr["commits"]["nodes"][0]["commit"]
    assert commit["oid"] == head
    assert commit["statusCheckRollup"]["contexts"]["nodes"][0]["checkSuite"]["commit"]["oid"] == head


@pytest.mark.parametrize("singleton", [False, True])
def test_unknown_pr_does_not_borrow_another_prs_checks(singleton):
    current, _ = _responses()
    data = _data()
    if singleton:
        data["pull_request"] = data.pop("prs")[0]
    assert current(data, {"number": "200"}) == {"data": {"repository": {"pullRequest": None}}}


def test_duplicate_pr_numbers_are_rejected_instead_of_selecting_an_arbitrary_head():
    current, _ = _responses()
    data = _data()
    data["prs"][1]["number"] = 5
    with pytest.raises(ValueError, match="Duplicate fixture PR number"):
        current(data, {"number": "5"})


@pytest.mark.parametrize("endpoint", ["check-runs", "check-suites"])
@pytest.mark.parametrize("head", [HEAD_A, HEAD_B, "c" * 40])
def test_commit_checks_select_the_requested_commit(endpoint, head):
    _, latest = _responses()
    response = latest(_data(), f"repos/example/project/commits/{head}/{endpoint}?filter=latest&app_id=1001")
    rows = response[endpoint.replace("-", "_")]
    assert response["total_count"] == (0 if head == "c" * 40 else 1)
    assert [row["head_sha"] for row in rows] == ([] if head == "c" * 40 else [head])


def test_two_prs_at_one_commit_do_not_duplicate_commit_checks():
    _, latest = _responses()
    data = _data()
    data["prs"][1]["head"]["sha"] = HEAD_A
    response = latest(data, f"repos/example/project/commits/{HEAD_A}/check-runs?filter=latest&app_id=1001")
    assert response["total_count"] == 1
    assert [row["head_sha"] for row in response["check_runs"]] == [HEAD_A]
