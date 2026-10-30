"""Explicit one-shot node; this file is outside normal test-file discovery."""
import json
import time

from agent_run.model import Commit, Delivery, DeliveryKind, RunSpec
from agent_run.storage.save_order import SaveClock
from support.core_operations import native_agent_fixture
from support.report_environment import ci_identity


def _prepare_storage(tmp_path):
    # Normal public creation, with no clock substitute or instrumentation.
    base = Commit('a' * 40, 'b' * 40)
    delivery = Delivery('parent:1', DeliveryKind.PARENT_ONLY, 1, 'original', base,
                        'refs/heads/storage-diagnostic', 'refs/heads/main')
    spec = RunSpec('native-storage-diagnostic', 'example/project', 1, 'parent', 'graph',
                   (delivery,), language='zh', repository_root=str(tmp_path))
    kernel, _ = native_agent_fixture(tmp_path / 'state' / 'kernel.sqlite3', spec)
    return kernel.storage


def test_native_storage_fixed_costs(tmp_path, request):
    store = _prepare_storage(tmp_path)
    run = store.run_id
    clock = SaveClock()
    request.addfinalizer(clock.close)
    samples = {name: [] for name in ('reader', 'unordered_writer', 'ordered_writer', 'clock_require')}
    expected = 0
    for _ in range(8):
        for name, options in (('reader', {'write': False}),
                              ('unordered_writer', {'ordered': False}),
                              ('ordered_writer', {})):
            wall, cpu = time.monotonic_ns(), time.thread_time_ns()
            with store.transaction(**options) as tx:
                if name == 'reader':
                    assert tx.one('SELECT stopped FROM runs WHERE id=?', (run,))[0] == expected
                else:
                    expected = 1 - expected
                    tx.execute('UPDATE runs SET stopped=? WHERE id=?', (expected, run))
            samples[name].append([time.monotonic_ns() - wall, time.thread_time_ns() - cpu])
        wall, cpu = time.monotonic_ns(), time.thread_time_ns()
        clock.require()
        samples['clock_require'].append([time.monotonic_ns() - wall, time.thread_time_ns() - cpu])
    with store.transaction(write=False) as tx:
        assert tx.one('SELECT stopped FROM runs WHERE id=?', (run,))[0] == expected == 0
        assert tx.one('SELECT count(*) FROM save_admissions')[0] == 0
    assert all(len(rows) == 8 for rows in samples.values())
    print(json.dumps({'stage': 'native-storage-fixed-costs', 'complete_product_pass': False,
        'ci_identity': ci_identity(), 'columns': ['wall_ns', 'thread_cpu_ns'], 'samples': samples,
        'scope': 'Serial uncontended public calls. Writer samples change a Run field. Ordered-minus-unordered includes clock checks, FIFO and extra commit work; it does not isolate fsync. Clock samples measure require(), excluding construction and close. No Host, Worker or full-product acceptance.'}))
