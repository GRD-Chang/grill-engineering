"""Capability reporting/classification units; substitutes are not native proof."""
import json
import sys

import pytest

from agent_run.storage import save_order
from support import report_environment as report


def test_save_clock_probe_requires_and_closes_the_real_interface(monkeypatch):
    events = []

    class Clock:
        def require(self):
            events.append('require')

        def close(self):
            events.append('close')

    monkeypatch.setattr(save_order, 'SaveClock', Clock)
    assert report.save_clock_capability() == {'available': True}
    assert events == ['require', 'close']


@pytest.mark.parametrize('failure', [FileNotFoundError(2, 'unavailable'), ValueError('unproven')])
def test_unproven_clock_is_not_a_pass_or_a_skip(monkeypatch, failure):
    def unavailable():
        raise failure

    monkeypatch.setattr(save_order, 'SaveClock', unavailable)
    result = report.save_clock_capability()
    assert result['available'] is False and result['error_type'] == type(failure).__name__


@pytest.mark.parametrize('phase', ['require', 'close'])
def test_watchdog_is_preserved_even_when_clock_cleanup_also_fails(monkeypatch, phase):
    watchdog, cleanup = TimeoutError('original watchdog'), OSError('cleanup unavailable')
    events = []

    class Clock:
        def require(self):
            if phase == 'require':
                raise watchdog
            raise OSError('native evidence unavailable')

        def close(self):
            events.append('close')
            raise cleanup if phase == 'require' else watchdog

    monkeypatch.setattr(save_order, 'SaveClock', Clock)
    with pytest.raises(TimeoutError) as caught:
        report.save_clock_capability()
    assert caught.value is watchdog and events == ['close']


@pytest.mark.parametrize('missing', ['memfd', 'pidfd', 'save_clock', 'user_namespace'])
def test_any_missing_capability_blocks_native_verification(monkeypatch, missing):
    for name in ('memfd', 'pidfd', 'save_clock'):
        monkeypatch.setattr(report, name + '_capability', lambda name=name: {'available': name != missing})

    def command(*args):
        return {'returncode': int(missing == 'user_namespace') if args[0] == 'bwrap' else 0}

    monkeypatch.setattr(report, 'command', command)
    result = report.native_capabilities()
    assert result['status'] == 'unsupported' and result['complete_product_pass'] is False
    assert result['checks'][missing]['available'] is False


@pytest.mark.parametrize('supported', [False, True])
def test_required_capability_command_returns_nonzero_for_unsupported(monkeypatch, capsys, supported):
    monkeypatch.setattr(sys, 'argv', ['report_environment.py', '--require-native'])
    expected = {'status': 'supported' if supported else 'unsupported', 'complete_product_pass': False}
    monkeypatch.setattr(report, 'native_capabilities', lambda: expected)
    if supported:
        report.main()
    else:
        with pytest.raises(SystemExit) as caught:
            report.main()
        assert caught.value.code == 2
    shown = capsys.readouterr()
    assert json.loads(shown.out) == expected
    assert ('UNRUN' in shown.err) is not supported


@pytest.mark.parametrize('operation', ['memfd', 'pidfd', 'command'])
def test_capability_helpers_do_not_convert_watchdog_into_diagnostics(monkeypatch, operation):
    failure = TimeoutError('original watchdog')

    def interrupted(*args, **kwargs):
        raise failure

    if operation == 'command':
        monkeypatch.setattr(report.subprocess, 'run', interrupted)
        call = lambda: report.command('bwrap', '--version')
    else:
        monkeypatch.setattr(report.os, 'memfd_create' if operation == 'memfd' else 'pidfd_open', interrupted)
        call = getattr(report, operation + '_capability')
    with pytest.raises(TimeoutError) as caught:
        call()
    assert caught.value is failure
