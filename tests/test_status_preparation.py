"""Status protocol with explicit projection inputs, not Core/Host acceptance."""
from contextlib import contextmanager
from dataclasses import replace
import os
from threading import Event
from types import SimpleNamespace

import pytest

from agent_run.contracts.status_comment import StatusProjection, StatusTarget
from agent_run.errors import KernelError
from agent_run.notifications import sender as native, status_comment as status
from agent_run.storage import notification_outbox as outbox
from agent_run.runtime.process_identity import _process_start_token
from support.status_comments import CommentsAPI


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    target = StatusTarget('example/project', 'frozen-run', 1, 'parent:1', 1, 1, 7,
        'offline-publisher', 'publication', 'receipt', 'refs/heads/work', 'refs/heads/main')
    current, reads = [StatusProjection(target, 'original')], []
    database = tmp_path / 'kernel.sqlite3'
    owner = status.StatusComments(database, storage=SimpleNamespace(path=database, run_id=target.run_id))
    def projection(subject):
        assert subject == target.subject
        reads.append(current[0])
        return current[0]
    binding = [SimpleNamespace(repository_root=tmp_path, repository=target.repository,
        inputs=SimpleNamespace(runner_identity={'source': 'host'}))]
    monkeypatch.setattr(owner, '_projection', projection)
    monkeypatch.setattr(owner, '_kernel', lambda: SimpleNamespace(operation_binding=lambda: binding[0]))
    return SimpleNamespace(owner=owner, current=current, reads=reads, binding=binding,
        api=CommentsAPI(target), target=target, root=tmp_path, subject=target.subject)


def entry(fixture):
    document = outbox.read_notifications(fixture.root, fixture.target.run_id)
    return document['remote_status']['bindings'][fixture.target.key]


def test_initial_projection_and_atomic_claim_do_not_replace_fresh_admit(prepared, monkeypatch):
    f = prepared
    transaction, records = outbox.transaction, []
    @contextmanager
    def recorded(*args):
        with transaction(*args) as document:
            yield document
            records.append(document.get('remote_status', {}).get('bindings', {}).get(f.target.key, {}).copy())
    monkeypatch.setattr(outbox, 'transaction', recorded)
    result, fingerprint = f.owner.deliver_current(f.subject, api=f.api)
    assert result == 'confirmed' and len(f.reads) == 2
    assert len(records) == 4  # prepare+claim, sent, receipt, claim release
    assert records[0]['intent']['sent'] is False and 'claim' in records[0]
    assert records[1]['intent']['sent'] is True
    assert entry(f)['intent'] is None and 'claim' not in entry(f)
    assert len(f.api.commands) == 1
    f.reads.clear()
    assert f.owner.deliver_current(f.subject, previous=fingerprint, api=f.api) == ('unchanged', fingerprint)
    assert len(f.reads) == 1 and len(records) == 4 and len(f.api.commands) == 1


@pytest.mark.parametrize('change', ['body', 'generation', 'pr_number', 'actor', 'run_id', 'binding'])
def test_changes_after_projection_close_before_claim_cannot_dispatch(prepared, monkeypatch, change):
    f = prepared
    projection = f.owner._projection
    def changed(subject):
        result = projection(subject)
        if len(f.reads) == 1:
            if change == 'binding':
                f.binding[0] = SimpleNamespace(**vars(f.binding[0]))
                # Alter only after the initial binding has been captured.
            elif change == 'body':
                f.current[0] = replace(result, body='operator stopped')
            else:
                value = {'generation': 2, 'pr_number': 8, 'actor': 'other', 'run_id': 'other-run'}[change]
                f.current[0] = replace(result, target=replace(result.target, **{change: value}))
        return result
    monkeypatch.setattr(f.owner, '_projection', changed)
    if change == 'binding':
        original = f.api.object
        def read(endpoint):
            result = original(endpoint)
            f.binding[0] = SimpleNamespace(**{**vars(f.binding[0]), 'changed': True})
            return result
        monkeypatch.setattr(f.api, 'object', read)
    assert f.owner.deliver_current(f.subject, api=f.api)[0] == 'unavailable'
    assert not f.api.commands and entry(f)['intent']['sent'] is False and 'claim' not in entry(f)


