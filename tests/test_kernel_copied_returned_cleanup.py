"""Normal copied returns must retire only their proved private resources."""
import sqlite3
import json
from pathlib import Path

import pytest

from agent_run_kernel.effects import LocalGit
from agent_run.managed_workspace import ManagedWorkspace
from test_kernel_migration_original_source import _git_bytes
from agent_run_kernel.offline import OfflinePort, Scenario
import test_copied_returned_decoder as copied_fixture
from agent_run_kernel.migration_active import read_copied_returned_import
from agent_run_kernel.migration import MigrationRefused
from agent_run_kernel.migration_records import V6RecordReader
from agent_run_kernel.model import wire_json
from agent_run_kernel.model import Role
from agent_run_kernel.migration_active import _copied_checkout_status


@pytest.mark.parametrize('observed', [
    {}, {'ticket_checkout_clean': False}, {'ticket_checkout_clean': None},
    {'ticket_checkout_clean': 1}, {'ticket_checkout_clean': 'true'},
    {'ticket_checkout_status': None}, {'ticket_checkout_status': ''},
    {'ticket_checkout_status': [], 'ticket_checkout_clean': False},
    {'ticket_checkout_status': ['?? file'], 'ticket_checkout_clean': True},
])
def test_checkout_observation_never_assumes_unknown_or_contradictory_clean(observed):
    with pytest.raises(MigrationRefused, match='source_checkout'):
        _copied_checkout_status(observed, Role.PUBLICATION)


def test_publication_clean_boolean_decodes_before_private_transport(tmp_path, git_repo, monkeypatch):
    public_convert = copied_fixture.convert_returned_role_v6
    checked = []
    def convert(snapshot, destination, **options):
        # A separate synthetic input uses the old boolean format. None of
        # the fixed oracle files owned by the other session are accessed.
        observation = options['observation_snapshot']
        value = json.loads(observation.read_bytes())
        assert value.pop('ticket_checkout_status') == []
        value['ticket_checkout_clean'] = True
        observation.write_bytes(wire_json(value))
        original = observation.read_bytes()
        facts = read_copied_returned_import(snapshot, destination, **options)
        native = public_convert(snapshot, destination, **options)
        assert facts.subjects[0].resources[0].checkout_status == ()
        assert V6RecordReader(native).document('oracle:observation')['body'] == original
        assert observation.read_bytes() == original
        checked.append(True)
        return native
    monkeypatch.setattr(copied_fixture, 'convert_returned_role_v6', convert)
    test_copied_returned_ticket_completes_and_retires_its_exact_resources(tmp_path, git_repo, monkeypatch, 'publication')
    assert checked == [True]


