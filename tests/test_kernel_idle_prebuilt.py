"""Normal private idle targets: precreated and proved absent share one clone."""
from dataclasses import replace
import json
import pytest

from agent_run_kernel.effects import LocalGit
from agent_run_kernel.migration import capture_v6
from agent_run_kernel.migration_conversion import read_initial_import
from agent_run_kernel.migration_run_install import install_imported_facts
from agent_run_kernel.migration_run_ownership import prebuilt_workspace_binding, authenticate_prebuilt_workspace
from agent_run_kernel.model import Effect, Execution, KernelError, Profile
from support.frozen_v6_runtime import old_v6_runtime


@pytest.fixture
def idle_copy(tmp_path, git_repo, monkeypatch, request):
    monkeypatch.setenv('PYTHONHASHSEED', '0')
    old = old_v6_runtime(tmp_path / 'unchanged-old')
    root = git_repo / '.agent-run'
    numbers = getattr(request, 'param', (2, 3))
    fixture = old('fixture', root=root, fixture_path=git_repo / 'github.json', issues={str(n): {
        'number': n, 'title': f'Ticket {n}', 'body': f'Deliver {n}', 'state': 'OPEN',
        'labels': ['ready-for-agent'], 'blocked_by': []} for n in numbers})
    state = old('start', root=root, git=git_repo, fixture=fixture)['state']
    snapshot = capture_v6(root / 'state.sqlite3', tmp_path / 'sealed.sqlite3')
    private = tmp_path / 'private'
    private.mkdir()
    repo = private / 'authority'
    LocalGit(git_repo).run('clone', '--no-hardlinks', str(git_repo), str(repo))
    git = LocalGit(repo)
    git.run('config', 'user.name', 'Synthetic idle test')
    git.run('config', 'user.email', 'idle@example.invalid')
    git.run('update-ref', 'refs/heads/' + state['run_branch'], state['base']['sha'])
    work = private / 'work'
    work.mkdir()
    options = dict(repository_root=repo, source_ref='refs/heads/idle-2', checkout=work / 'ticket-2',
        final_checkout=work / 'final', development_profile=Profile('dev', 'high'),
        review_profile=Profile('review', 'high'), publication_profile=Profile('publication', 'high'),
        additional_workspaces={n: (f'refs/heads/idle-{n}', work / f'ticket-{n}') for n in numbers if n != 2})
    return snapshot, options, git, private / 'kernel.sqlite3', state['base']['sha']


def test_decoder_accepts_only_proved_unregistered_absence_for_lazy_targets(idle_copy):
    snapshot, options, git, target, base = idle_copy
    facts = read_initial_import(snapshot, **options)
    assert len(facts.subjects) == 2
    for subject in facts.subjects:
        resource = subject.resources[0]
        assert resource.owner.table == 'run_graph_members'
        assert resource.before_head is None and resource.checkout_status is None
        assert resource.workspace_oid == base and resource.expected_oid is None
        assert not resource.checkout.exists()
    assert not target.exists()


def _binding(kernel, subject):
    with kernel.storage.transaction(write=False) as tx:
        return prebuilt_workspace_binding(kernel.storage, tx, tx.one('SELECT * FROM runs'),
            tx.one('SELECT * FROM subjects WHERE id=?', (subject,)))


def _effect(kernel, git, subject, head):
    binding = _binding(kernel, subject)
    assert binding is not None
    payload = {key: binding[key] for key in ('checkout', 'owned_root', 'common_git_dir')}
    return Effect('readonly-probe', subject, 'workspace', 'example/project', binding['source_ref'],
        'unused-target', head, None, None, git.commit(head), None, None,
        {**payload, 'create_ref': True, 'prebuilt_workspace': binding}, Execution(binding['run_id'], 'probe', 1))


@pytest.mark.parametrize('prebuilt', [False, True])
def test_idle_shared_marker_binds_real_graph_members_and_reads_without_git(idle_copy, monkeypatch, prebuilt):
    snapshot, options, git, target, base = idle_copy
    if prebuilt:
        for checkout in (options['checkout'], options['additional_workspaces'][3][1]):
            git.run('worktree', 'add', '--detach', str(checkout), base)
    before = snapshot.source.read_bytes(), snapshot.path.read_bytes()
    facts = read_initial_import(snapshot, **options)
    kernel = install_imported_facts(facts, target)
    marker = (git.root / '.git/agent-run-checkout-id').read_text().strip()
    def forbidden(*args, **kwargs):
        raise AssertionError('Core binding must not read Git')
    with monkeypatch.context() as context:
        context.setattr(LocalGit, 'run', forbidden)
        bindings = [_binding(kernel, f'ticket:{number}') for number in (2, 3)]
    assert {binding['copy_uuid'] for binding in bindings} == {marker}
    assert [binding['source_record']['key'][2] for binding in bindings] == [2, 3]
    assert all(binding['source_record']['table'] == 'run_graph_members' for binding in bindings)
    assert all(binding['before_head'] == (base + '\n' if prebuilt else None) for binding in bindings)
    with kernel.storage.transaction(write=False) as tx:
        assert tx.rows('SELECT id FROM attempts') == []
        assert tx.rows('SELECT id FROM invocations') == []
        identity = tx.one("SELECT id,subject FROM material WHERE purpose='v6_import_identity'")
        proof = json.loads(tx.get(identity['id'], identity['subject']))['local_resources']
        assert proof['format'] == 'copied-idle-resources/1'
        assert len(proof['resources']) == 2
    for number in (2, 3):
        effect = _effect(kernel, git, f'ticket:{number}', base)
        actual = authenticate_prebuilt_workspace(git, effect)
        assert actual == effect.payload['prebuilt_workspace']
        assert actual is not effect.payload['prebuilt_workspace']
    assert (snapshot.source.read_bytes(), snapshot.path.read_bytes()) == before


