"""将完整收集结果分成互斥且均衡的 CI 分片，默认仍运行全部用例。"""
from __future__ import annotations

import pytest
from _pytest.junitxml import LogXML, xml_key
from _pytest.subtests import SubtestReport


PASSED_SUBTESTS_PROPERTY = "agent-run-pytest-passed-subtests-v1"


class _JUnitSubtests:
    def __init__(self, xml: LogXML) -> None:
        self.xml = xml
        self.passed = 0

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if isinstance(report, SubtestReport) and report.when == "call" and report.passed:
            self.passed += 1

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionfinish(self) -> None:
        # pytest 9 把成功 subtest 计入 tests，但复用父 testcase；不改写原生计数。
        self.xml.add_global_property(PASSED_SUBTESTS_PROPERTY, self.passed)


def select_shard(nodeids: list[str], count: int, index: int) -> set[str]:
    if count < 1 or not 0 <= index < count:
        raise ValueError("Test shard requires count >= 1 and 0 <= index < count")
    if len(set(nodeids)) != len(nodeids):
        raise ValueError("Test sharding requires unique collected node IDs")
    selected = set(sorted(nodeids)[index::count])
    if not selected:
        raise ValueError("Test shard has no collected tests")
    return selected


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("CI sharding")
    group.addoption("--test-shard-count", type=int, default=1)
    group.addoption("--test-shard-index", type=int, default=0)


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    count, index = config.getoption("test_shard_count"), config.getoption("test_shard_index")
    if count < 1 or not 0 <= index < count:
        raise pytest.UsageError("Test shard requires count >= 1 and 0 <= index < count")
    xml = config.stash.get(xml_key, None)
    if xml is not None:
        # 只在 JUnit controller 上登记；xdist worker 的报告由 controller 汇总。
        config.pluginmanager.register(_JUnitSubtests(xml))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    count, index = config.getoption("test_shard_count"), config.getoption("test_shard_index")
    if count == 1:
        return
    try:
        selected = select_shard([item.nodeid for item in items], count, index)
    except ValueError as error:
        raise pytest.UsageError(str(error)) from error
    deselected = [item for item in items if item.nodeid not in selected]
    items[:] = [item for item in items if item.nodeid in selected]
    config.hook.pytest_deselected(items=deselected)
