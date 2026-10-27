"""将完整收集结果分成互斥且均衡的 CI 分片，默认仍运行全部用例。"""
from __future__ import annotations

import pytest
import sys
from _pytest.junitxml import LogXML, xml_key
from _pytest.subtests import SubtestReport
from _pytest.terminal import TerminalReporter


PASSED_SUBTESTS_PROPERTY = "agent-run-pytest-passed-subtests-v1"
FAILURE_TRACEBACK_CHARACTERS = 64 * 1024
SLOW_PHASE_LOG_SECONDS = 5.0


class _Reports:
    def __init__(self, xml: LogXML | None, terminal: TerminalReporter | None) -> None:
        self.xml = xml
        self.terminal = terminal
        self.passed = 0

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        # Observe pytest's final report classification, including the parent
        # call which pytest 9 marks failed after a failed subtest.
        if report.duration >= SLOW_PHASE_LOG_SECONDS:
            timing = (f"[agent-run slow] phase={report.when} duration_seconds={report.duration!r} "
                      f"nodeid={report.nodeid!r}")
            if self.terminal is None:
                print(timing, file=sys.stderr, flush=True)
            else:
                self.terminal.write_line(timing)
                self.terminal.flush()
        if report.failed:
            heading = f"[agent-run failure] phase={report.when} nodeid={report.nodeid!r}"
            detail = report.longreprtext
            if len(detail) > FAILURE_TRACEBACK_CHARACTERS:
                marker = "\n... traceback truncated; original pytest summary/JUnit unchanged ...\n"
                head = FAILURE_TRACEBACK_CHARACTERS // 4
                tail = FAILURE_TRACEBACK_CHARACTERS - head - len(marker)
                detail = detail[:head] + marker + detail[-tail:]
            if self.terminal is None:
                print(heading, detail, sep="\n", file=sys.stderr, flush=True)
            else:
                self.terminal.write_line(heading)
                self.terminal.write_line(detail)
                self.terminal.flush()
        if isinstance(report, SubtestReport) and report.when == "call" and report.passed:
            self.passed += 1

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionfinish(self) -> None:
        # pytest 9 把成功 subtest 计入 tests，但复用父 testcase；不改写原生计数。
        if self.xml is not None:
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
    if not hasattr(config, "workerinput"):
        # Workers send their original reports; only the controller emits live
        # diagnostics and counts passed subtests, including without JUnit.
        config.pluginmanager.register(_Reports(config.stash.get(xml_key, None),
            config.pluginmanager.get_plugin("terminalreporter")))


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