@pytest.mark.parametrize('role', ['development', 'review', 'publication'])
def test_copied_returned_ticket_completes_and_retires_its_exact_resources(tmp_path, git_repo, monkeypatch, role):
    installed = []
    public_convert = copied_fixture.convert_returned_role_v6
    def convert(snapshot, destination, **options):
        facts = read_copied_returned_import(snapshot, destination, **options)
        native = public_convert(snapshot, destination, **options)
        installed.append((facts, native))
        return native
    monkeypatch.setattr(copied_fixture, 'convert_returned_role_v6', convert)
    copied_fixture.test_copied_returned_facts_keep_private_transport_and_original_untouched(tmp_path, git_repo, role)
    facts, native = installed[0]
    source_root = ManagedWorkspace.for_repository(facts.spec.repository).repository_root
    before = facts.source.snapshot.source.read_bytes(), facts.source.snapshot.path.read_bytes(), _git_bytes(source_root)
    private = native.storage.path.parent
    port = OfflinePort.create(private)
    identity = port.git.env.copy()
    port.git = LocalGit(Path(facts.spec.repository_root))
    port.git.env.update(identity)
    base = facts.subjects[0].historical_base
    delivery = native.delivery('ticket:2')
    with sqlite3.connect(port.database) as db:
        db.execute('DELETE FROM refs')
        for ref in ('refs/heads/main', delivery.source_ref, delivery.target_ref):
            db.execute('INSERT OR IGNORE INTO refs VALUES (?,?)', (ref, base.sha))
    scenario = Scenario.__new__(Scenario)
    scenario.script, scenario.port, scenario.kernel = None, port, native
    scenario.requirements, scenario.spec = facts.requirements, facts.spec
    scenario.delivery, scenario.initial = delivery, base
    scenario.execution = native.claim_executor('copied-return-cleanup')
    scenario.reviews, scenario.checks, scenario.edits = ['pass'], ['none'], None
    scenario.review_count = scenario.development_count = scenario.publication_count = 0
    scenario.heads, scenario.role_calls, scenario.checkpoints, scenario.reviewed_heads = [], [], [], []
    scenario.check_heads = {}
    with native.storage.transaction(write=False) as tx:
        identity = tx.one("SELECT id FROM material WHERE purpose='v6_import_identity'")
        proof = json.loads(tx.get(identity['id'], delivery.key))
        assert proof['source_mode'] == 'copied'
        assert proof['local_resources']['copy_uuid'] != facts.source.checkout_uuid
        assert not tx.rows("SELECT identity FROM history WHERE kind='v6_original_cutover'")
    assert scenario.run().kind == 'complete'
    cleanup = scenario.cleanup()
    assert cleanup.kind == 'complete', cleanup
    state = native.status(delivery.key)
    assert state['phase'] == 'complete' and state['resource_cleanup']['state'] == 'complete'
    assert not Path(delivery.checkout).exists()
    assert port.git.run('show-ref', '--verify', '--quiet', delivery.source_ref, check=False).returncode == 1
    assert [call['role'] for call in scenario.role_calls] == (
        ['review', 'publication'] if role == 'development' else ['publication'] if role == 'review' else [])
    assert state['budgets']['requirements']['developments_used'] == 1
    assert state['budgets']['requirements']['reviews_used'] == 1
    assert before == (facts.source.snapshot.source.read_bytes(), facts.source.snapshot.path.read_bytes(), _git_bytes(source_root))


@pytest.mark.parametrize('foreign', [False, True])
def test_private_copy_marker_is_neither_adopted_nor_left_by_failed_publication(tmp_path, git_repo, monkeypatch, foreign):
    public_convert = copied_fixture.convert_returned_role_v6
    checked = []
    def convert(snapshot, destination, **options):
        git = LocalGit(options['repository_root'])
        marker = options['repository_root'] / '.git' / 'agent-run-checkout-id'
        before = git.run('show-ref').stdout, git.run('status', '--porcelain', cwd=options['checkout']).stdout
        with monkeypatch.context() as patch:
            if foreign:
                marker.write_bytes(b'f' * 32 + b'\n')
                expected = pytest.raises(MigrationRefused, match='copied_repository_identity_exists')
            else:
                def fail(*args, **kwargs):
                    raise OSError('controlled publication failure')
                patch.setattr('agent_run_kernel.migration_run_install.os.link', fail)
                expected = pytest.raises(OSError, match='controlled publication failure')
            with expected:
                public_convert(snapshot, destination, **options)
        assert not destination.exists()
        assert (marker.read_bytes() == b'f' * 32 + b'\n') if foreign else not marker.exists()
        assert before == (git.run('show-ref').stdout, git.run('status', '--porcelain', cwd=options['checkout']).stdout)
        checked.append(True)
        raise RuntimeError('checked private-copy publication refusal')
    monkeypatch.setattr(copied_fixture, 'convert_returned_role_v6', convert)
    with pytest.raises(RuntimeError, match='checked private-copy publication refusal'):
        copied_fixture.test_copied_returned_facts_keep_private_transport_and_original_untouched(tmp_path, git_repo, 'review')
    assert checked == [True]