@pytest.mark.parametrize('prebuilt', [False, True])
def test_physical_authentication_keeps_h0_outside_h1_and_allows_only_normal_current_states(idle_copy, prebuilt):
    snapshot, options, git, target, base = idle_copy
    checkout = options['checkout']
    if prebuilt:
        git.run('worktree', 'add', '--detach', str(checkout), base)
    kernel = install_imported_facts(read_initial_import(snapshot, **options), target)
    tree = git.commit(base).tree
    h1 = git.run('commit-tree', tree, '-p', base, input_text='Native dependency completion\n').stdout.strip()
    effect = _effect(kernel, git, 'ticket:2', h1)
    assert effect.before_sha == h1 != effect.payload['prebuilt_workspace']['workspace_oid']
    assert authenticate_prebuilt_workspace(git, effect) is not None
    # Simulate normal existing dispatch's complete target; helper does not write.
    git.run('update-ref', effect.source_ref, h1)
    if prebuilt:
        git.run('reset', '--hard', h1, cwd=checkout)
    else:
        git.run('worktree', 'add', '--detach', str(checkout), h1)
    assert authenticate_prebuilt_workspace(git, effect) is not None
    # Actual dirty state is foreign even when HEAD/ref match H1.
    (checkout / 'dirty').write_text('uncommitted')
    with pytest.raises(KernelError, match='foreign_local_checkout'):
        authenticate_prebuilt_workspace(git, effect)


@pytest.mark.parametrize('layout', ['broken-symlink', 'prunable', 'foreign-directory'])
def test_decoder_never_converts_unavailable_or_foreign_checkout_to_lazy(idle_copy, layout):
    snapshot, options, git, target, base = idle_copy
    checkout = options['checkout']
    if layout == 'broken-symlink':
        checkout.symlink_to(checkout.parent / 'missing')
    elif layout == 'prunable':
        import shutil
        git.run('worktree', 'add', '--detach', str(checkout), base)
        shutil.rmtree(checkout)
    else:
        checkout.mkdir()
    with pytest.raises(ValueError, match='foreign_local_checkout'):
        read_initial_import(snapshot, **options)
    assert not target.exists()


def test_private_idle_marker_publication_rolls_back_only_its_own_new_marker(idle_copy, monkeypatch):
    import agent_run_kernel.migration_run_install as install
    snapshot, options, git, target, base = idle_copy
    facts = read_initial_import(snapshot, **options)
    def fail(*args):
        raise OSError('synthetic publication failure')
    monkeypatch.setattr(install.os, 'link', fail)
    with pytest.raises(OSError, match='synthetic publication'):
        install_imported_facts(facts, target)
    assert not target.exists()
    assert not (git.root / '.git/agent-run-checkout-id').exists()
    assert not options['checkout'].exists()


def test_physical_read_error_is_not_an_absent_lazy_checkout(idle_copy, monkeypatch):
    snapshot, options, git, target, base = idle_copy
    kernel = install_imported_facts(read_initial_import(snapshot, **options), target)
    effect = _effect(kernel, git, 'ticket:2', base)
    original = git.run
    def fail(*args, **kwargs):
        if args[:2] == ('worktree', 'list'):
            raise KernelError('git_operation_failed', 'synthetic read failure')
        return original(*args, **kwargs)
    monkeypatch.setattr(git, 'run', fail)
    with pytest.raises(KernelError, match='local_read_failed'):
        authenticate_prebuilt_workspace(git, effect)


def test_missing_optional_binding_is_distinct_from_present_invalid_value(idle_copy):
    snapshot, options, git, target, base = idle_copy
    kernel = install_imported_facts(read_initial_import(snapshot, **options), target)
    effect = _effect(kernel, git, 'ticket:2', base)
    ordinary = replace(effect, payload={k: v for k, v in effect.payload.items() if k != 'prebuilt_workspace'})
    assert authenticate_prebuilt_workspace(git, ordinary) is None
    with pytest.raises(KernelError, match='foreign_local_checkout'):
        authenticate_prebuilt_workspace(git, replace(ordinary, payload={**ordinary.payload, 'prebuilt_workspace': None}))


def test_idle_install_preserves_a_foreign_existing_marker(idle_copy):
    snapshot, options, git, target, base = idle_copy
    facts = read_initial_import(snapshot, **options)
    marker = git.root / '.git/agent-run-checkout-id'
    original = b'foreign-clone-identity\n'
    marker.write_bytes(original)
    with pytest.raises(ValueError, match='copied_repository_identity_exists'):
        install_imported_facts(facts, target)
    assert marker.read_bytes() == original
    assert not target.exists()


@pytest.mark.parametrize('idle_copy', [(2,)], indirect=True)
def test_single_ticket_uses_its_real_member_without_fabricating_final_or_workjob(idle_copy):
    snapshot, options, git, target, base = idle_copy
    kernel = install_imported_facts(read_initial_import(snapshot, **options), target)
    binding = _binding(kernel, 'ticket:2')
    assert binding['source_record']['table'] == 'run_graph_members'
    assert binding['source_record']['key'][2] == 2
    assert binding['expected_oid'] is None
    with kernel.storage.transaction(write=False) as tx:
        assert [row['id'] for row in tx.rows('SELECT id FROM subjects')] == ['ticket:2']
    assert authenticate_prebuilt_workspace(git, _effect(kernel, git, 'ticket:2', base)) == binding
