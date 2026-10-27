"""Final CLI query/profile assembly and write-before-refusal boundaries.

Only real files, SQLite and in-process CLI parsing are used. No child, Host,
notification thread, login, model probe, service or protocol activation is run.
All new nodes are supplied UNRUN by the author for independent execution.
"""

from support.repository_fixture import create_kernel
from contextlib import closing
from pathlib import Path
import json
import sqlite3

import pytest

from agent_run.cli import main as cli_main, profiles as profile_cli, selection
from agent_run.config.defaults import UserDefaultsStore
from agent_run.contracts.task import TaskKey
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import Commit, Delivery, DeliveryKind, ParentIssue, Requirements, RunSpec
from agent_run.queries.read_facts import QueryError
from agent_run.queries.run import header, read
from agent_run.storage import routing
from agent_run.storage.preflight import UnsupportedState
from agent_run.storage.routing import RepositoryRoute
from agent_run.storage.sqlite import Storage
from support.offline import fixture_inputs

SUBJECT = 'shared-subject'


def proposal(root, name, parent=1):
    base = Commit('a' * 40, 'b' * 40)
    delivery = Delivery(SUBJECT, DeliveryKind.PARENT_ONLY, parent, 'unobserved', base,
        'refs/heads/work-' + name, 'refs/heads/main')
    return RunSpec(name, 'example/project', parent, 'r', 'g', (delivery,), language='en',
        repository_root=str(root), inputs=fixture_inputs()), Requirements(
            ParentIssue(parent, 'Parent ' + str(parent), 'Frozen requirement ' + str(parent)), ())


@pytest.fixture
def repository(tmp_path):
    route = RepositoryRoute(tmp_path / 'state' / 'kernel.sqlite3', tmp_path / 'repository', 'example/project')
    route.initialize()
    for name, parent in (('A', 1), ('B', 2)):
        spec, source = proposal(route.repository_root, name, parent)
        assert route.create(TaskKey(route.repository_root, route.repository, parent), spec, source).created
    return route


def dump(database):
    with closing(sqlite3.connect(database)) as db:
        return tuple(db.iterdump())


def parsed(arguments):
    return cli_main._parser('en').parse_args(arguments)


@pytest.mark.parametrize('kind', ['status', 'history', 'settings'])
def test_existing_reader_projects_exact_selected_run_without_fallback_or_write(repository, kind, monkeypatch):
    before = dump(repository.database)
    storage = repository.at_run('A')
    document = read(repository.database, kind, storage=storage,
        expected_run='A', expected_repository='example/project')
    assert document.kind == kind and document.data['run_id'] == 'A'
    info = header(repository.database, storage=storage)
    assert info['run_id'] == 'A' and info['parent'] == 1
    assert info['state_dir'] == str(repository.database.parent)
    assert info['artifacts_dir'] == str(repository.kernel('A').run_paths().artifacts)
    assert dump(repository.database) == before
    def no_conflicting_read(*args, **kwargs):
        raise AssertionError('Conflicting selector entered a database transaction')
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'transaction', no_conflicting_read)
        with pytest.raises(QueryError) as wrong:
            read(repository.database, kind, storage=storage, expected_run='B')
    assert wrong.value.code == 'run_locator_conflict'
    assert dump(repository.database) == before


def test_internal_query_adapter_uses_typed_targets_for_parent_and_historical_queries(repository):
    parent = parsed(['status', '--repo', repository.repository, '--parent', '1', '--json'])
    document = cli_main._repository_query(parent, (repository,), 'en')
    assert document.data['run_id'] == 'A' and document.data['parent']['number'] == 1
    history = parsed(['history', 'B', '--repo', repository.repository, '--json'])
    assert cli_main._repository_query(history, (repository,), 'en').data['run_id'] == 'B'
    listing = cli_main._repository_query(parsed(['runs', '--repo', repository.repository, '--json']), (repository,), 'en')
    assert [(item['run_id'], item['parent']) for item in listing.data['runs']] == [('A', 1), ('B', 2)]
    assert all(item['database'] == str(repository.database) for item in listing.data['runs'])
    # Metadata selection and projection do not arm a public lifecycle.
    assert not hasattr(routing, 'REPOSITORY_PUBLIC_READY')


