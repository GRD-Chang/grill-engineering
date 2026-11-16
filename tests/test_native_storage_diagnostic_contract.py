"""Pure Python probe contract checks; these are not native Storage evidence."""
from contextlib import contextmanager
import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('failure_at,failure', [
    (None, None),
    ('ordered_writer', RuntimeError('original')), ('ordered_writer', TimeoutError('watchdog')),
    ('ordered_writer', InterruptedError('interrupt')),
    ('clock_require', RuntimeError('original')), ('clock_require', TimeoutError('watchdog')),
    ('clock_require', InterruptedError('interrupt')),
])
def test_probe_counts_or_preserves_original_exception(tmp_path, capsys, failure_at, failure):
    module = runpy.run_path(str(Path(__file__).parent / 'diagnostics/native_storage_costs.py'))
    probe = module['test_native_storage_fixed_costs']
    calls, finalizers, state = [], [], [0]

    class Clock:
        def require(self):
            calls.append('clock_require')
            if failure_at == 'clock_require':
                raise failure
        def close(self):
            calls.append('clock_close')

    class Transaction:
        def one(self, sql, parameters=()):
            return (0 if 'save_admissions' in sql else state[0],)
        def execute(self, sql, parameters):
            state[0] = parameters[0]

    @contextmanager
    def transaction(**options):
        name = ('reader' if options.get('write') is False else
                'unordered_writer' if options.get('ordered') is False else 'ordered_writer')
        calls.append(name)
        if name == failure_at:
            raise failure
        yield Transaction()

    def created(path, spec):
        calls.append('created')
        return SimpleNamespace(storage=SimpleNamespace(run_id=spec.run_id, transaction=transaction)), None

    probe.__globals__.update(native_agent_fixture=created, SaveClock=Clock, ci_identity=lambda: {})
    try:
        if failure_at is None:
            probe(tmp_path, SimpleNamespace(addfinalizer=finalizers.append))
            report = json.loads(capsys.readouterr().out)
            assert report['complete_product_pass'] is False
            assert report['stage'] == 'native-storage-fixed-costs'
            assert {key: len(rows) for key, rows in report['samples'].items()} == {
                'reader': 8, 'unordered_writer': 8, 'ordered_writer': 8, 'clock_require': 8}
            assert calls.count('reader') == 9  # Final queue/result verification is outside measurement.
            assert all(calls.count(name) == 8 for name in ('unordered_writer', 'ordered_writer', 'clock_require'))
        else:
            with pytest.raises(type(failure)) as caught:
                probe(tmp_path, SimpleNamespace(addfinalizer=finalizers.append))
            assert caught.value is failure
            assert capsys.readouterr().out == ''
            assert calls.count(failure_at) == 1
    finally:
        for close in finalizers:
            close()
    assert calls.count('created') == 1
    assert len(finalizers) == 1 and calls.count('clock_close') == 1


def test_probe_dataset_preparation_uses_actual_public_creation(tmp_path):
    module = runpy.run_path(str(Path(__file__).parent / 'diagnostics/native_storage_costs.py'))
    store = module['_prepare_storage'](tmp_path)
    with store.transaction(write=False) as tx:
        assert tx.one('SELECT stopped FROM runs WHERE id=?', (store.run_id,))[0] == 0
        assert tx.one('SELECT count(*) FROM save_admissions')[0] == 0
