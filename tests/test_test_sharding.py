"""分片完整性、真实 pytest 收集与错误参数的回归。"""
from __future__ import annotations

import os
from pathlib import Path
import selectors
import subprocess
import sys
import time
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from support.sharding import FAILURE_TRACEBACK_CHARACTERS, PASSED_SUBTESTS_PROPERTY, _Reports, pytest_configure, select_shard


@pytest.mark.parametrize("count", [1, 2, 3, 7])
def test_shards_are_disjoint_complete_balanced_and_order_independent(count: int) -> None:
    nodes = [f"tests/test_sample.py::test_case[{i}]" for i in range(101)]
    shards = [select_shard(nodes, count, index) for index in range(count)]
    assert set.union(*shards) == set(nodes)
    assert sum(map(len, shards)) == len(nodes)
    assert max(map(len, shards)) - min(map(len, shards)) <= 1
    assert shards == [select_shard(list(reversed(nodes)), count, index) for index in range(count)]


def test_completed_boundary_selector_only_removes_the_exact_expanded_fixed_partition():
    from runpy import run_path
    scope = run_path(str(Path(__file__).resolve().parents[1] / '.github/ci/test_scope.py'))
    targets = scope['NATIVE_CONTRACT_TESTS']
    expanded = []
    for target in targets:
        if target.endswith('test_registration_commit_unknown_reads_only_original_uuid_and_never_reinserts'):
            expanded.extend(target + '[' + p + ']' for p in ('False', 'True'))
        elif target.endswith('test_registration_commit_known_failure_keeps_category_and_never_registers_again'):
            expanded.extend(target + '[' + p + ']' for p in ('13-storage_limit', '9-transaction_aborted'))
        else:
            expanded.append(target)
    completed = set(expanded) | set(scope['BOUNDARY_TESTS'])
    rest = [f'tests/test_other.py::test_value[{i}]' for i in range(21)]
    nodes = [*completed, *rest]
    shards = [select_shard(nodes, 4, i, completed_boundary=True) for i in range(4)]
    assert set.union(*shards) == set(rest) and sum(map(len, shards)) == len(rest)
    assert not set.union(*shards) & completed
    assert set.union(*(select_shard(nodes, 4, i) for i in range(4))) == set(nodes)
    for damaged in (nodes[:-1] + [nodes[0]], [n for n in nodes if n != expanded[0]],
                    [*nodes, expanded[0].partition('[')[0] + '[extra]']):
        with pytest.raises(ValueError):
            select_shard(damaged, 4, 0, completed_boundary=True)


@pytest.mark.parametrize('diagnostic,workers', [(False, 2), (True, 2), (False, 1), (False, 0)])
def test_report_profile_comes_from_actual_controller_options(diagnostic, workers):
    registered, properties = [], []
    options = {'test_shard_count': 4, 'test_shard_index': 0,
               'host_loss_io_diagnostics': diagnostic, 'numprocesses': workers, 'dist': 'worksteal'}
    config = SimpleNamespace(getoption=lambda key, default=None: options.get(key, default),
        stash={}, pluginmanager=SimpleNamespace(register=registered.append, get_plugin=lambda name: None))
    pytest_configure(config)
    report, = registered
    report.xml = SimpleNamespace(add_global_property=lambda key, value: properties.append((key, value)))
    report.pytest_sessionfinish()
    import json
    from support.sharding import EXECUTION_PROFILE_PROPERTY
    value, = [value for key, value in properties if key == EXECUTION_PROFILE_PROPERTY]
    assert json.loads(value) == {'host_loss_io_diagnostics': diagnostic, 'workers': workers, 'dist': 'worksteal'}


@pytest.mark.parametrize("count,index,nodes", [
    (0, 0, ["a"]), (2, -1, ["a"]), (2, 2, ["a"]),
    (2, 1, ["a"]), (2, 0, []), (2, 0, ["a", "a"]),
])
def test_invalid_or_empty_shards_cannot_report_success(count: int, index: int, nodes: list[str]) -> None:
    with pytest.raises(ValueError):
        select_shard(nodes, count, index)


