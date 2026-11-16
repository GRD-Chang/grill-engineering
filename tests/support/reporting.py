"""记录真实 pytest 报告、即时诊断与执行参数，不参与测试选取。"""
from __future__ import annotations

import pytest
import sys
import json
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


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    if not hasattr(config, "workerinput"):
        # Workers send their original reports; only the controller emits live
        # diagnostics and counts passed subtests, including without JUnit.
        config.pluginmanager.register(_Reports(config.stash.get(xml_key, None),
            config.pluginmanager.get_plugin("terminalreporter"),
            {'host_loss_io_diagnostics': config.getoption('host_loss_io_diagnostics', default=False),
             'workers': config.getoption('numprocesses', default=0), 'dist': config.getoption('dist', default='no')}))