def test_explicit_state_route_uses_database_namespace_instead_of_directory_name(repository, monkeypatch):
    def no_cwd():
        raise AssertionError('Explicit state query attempted cwd/Git discovery')
    monkeypatch.setattr(selection, '_cwd_repository', no_cwd)
    request = parsed(['status', 'A', '--state-dir', str(repository.database.parent)])
    routes = selection.managed_repository_routes(request)
    assert routes == (repository,) and routes[0].repository == 'example/project'
    assert cli_main._repository_query(request, routes, 'en').data['run_id'] == 'A'
    request.repo = 'foreign/project'
    with pytest.raises(QueryError) as wrong:
        selection.managed_repository_routes(request)
    assert wrong.value.code == 'run_locator_conflict'


def test_default_registry_adapter_needs_no_json_publication_or_checkout_to_find_exact_metadata(tmp_path, monkeypatch):
    from agent_run.adapters.workspace import ManagedWorkspace
    workspace = ManagedWorkspace.for_repository('example/project')
    route = RepositoryRoute.for_workspace(workspace)
    route.initialize()
    spec, source = proposal(route.repository_root, 'managed-A')
    route.create(TaskKey(route.repository_root, route.repository, 1), spec, source)
    monkeypatch.setattr(selection, '_cwd_repository', lambda: None)
    request = parsed(['runs', '--repo', 'example/project'])
    routes = selection.managed_repository_routes(request)
    assert routes == (route,) and not workspace.repository_root.exists()
    document = cli_main._repository_query(request, routes, 'en')
    assert [item['run_id'] for item in document.data['runs']] == ['managed-A']
    assert not (workspace.state_root / 'managed-A').exists()


def test_occupied_explicit_data_root_is_not_silently_reported_as_no_runs(tmp_path, monkeypatch):
    state = tmp_path / 'old-state'
    old = state / 'old-run' / 'kernel.sqlite3'
    old.parent.mkdir(parents=True)
    old.write_bytes(b'Original opaque old state')
    before = old.read_bytes()
    def no_cwd():
        raise AssertionError('Explicit data root attempted cwd/Git discovery')
    monkeypatch.setattr(selection, '_cwd_repository', no_cwd)
    with pytest.raises(UnsupportedState) as refused:
        selection.managed_repository_routes(parsed(['runs', '--data-root', str(state)]))
    assert refused.value.reason == 'repository_state_occupied'
    assert old.read_bytes() == before and not (state / 'kernel.sqlite3').exists()


def test_run_settings_ignore_later_personal_defaults_and_selected_profile_change_is_run_bound(repository, tmp_path, monkeypatch):
    settings = parsed(['settings', 'show', '--run', 'A', '--repo', repository.repository, '--json'])
    original = cli_main._repository_query(settings, (repository,), 'en').data
    defaults = UserDefaultsStore(tmp_path / 'personal' / 'settings.json')
    defaults.configure(profile={'development_model': 'future-only-model'}, language='zh')
    def no_defaults(*args, **kwargs):
        raise AssertionError('Frozen Run projection reloaded personal defaults')
    monkeypatch.setattr(UserDefaultsStore, 'load', no_defaults)
    frozen = cli_main._repository_query(settings, (repository,), 'en').data
    assert frozen == original and frozen['source'] == 'run_snapshot'
    before_b = repository.read('B', 'settings').data
    assert repository.kernel('A').configure_task_profiles('new-profile', overrides={'development_model': 'selected-new-model'}) == 2
    changed = cli_main._repository_query(settings, (repository,), 'en').data
    assert changed['profile']['profile_revision'] == 2
    assert changed['profile']['profiles']['development']['model'] == 'selected-new-model'
    assert repository.read('B', 'settings').data == before_b
    with repository.at_run('A').transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
        assert tx.one('SELECT count(*) FROM hosts')[0] == 0