def test_real_pytest_shards_cover_the_unfiltered_collection(tmp_path: Path) -> None:
    sample = tmp_path / "test_sample.py"
    sample.write_text("import pytest\n@pytest.mark.parametrize('value',range(7))\ndef test_case(value):\n    assert value >= 0\n")
    command = [sys.executable, "-m", "pytest", "--noconftest", "-p", "support.sharding", "--collect-only", "-q", str(sample)]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent), "PYTHONDONTWRITEBYTECODE": "1"}

    def collect(*args: str) -> set[str]:
        result = subprocess.run([*command, *args], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        return {line for line in result.stdout.splitlines() if "::test_case[" in line}

    complete = collect()
    shards = [collect("--test-shard-count=2", f"--test-shard-index={index}") for index in range(2)]
    assert len(complete) == 7
    assert set.union(*shards) == complete
    assert shards[0].isdisjoint(shards[1])
    invalid = subprocess.run([*command, "--test-shard-count=2", "--test-shard-index=2"], cwd=tmp_path, env=env,
                             capture_output=True, text=True, timeout=15)
    assert invalid.returncode == 4
    assert "0 <= index < count" in invalid.stderr


@pytest.mark.parametrize('phase', ['setup', 'call', 'teardown'])
def test_failure_report_is_flushed_before_session_finish_or_interruption(phase):
    lines, flushed = [], []
    terminal = SimpleNamespace(write_line=lines.append, flush=lambda: flushed.append(tuple(lines)))
    observer = _Reports(None, terminal)
    with pytest.raises(ValueError) as cause:
        raise ValueError('Original failure; not a replacement outcome')
    report = pytest.TestReport('test_sample.py::test_original', ('test_sample.py', 0, 'test_original'),
                              {}, 'failed', cause.getrepr(), phase)
    before = vars(report).copy()
    with pytest.raises(KeyboardInterrupt):
        observer.pytest_runtest_logreport(report)
        assert len(flushed) == 1
        assert flushed[0][0] == f"[agent-run failure] phase={phase} nodeid='test_sample.py::test_original'"
        assert 'ValueError: Original failure; not a replacement outcome' in flushed[0][1]
        raise KeyboardInterrupt('No sessionfinish is delivered after this point')
    assert vars(report) == before and report.failed
    assert len(flushed) == 1


def test_live_traceback_is_bounded_without_changing_the_original_report():
    lines, flushed = [], []
    observer = _Reports(None, SimpleNamespace(write_line=lines.append, flush=lambda: flushed.append(True)))
    original = 'Original first frame\n' + 'x' * (2 * FAILURE_TRACEBACK_CHARACTERS) + '\nValueError: root cause'
    report = pytest.TestReport('test_large.py::test_original', ('test_large.py', 0, 'test_original'),
                              {}, 'failed', original, 'call')
    observer.pytest_runtest_logreport(report)
    assert len(lines[1]) == FAILURE_TRACEBACK_CHARACTERS and flushed == [True]
    assert lines[1].startswith('Original first frame') and lines[1].endswith('ValueError: root cause')
    assert 'traceback truncated' in lines[1]
    assert report.longrepr == original and report.failed


@pytest.mark.parametrize('outcome', ['passed', 'skipped'])
@pytest.mark.parametrize('duration', [0.0, 4.999999])
def test_nonfailure_reports_do_not_emit_live_failure_diagnostics(outcome, duration):
    lines = []
    observer = _Reports(None, SimpleNamespace(write_line=lines.append, flush=lambda: lines.append('flush')))
    report = pytest.TestReport('test_sample.py::test_original', ('test_sample.py', 0, 'test_original'),
                              {}, outcome, None, 'call', duration=duration)
    observer.pytest_runtest_logreport(report)
    assert lines == [] and report.outcome == outcome


@pytest.mark.parametrize('worker', [False, True])
def test_only_controller_registers_live_reporting(worker, capsys):
    registered = []
    config = SimpleNamespace(getoption=lambda key, default=None: {'test_shard_count': 1, 'test_shard_index': 0}.get(key, default),
        stash={}, pluginmanager=SimpleNamespace(register=registered.append, get_plugin=lambda name: None))
    if worker:
        config.workerinput = {}
    pytest_configure(config)
    assert len(registered) == (0 if worker else 1)
    report = pytest.TestReport('test_sample.py::test_slow', ('test_sample.py', 0, 'test_slow'),
                              {}, 'passed', None, 'call', duration=5.0)
    for observer in registered:
        observer.pytest_runtest_logreport(report)
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err.count('[agent-run slow]') == (0 if worker else 1)


@pytest.mark.parametrize('phase', ['setup', 'call', 'teardown'])
@pytest.mark.parametrize('duration', [5.0, 17.12500001])
@pytest.mark.parametrize('outcome', ['passed', 'failed'])
def test_slow_completed_phase_logs_exact_report_duration_without_changing_outcome(phase, duration, outcome):
    lines, flushed = [], []
    observer = _Reports(None, SimpleNamespace(write_line=lines.append, flush=lambda: flushed.append(tuple(lines))))
    report = pytest.TestReport('test_sample.py::test_slow', ('test_sample.py', 0, 'test_slow'),
                              {}, outcome, 'RuntimeError: original slow failure' if outcome == 'failed' else None,
                              phase, duration=duration)
    before = vars(report).copy()
    observer.pytest_runtest_logreport(report)
    assert lines[0] == (f"[agent-run slow] phase={phase} duration_seconds={duration!r} "
                        "nodeid='test_sample.py::test_slow'")
    assert len(flushed) == (2 if outcome == 'failed' else 1)
    assert flushed[0] == (lines[0],)
    if outcome == 'failed':
        assert lines[1].startswith('[agent-run failure]')
        assert lines[2] == 'RuntimeError: original slow failure'
    else:
        assert len(lines) == 1
    assert vars(report) == before and report.outcome == outcome


@pytest.mark.parametrize('workers', [0, 2])
def test_real_failure_reports_preserve_summary_junit_and_exit_code(tmp_path, workers):
    sample = tmp_path / 'test_failures.py'
    sample.write_text('''import pytest
@pytest.fixture
def bad_setup():
    raise RuntimeError('original setup failure')
@pytest.fixture
def bad_teardown():
    yield
    raise RuntimeError('original teardown failure')
def test_setup(bad_setup):
    pass
def test_call():
    assert False, 'original call failure'
def test_teardown(bad_teardown):
    pass
def test_subtests(subtests):
    with subtests.test(case='passed'):
        assert True
    with subtests.test(case='failed'):
        assert False, 'original subtest failure'
''')
    junit = tmp_path / 'failures.xml'
    command = [sys.executable, '-m', 'pytest', '--noconftest', '-p', 'support.sharding',
               '-q', str(sample), '--junitxml=' + str(junit)]
    if workers:
        command += ['-p', 'xdist.plugin', '-n', str(workers)]
    environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).parent),
                   'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 1, result.stdout + result.stderr
    # pytest 9 retains both the failed subtest and its failed parent call.
    # Observe that classification after the normal reporting hooks, without
    # deduplicating outcomes or counting worker-side console output.
    assert result.stdout.count('[agent-run failure]') == 5
    for name in ('test_setup', 'test_call', 'test_teardown'):
        assert result.stdout.count("nodeid='test_failures.py::" + name + "'") == 1
    for phase in ('setup', 'call', 'teardown'):
        assert '[agent-run failure] phase=' + phase in result.stdout
    assert 'short test summary info' in result.stdout
    for reason in ('original setup failure', 'original call failure', 'original teardown failure', 'original subtest failure'):
        assert reason in result.stdout
    document = ET.parse(junit)
    assert len(document.findall('.//failure')) == 3
    assert len(document.findall('.//error')) == 2
    properties = document.findall('.//property')
    assert any(row.attrib == {'name': PASSED_SUBTESTS_PROPERTY, 'value': '1'} for row in properties)