@pytest.mark.parametrize('failure', [OSError('reader close'), KernelError('query_limit')])
def test_initial_projection_failure_cannot_prepare_or_claim(prepared, monkeypatch, failure):
    f = prepared
    def failed(subject):
        raise failure
    monkeypatch.setattr(f.owner, '_projection', failed)
    with pytest.raises(type(failure)) as caught:
        f.owner.deliver_current(f.subject, api=f.api)
    assert caught.value is failure and not f.api.commands
    assert not outbox.notification_path(f.root, f.target.run_id).exists()
    assert f.owner.deliver(f.subject, api=f.api) == 'unavailable'


def test_concurrent_confirmation_after_projection_is_no_work(prepared, monkeypatch):
    f = prepared
    project, invoked = f.owner._projection, []
    def original(subject):
        result = project(subject)
        if not invoked:
            invoked.append(True)
            with monkeypatch.context() as patch:
                patch.setattr(f.owner, '_projection', project)
                assert f.owner.deliver(subject, api=f.api) == 'confirmed'
        return result
    monkeypatch.setattr(f.owner, '_projection', original)
    assert f.owner.deliver_current(f.subject, api=f.api)[0] == 'unchanged'
    assert [method for _, method, _ in f.api.commands] == ['POST']


def test_unknown_sent_intent_survives_and_second_opportunity_prepares_again(prepared, monkeypatch):
    f = prepared
    f.api.lose = True
    assert f.owner.deliver(f.subject, api=f.api) == 'unavailable'
    original = entry(f)['intent'].copy()
    assert original['sent'] and len(f.api.commands) == 1
    f.current[0] = replace(f.current[0], body='intermediate')
    f.api.lose, f.api.hidden = False, True
    assert f.owner.deliver_current(f.subject, api=f.api)[0] == 'unknown'
    assert entry(f)['intent'] == original and len(f.api.commands) == 1
    f.api.hidden = False
    read, changed = f.api.object, []
    def advance(endpoint):
        result = read(endpoint)
        if not changed:
            changed.append(True)
            f.current[0] = replace(f.current[0], body='latest after remote read')
        return result
    monkeypatch.setattr(f.api, 'object', advance)
    monkeypatch.setattr(native, 'status_subjects', lambda *args, **kwargs: [f.subject])
    observer = SimpleNamespace(database=f.root / 'kernel.sqlite3', _storage_options={},
        status=f.owner, status_api=f.api, _status_seen={}, status_failure_reason=None)
    f.reads.clear()
    native.NativeNotifications._send_status(observer, SimpleNamespace(cancel=Event()))
    assert [projection.body for projection in f.reads] == ['intermediate', 'latest after remote read', 'latest after remote read']
    assert [method for _, method, _ in f.api.commands] == ['POST', 'PATCH']
    assert f.api.comments[0]['body'].endswith('latest after remote read')
    assert entry(f)['intent'] is None and 'claim' not in entry(f)