def test_default_personal_settings_still_read_defaults_without_opening_repository_context(repository, monkeypatch, capsys):
    def no_repository(*args, **kwargs):
        raise AssertionError('Personal defaults attempted repository selection')
    monkeypatch.setattr(selection, 'repository_target', no_repository)
    monkeypatch.setattr(RepositoryRoute, 'metadata', no_repository)
    assert cli_main.main(['settings', 'show', '--json'], repository_routes=(repository,)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['scope'] == 'future-runs' and output['source'] != 'run_snapshot'


@pytest.mark.parametrize('arguments', [
    ['status', 'A', '--repo', 'example/project', '--json'],
    ['history', 'A', '--repo', 'example/project', '--json'],
    ['runs', '--repo', 'example/project', '--json'],
    ['settings', 'show', '--run', 'A', '--repo', 'example/project', '--json'],
    ['doctor', '--run', 'A', '--repo', 'example/project', '--local', '--json'],
    ['configure', 'A', '--repo', 'example/project', '--development-model', 'selected-model', '--json'],
    ['resume', 'A', '--repo', 'example/project', '--json'],
])
def test_public_repository_adapters_remain_closed_before_any_query_profile_or_probe(repository, monkeypatch, capsys, arguments):
    before = dump(repository.database)
    def no_repository(*args, **kwargs):
        raise QueryError('fixture_selection_boundary', 'Selected canonical route reached its controlled boundary')
    from agent_run.runtime import package_lease
    monkeypatch.setattr(package_lease, 'runner_usage_lease', no_repository)
    monkeypatch.setattr(selection, 'repository_target', no_repository)
    monkeypatch.setattr(selection, 'repository_candidates', no_repository)
    monkeypatch.setattr(RepositoryRoute, 'metadata', no_repository)
    assert cli_main.main(arguments, repository_routes=(repository,)) == 2
    assert json.loads(capsys.readouterr().out)['diagnostics'][0]['code'] == 'fixture_selection_boundary'
    assert dump(repository.database) == before


def test_direct_profile_adapter_is_also_closed_before_selection(repository, monkeypatch):
    request = parsed(['configure', 'A', '--repo', repository.repository, '--development-effort', 'low'])
    def no_selection(*args):
        raise QueryError('fixture_selection_boundary', 'Canonical selection reached')
    monkeypatch.setattr(selection, 'repository_target', no_selection)
    with pytest.raises(QueryError) as refused:
        profile_cli.execute(request, repository_routes=(repository,))
    assert refused.value.code == 'fixture_selection_boundary'


def test_current_profile_entry_keeps_original_frozen_authority_and_default_query_behavior(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    root = tmp_path / 'repository'
    spec, source = proposal(root, 'current-profile')
    database = tmp_path / 'current' / 'kernel.sqlite3'
    kernel = create_kernel(database, spec, requirements=source)
    before = read(database, 'settings', expected_run=spec.run_id).data
    monkeypatch.setattr(selection, 'managed_repository_routes', lambda request: (RepositoryRoute(database, root, spec.repository),))
    monkeypatch.setattr(profile_cli, 'workspace_for_root', lambda path:
        SimpleNamespace(state_root=database.parent, repository_root=root))
    request = parsed(['configure', spec.run_id, '--repo', spec.repository,
        '--development-model', 'explicit-current-model', '--json'])
    assert profile_cli.execute(request) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['run_id'] == spec.run_id and output['profile_revision'] == 2
    after = read(database, 'settings', expected_run=spec.run_id).data
    assert before['profile']['profiles']['development']['model'] != after['profile']['profiles']['development']['model']
    assert after['profile']['profiles']['development']['model'] == 'explicit-current-model'
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
        assert tx.one('SELECT count(*) FROM hosts')[0] == 0


def test_run_listing_has_an_incremental_response_budget_and_never_writes(repository, monkeypatch):
    before = dump(repository.database)
    monkeypatch.setattr(cli_main, 'QUERY_LIMIT', 1)
    with pytest.raises(QueryError) as limited:
        cli_main._repository_query(parsed(['runs', '--repo', repository.repository]), (repository,), 'en')
    assert limited.value.code == 'query_limit' and dump(repository.database) == before


@pytest.mark.parametrize('entry', ['old-run', 'kernel.sqlite3-wal', 'kernel.sqlite3-shm', 'raw', 'notes.md'])
def test_occupied_root_is_refused_before_creating_a_canonical_database_or_decoding_anything(tmp_path, monkeypatch, entry):
    state = tmp_path / 'state'
    state.mkdir()
    path = state / entry
    if entry in {'old-run', 'raw'}:
        path.mkdir()
        path = path / 'opaque.bin'
    path.write_bytes(b'Original opaque data\0\xff; no decoder or overwrite is permitted')
    original = path.read_bytes(), path.stat().st_ino, path.stat().st_mode, path.stat().st_mtime_ns
    route = RepositoryRoute(state / 'kernel.sqlite3', tmp_path / 'repository', 'example/project')
    def forbidden(*args, **kwargs):
        raise AssertionError('Occupied state reached SQLite/create before refusal')
    monkeypatch.setattr(Storage, 'create', forbidden)
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    for attempt in (route.initialize, route.metadata, lambda: RepositoryRoute.from_database(route.database)):
        with pytest.raises(UnsupportedState) as refused:
            attempt()
        assert refused.value.reason == 'repository_state_occupied'
    assert not route.database.exists()
    assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mode, path.stat().st_mtime_ns) == original


def test_real_current_database_uncheckpointed_wal_materials_and_budget_survive_new_layout_refusal(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    root = tmp_path / 'repository'
    spec, source = proposal(root, 'old-current')
    database = state / spec.run_id / 'kernel.sqlite3'
    old = create_kernel(database, spec, requirements=source)
    raw = database.parent / 'raw.bin'
    raw.write_bytes(b'Exact old raw bytes\0\xff')
    with closing(sqlite3.connect(database, isolation_level=None)) as keeper:
        # BEGIN is deferred: an actual read pins the pre-Stop snapshot so the
        # writer's committed WAL cannot disappear on its final close.
        keeper.execute('BEGIN')
        assert keeper.execute('SELECT id FROM runs').fetchall() == [(spec.run_id,)]
        assert keeper.in_transaction
        old.stop_task('old-stop')
        wal = database.with_name(database.name + '-wal')
        assert wal.is_file() and wal.stat().st_size > 0
        before = {path: path.read_bytes() for path in (database, wal, raw)}
        budget = keeper.execute('SELECT * FROM budgets ORDER BY subject,purpose,window').fetchall()
        route = RepositoryRoute(state / 'kernel.sqlite3', root, 'example/project')
        def forbidden(*args, **kwargs):
            raise AssertionError('Old managed state was opened or overwritten by fresh initialization')
        with monkeypatch.context() as patch:
            patch.setattr(Storage, 'create', forbidden)
            patch.setattr(sqlite3, 'connect', forbidden)
            for attempt in (route.initialize, route.metadata):
                with pytest.raises(UnsupportedState) as refused:
                    attempt()
                assert refused.value.reason == 'repository_state_occupied'
        assert {path: path.read_bytes() for path in before} == before
        assert keeper.execute('SELECT * FROM budgets ORDER BY subject,purpose,window').fetchall() == budget
        assert not route.database.exists()


@pytest.mark.parametrize('protocol', ['sqlite-work-records-v6', 'independent-delivery-kernel-v20', 'independent-delivery-kernel-v21', 'independent-delivery-repository-unactivated', 'unknown-format'])
def test_existing_canonical_old_protocol_is_refused_readonly_without_interpreting_or_replacing_its_data(tmp_path, protocol):
    state = tmp_path / 'state'
    state.mkdir()
    database = state / 'kernel.sqlite3'
    with closing(sqlite3.connect(database, isolation_level=None)) as keeper:
        keeper.execute('PRAGMA journal_mode=WAL')
        if protocol == 'sqlite-work-records-v6':
            keeper.execute('CREATE TABLE schema_meta(key TEXT,value TEXT)')
            keeper.execute('INSERT INTO schema_meta VALUES (?,?)', ('protocol', protocol))
        else:
            keeper.execute('CREATE TABLE meta(protocol TEXT)')
            keeper.execute('INSERT INTO meta VALUES (?)', (protocol,))
        keeper.execute('CREATE TABLE opaque_old_budget(balance INTEGER,raw BLOB)')
        keeper.execute('INSERT INTO opaque_old_budget VALUES (?,?)', (7, b'Original old raw\0\xff'))
        wal = database.with_name(database.name + '-wal')
        assert wal.is_file() and wal.stat().st_size > 0
        before = database.read_bytes(), wal.read_bytes()
        route = RepositoryRoute(database, tmp_path / 'repository', 'example/project')
        with pytest.raises(UnsupportedState):
            route.metadata()
        with pytest.raises(UnsupportedState):
            RepositoryRoute.from_database(database)
        with pytest.raises(UnsupportedState) as refused:
            route.initialize()
        assert refused.value.reason == 'repository_state_occupied'
        assert (database.read_bytes(), wal.read_bytes()) == before
        assert keeper.execute('SELECT * FROM opaque_old_budget').fetchall() == [(7, b'Original old raw\0\xff')]


def test_failed_root_inspection_refuses_before_any_directory_or_database_write(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    state.mkdir()
    route = RepositoryRoute(state / 'kernel.sqlite3', tmp_path / 'repository', 'example/project')
    original_scandir = routing.os.scandir
    def inaccessible(path):
        if Path(path) == state:
            raise PermissionError('Controlled state inspection failure')
        return original_scandir(path)
    def forbidden(*args, **kwargs):
        raise AssertionError('Uncertain root inspection reached create')
    monkeypatch.setattr(routing.os, 'scandir', inaccessible)
    monkeypatch.setattr(Storage, 'create', forbidden)
    with pytest.raises(UnsupportedState) as refused:
        route.initialize()
    assert refused.value.reason == 'unreadable_state' and not route.database.exists()


@pytest.mark.parametrize('committed', [False, True])
def test_partial_fresh_initialization_is_preserved_and_never_automatically_recreated(tmp_path, monkeypatch, committed):
    route = RepositoryRoute(tmp_path / 'state' / 'kernel.sqlite3', tmp_path / 'repository', 'example/project')
    original = sqlite3.connect
    fired = []
    class Interrupted(sqlite3.Connection):
        def commit(self):
            if not fired:
                fired.append(True)
                if committed:
                    super().commit()
                raise OSError('Controlled original fresh initialization failure')
            return super().commit()
    def connect(*args, **kwargs):
        kwargs['factory'] = Interrupted
        return original(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, 'connect', connect)
        with pytest.raises(OSError):
            route.initialize()
    assert fired == [True] and route.database.is_file()
    before = route.database.read_bytes()
    with pytest.raises(UnsupportedState) as refused:
        route.initialize()
    assert refused.value.reason == 'repository_state_occupied'
    assert route.database.read_bytes() == before
    if committed:
        with route.metadata().transaction(write=False) as owner:
            assert owner.one('SELECT count(*) FROM runs')[0] == 0
            assert owner.one('SELECT count(*) FROM tasks')[0] == 0
    else:
        with pytest.raises(UnsupportedState):
            route.metadata()
    assert route.database.read_bytes() == before