def test_live_failure_is_available_before_controlled_pytest_cancellation(tmp_path):
    sample = tmp_path / 'test_interrupted.py'
    sample.write_text('''import sys
def test_a_failure():
    assert False, 'original failure before interruption'
def test_b_hold_for_owner():
    print('held-for-controlled-cancellation', flush=True)
    sys.stdin.read(1)
''')
    command = [sys.executable, '-m', 'pytest', '--noconftest', '-p', 'support.sharding', '-q', '-s', str(sample)]
    environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).parent),
                   'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    # This direct child has no xdist/Host/Worker or child-launching test. Kill
    # only its still-owned Popen handle after reading the flushed diagnostic.
    with subprocess.Popen(command, cwd=tmp_path, env=environment, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as child:
        try:
            assert child.stdout is not None
            output = bytearray()
            deadline = time.monotonic() + 10
            with selectors.DefaultSelector() as ready:
                ready.register(child.stdout, selectors.EVENT_READ)
                while b'held-for-controlled-cancellation\n' not in output:
                    assert time.monotonic() < deadline, output.decode(errors='replace')
                    for key, _ in ready.select(timeout=max(0, deadline - time.monotonic())):
                        data = os.read(key.fd, 65536)
                        assert data, output.decode(errors='replace')
                        output.extend(data)
                        assert len(output) <= 1024 * 1024
            assert child.poll() is None
            assert b"[agent-run failure] phase=call nodeid='test_interrupted.py::test_a_failure'" in output
            assert b'AssertionError: original failure before interruption' in output
            assert b'short test summary info' not in output
            child.kill()
            assert child.wait(timeout=5) != 0
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