@pytest.mark.parametrize('stage', ['projection', 'claim', 'remote', 'admit'])
def test_all_preparation_stages_share_the_original_deadline(prepared, monkeypatch, stage):
    f = prepared
    now = [100.25]
    monkeypatch.setattr(status, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    project = f.owner._projection
    def projected(subject):
        result = project(subject)
        if (stage == 'projection' and len(f.reads) == 1) or (stage == 'admit' and len(f.reads) == 2):
            now[0] = 160.25
        return result
    monkeypatch.setattr(f.owner, '_projection', projected)
    transaction, commits = outbox.transaction, []
    @contextmanager
    def committed(*args):
        with transaction(*args) as document:
            yield document
        commits.append(True)
        if stage == 'claim' and len(commits) == 1:
            now[0] = 160.25
    monkeypatch.setattr(outbox, 'transaction', committed)
    read = f.api.object
    def remote(endpoint):
        result = read(endpoint)
        if stage == 'remote':
            now[0] = 160.25
        return result
    monkeypatch.setattr(f.api, 'object', remote)
    assert f.owner.deliver_current(f.subject, api=f.api)[0] == 'unavailable'
    assert now[0] == 160.25 and not f.api.commands
    assert entry(f)['intent']['sent'] is False and 'claim' not in entry(f)


def test_preparation_commit_unknown_never_reenters_or_regenerates_intent(prepared, monkeypatch):
    f = prepared
    transaction, prepared_intents = outbox.transaction, []
    @contextmanager
    def unknown(*args):
        with transaction(*args) as document:
            yield document
        if not prepared_intents:
            prepared_intents.append(entry(f)['intent'].copy())
            raise OSError('prepare acknowledgement unknown')
    with monkeypatch.context() as patch:
        patch.setattr(outbox, 'transaction', unknown)
        assert f.owner.deliver_current(f.subject, api=f.api)[0] == 'unavailable'
    assert not f.api.commands and entry(f)['intent'] == prepared_intents[0]
    assert 'claim' not in entry(f)
    assert f.owner.deliver(f.subject, api=f.api) == 'confirmed'


def test_standalone_stage_keeps_its_original_no_claim_semantics(prepared):
    f = prepared
    intent = f.owner.stage(f.subject)
    assert intent == entry(f)['intent'] and 'claim' not in entry(f)
    assert not f.api.commands and len(f.reads) == 1


def test_live_claim_is_not_replaced_by_a_changed_projection(prepared):
    f = prepared
    original = f.owner.stage(f.subject)
    claim = {'pid': os.getpid(), 'token': _process_start_token(os.getpid()), 'id': 'b' * 32}
    with outbox.transaction(f.root, f.target.run_id) as document:
        document['remote_status']['bindings'][f.target.key]['claim'] = claim
    f.current[0] = replace(f.current[0], body='new copy')
    assert f.owner.deliver_current(f.subject, api=f.api)[0] == 'busy'
    assert entry(f)['claim'] == claim and entry(f)['intent'] == original
    assert not f.api.commands


def test_concurrent_confirmation_before_claim_release_is_unchanged(prepared):
    f = prepared
    assert f.owner.deliver(f.subject, api=f.api) == 'confirmed'
    claim = {'pid': os.getpid(), 'token': _process_start_token(os.getpid()), 'id': 'c' * 32}
    with outbox.transaction(f.root, f.target.run_id) as document:
        document['remote_status']['bindings'][f.target.key]['claim'] = claim
    assert f.owner.deliver_current(f.subject, api=f.api)[0] == 'unchanged'
    assert entry(f)['claim'] == claim and entry(f)['intent'] is None
    assert len(f.api.commands) == 1


def test_cancellation_cannot_dispatch_or_leave_an_owned_claim(prepared):
    f = prepared
    assert f.owner.deliver_current(f.subject, api=f.api, cancelled=lambda: True)[0] == 'unavailable'
    assert not f.api.commands and entry(f)['intent']['sent'] is False
    assert 'claim' not in entry(f)


def test_failure_before_prepare_commit_cannot_reenter_or_call_remote(prepared, monkeypatch):
    f = prepared
    opened = []
    @contextmanager
    def failed(*args):
        opened.append(args)
        raise OSError('prepare did not commit')
        yield
    monkeypatch.setattr(outbox, 'transaction', failed)
    assert f.owner.deliver_current(f.subject, api=f.api)[0] == 'unavailable'
    assert len(opened) == 1 and not f.api.commands
    assert not outbox.notification_path(f.root, f.target.run_id).exists()


def test_settlement_and_cleanup_failures_keep_sent_identity_for_readback(prepared, monkeypatch):
    f = prepared
    transaction = outbox.transaction
    @contextmanager
    def unavailable_after_write(*args):
        if f.api.commands:
            raise OSError('journal unavailable after physical write')
        with transaction(*args) as document:
            yield document
    with monkeypatch.context() as patch:
        patch.setattr(outbox, 'transaction', unavailable_after_write)
        assert f.owner.deliver_current(f.subject, api=f.api)[0] == 'unavailable'
    original = entry(f)['intent'].copy()
    assert original['sent'] and 'claim' in entry(f) and len(f.api.commands) == 1
    # An explicit controlled absence proof permits recovery; this is not an OS
    # process-exit test and never treats the still-live test PID as actually gone.
    monkeypatch.setattr(status, '_process_binding_status', lambda *args: 'absent')
    assert f.owner.deliver(f.subject, api=f.api) == 'confirmed'
    assert len(f.api.commands) == 1 and entry(f)['receipt']['intent'] == original['id']
    assert entry(f)['intent'] is None and 'claim' not in entry(f)
