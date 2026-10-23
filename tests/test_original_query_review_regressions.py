"""Native control history stays in recorded order and queries remain read-only."""
from agent_run_kernel.query import read
from test_kernel_query_cli import _create, _snapshot as _bytes


def test_native_page_control_records_keep_the_recorded_order(tmp_path):
    kernel = _create(tmp_path / 'native')
    actions = ['z-stop', 'z-resume', 'a-stop', 'a-resume']
    for kind, identity in zip(['stop', 'resume', 'stop', 'resume'], actions):
        kernel.control(kind, identity, subject='ticket:2')
    before = _bytes(tmp_path)
    assert [row['id'] for row in read(kernel.storage.path, 'history').data['actions']] == actions
    assert _bytes(tmp_path) == before
