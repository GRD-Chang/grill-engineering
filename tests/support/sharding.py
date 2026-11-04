"""将完整收集的待执行部分按数量分成互斥 CI 分片，默认仍运行全部用例。"""
from __future__ import annotations

import pytest
import sys
import json
from pathlib import Path
from runpy import run_path
from _pytest.junitxml import LogXML, xml_key
from _pytest.subtests import SubtestReport
from _pytest.terminal import TerminalReporter


PASSED_SUBTESTS_PROPERTY = "agent-run-pytest-passed-subtests-v1"
FAILURE_TRACEBACK_CHARACTERS = 64 * 1024
SLOW_PHASE_LOG_SECONDS = 5.0
EXECUTION_PROFILE_PROPERTY = 'agent-run-execution-profile-v1'


class _Reports:
    def __init__(self, xml: LogXML | None, terminal: TerminalReporter | None, profile: dict | None = None) -> None:
        self.xml = xml
        self.terminal = terminal
        self.passed = 0
        self.profile = profile

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
            if self.profile is not None:
                self.xml.add_global_property(EXECUTION_PROFILE_PROPERTY, json.dumps(self.profile, sort_keys=True))


def select_shard(nodeids: list[str], count: int, index: int, *, completed_boundary: bool = False) -> set[str]:
    if count < 1 or not 0 <= index < count:
        raise ValueError("Test shard requires count >= 1 and 0 <= index < count")
    if len(set(nodeids)) != len(nodeids):
        raise ValueError("Test sharding requires unique collected node IDs")
    if completed_boundary:
        scope = run_path(str(Path(__file__).resolve().parents[2] / '.github/ci/test_scope.py'))
        completed = set(scope['completed_boundary_nodes'](nodeids))
        nodeids = [node for node in nodeids if node not in completed]
    selected = set(sorted(nodeids)[index::count])
    if not selected:
        raise ValueError("Test shard has no collected tests")
    return selected


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("CI sharding")
    group.addoption("--test-shard-count", type=int, default=1)
    group.addoption("--test-shard-index", type=int, default=0)
    group.addoption('--test-completed-boundary', action='store_true', default=False,
                    help='Partition after the fixed official 19/14; CI still requires their validated receipt')


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    count, index = config.getoption("test_shard_count"), config.getoption("test_shard_index")
    if count < 1 or not 0 <= index < count:
        raise pytest.UsageError("Test shard requires count >= 1 and 0 <= index < count")
    if not hasattr(config, "workerinput"):
        # Workers send their original reports; only the controller emits live
        # diagnostics and counts passed subtests, including without JUnit.
        config.pluginmanager.register(_Reports(config.stash.get(xml_key, None),
            config.pluginmanager.get_plugin("terminalreporter"),
            {'host_loss_io_diagnostics': config.getoption('host_loss_io_diagnostics', default=False),
             'workers': config.getoption('numprocesses', default=0), 'dist': config.getoption('dist', default='no')}))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    count, index = config.getoption("test_shard_count"), config.getoption("test_shard_index")
    completed = config.getoption('test_completed_boundary')
    if count == 1 and not completed:
        return
    try:
        selected = select_shard([item.nodeid for item in items], count, index, completed_boundary=completed)
    except ValueError as error:
        raise pytest.UsageError(str(error)) from error
    deselected = [item for item in items if item.nodeid not in selected]
    items[:] = [item for item in items if item.nodeid in selected]
    config.hook.pytest_deselected(items=deselected)
