"""Reuse immutable complete objects only inside one fresh delivery read."""
from collections import Counter
from dataclasses import replace
import json

import pytest

from agent_run.contracts.write_authority import WriteAuthority
from agent_run.core.operation_contract import DeliveryRead, ReadUnavailable
from agent_run.errors import GitHubReadError
from support.remote_effects import _fixture, _publish_pr


def _reads(path, start=0):
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    return [json.loads(line) for line in lines[start:]]


def _objects(calls):
    return Counter(command[1].rsplit('/', 1)[1] for command in calls
        if command[:1] == ['api'] and '/git/commits/' in command[1])


def test_one_delivery_reads_each_complete_object_once_but_keeps_live_pr_checks(tmp_path, monkeypatch):
    port, _git, push, _state, calls = _fixture(tmp_path, monkeypatch)
    _original, pr = _publish_pr(port, push)
    start = len(_reads(calls))
    facts = port.delivery(DeliveryRead(push.source_ref, push.target_ref, pr.number, ()))
    observed = _reads(calls, start)
    assert facts.source == pr.head and facts.target == pr.base and facts.pr == pr
    assert facts.checks is not None and facts.checks.result == 'none'
    assert _objects(observed) == {pr.head.sha: 1, pr.base.sha: 1}
    assert sum(c[:2] == ['api', 'repos/example/project/pulls/17'] for c in observed) == 3
    assert sum(c[:2] == ['repo', 'view'] for c in observed) == 1
    assert not any('--method' in command for command in observed)
    assert len(observed) == 11


def test_merged_pr_retains_its_independent_first_parent_receipt_read(tmp_path, monkeypatch):
    port, _git, push, _state, calls = _fixture(tmp_path, monkeypatch)
    _original, pr = _publish_pr(port, push)
    merge = replace(push, effect_id='original-merge', operation='merge', pr_number=pr.number,
        payload={'method': 'merge', 'parents': [push.base.sha, push.after_sha],
                 'commit_message': 'Original complete object'})
    result = port.execute(merge, WriteAuthority(lambda: lambda: None))
    assert result.disposition == 'target' and result.commit is not None
    start = len(_reads(calls))
    facts = port.delivery(DeliveryRead(push.source_ref, push.target_ref, pr.number, ()))
    assert facts.target == result.commit and facts.source == pr.head
    assert facts.pr is not None and facts.pr.state == 'MERGED'
    assert facts.pr.base == push.base and facts.pr.merged == result.commit
    assert facts.checks is None
    observed = _reads(calls, start)
    assert _objects(observed) == {pr.head.sha: 1, push.base.sha: 2, result.commit.sha: 2}
    assert not any('--method' in command for command in observed)


def test_next_observation_rereads_objects_refs_pr_and_actor(tmp_path, monkeypatch):
    port, git, push, state, calls = _fixture(tmp_path, monkeypatch)
    _original, pr = _publish_pr(port, push)
    request = DeliveryRead(push.source_ref, push.target_ref, pr.number, ())
    first = port.delivery(request)
    later = git.make_commit(pr.head.tree, (pr.head.sha,), 'New source object')
    git.run('push', 'origin', later.sha + ':' + push.source_ref)
    document = json.loads(state.read_text())
    document['prs'][0]['head']['sha'] = later.sha
    document['prs'][0]['title'] = 'Fresh PR title'
    document['actor'] = 'fresh-reader'
    state.write_text(json.dumps(document))
    start = len(_reads(calls))
    second = port.delivery(request)
    assert first.source == pr.head and first.pr.title == pr.title and first.publisher_actor == 'publisher'
    assert second.source == later and second.pr.head == later and second.pr.title == 'Fresh PR title'
    assert second.publisher_actor == 'fresh-reader'
    assert _objects(_reads(calls, start)) == {later.sha: 1, pr.base.sha: 1}


@pytest.mark.parametrize('fault', ['foreign-sha', 'missing-sha', 'invalid-tree', 'missing-tree', 'invalid-parent', 'unavailable'])
def test_completed_read_never_hides_a_later_invalid_or_unavailable_object(tmp_path, monkeypatch, fault):
    port, _git, push, _state, calls = _fixture(tmp_path, monkeypatch)
    _original, pr = _publish_pr(port, push)
    request = DeliveryRead(push.source_ref, push.target_ref, pr.number, ())
    assert port.delivery(request).source == pr.head
    read = port._api
    seen = []
    def broken(endpoint, *args, **kwargs):
        data = read(endpoint, *args, **kwargs)
        if endpoint.endswith('/git/commits/' + pr.head.sha):
            seen.append(endpoint)
            if fault == 'foreign-sha':
                data['sha'] = pr.base.sha
            elif fault == 'missing-sha':
                data.pop('sha')
            elif fault == 'invalid-tree':
                data['tree']['sha'] = 'HEAD'
            elif fault == 'missing-tree':
                data.pop('tree')
            elif fault == 'invalid-parent':
                data['parents'] = [{'sha': pr.base.sha[:8]}]
            else:
                raise GitHubReadError('github_read_failed', 'controlled unavailable object')
        return data
    start = len(_reads(calls))
    with monkeypatch.context() as damaged:
        damaged.setattr(port, '_api', broken)
        with pytest.raises((ValueError, KeyError, ReadUnavailable)):
            port.delivery(request)
    assert len(seen) == 1 and not any('--method' in c for c in _reads(calls, start))
    start = len(_reads(calls))
    assert port.delivery(request).source == pr.head
    assert _objects(_reads(calls, start)) == {pr.head.sha: 1, pr.base.sha: 1}


@pytest.mark.parametrize('replacement', ['different-object', 'short-sha', 'ref-name'])
def test_pr_head_cannot_rebind_or_alias_an_object_read_from_the_source_ref(tmp_path, monkeypatch, replacement):
    port, git, push, state, calls = _fixture(tmp_path, monkeypatch)
    _original, pr = _publish_pr(port, push)
    other = git.make_commit(pr.head.tree, (pr.head.sha,), 'Unselected object')
    git.run('push', 'origin', other.sha + ':refs/heads/unselected')
    document = json.loads(state.read_text())
    value = {'different-object': other.sha, 'short-sha': pr.head.sha[:8], 'ref-name': 'refs/heads/delivery'}[replacement]
    document['prs'][0]['head']['sha'] = value
    state.write_text(json.dumps(document))
    start = len(_reads(calls))
    with pytest.raises((ValueError, ReadUnavailable)):
        port.delivery(DeliveryRead(push.source_ref, push.target_ref, pr.number, ()))
    observed = _reads(calls, start)
    assert not any('--method' in c for c in observed)
    if replacement == 'different-object':
        assert _objects(observed)[other.sha] == 1
    else:
        assert all(c[1] != 'repos/example/project/git/commits/' + value for c in observed)


def test_write_readback_uses_its_own_fresh_commit_reads_after_delivery(tmp_path, monkeypatch):
    port, _git, push, _state, calls = _fixture(tmp_path, monkeypatch)
    original, pr = _publish_pr(port, push)
    assert port.delivery(DeliveryRead(push.source_ref, push.target_ref, pr.number, ())).source == pr.head
    start = len(_reads(calls))
    observed = port.reconcile(original)
    assert observed.disposition == 'target' and observed.pr == pr
    reads = _reads(calls, start)
    assert _objects(reads) == {pr.head.sha: 1, pr.base.sha: 1}
    assert not any('--method' in c for c in reads)


@pytest.mark.parametrize('which', ['source', 'target'])
def test_same_sha_changed_api_tree_cannot_be_hidden_by_reuse(tmp_path, monkeypatch, which):
    port, _git, push, _state, calls = _fixture(tmp_path, monkeypatch)
    _original, pr = _publish_pr(port, push)
    selected, wrong = (pr.head, pr.base) if which == 'source' else (pr.base, pr.head)
    assert selected.tree != wrong.tree
    read, seen = port._api, []
    def inconsistent(endpoint, *args, **kwargs):
        data = read(endpoint, *args, **kwargs)
        if endpoint.endswith('/git/commits/' + selected.sha):
            seen.append(endpoint)
            if len(seen) == 1:
                data['tree']['sha'] = wrong.tree
        return data
    start = len(_reads(calls))
    with monkeypatch.context() as changed:
        changed.setattr(port, '_api', inconsistent)
        with pytest.raises(ReadUnavailable, match='Commit object and API tree do not agree'):
            port.delivery(DeliveryRead(push.source_ref, push.target_ref, pr.number, ()))
    assert len(seen) == 1
    assert not any('--method' in c for c in _reads(calls, start))
    assert port.delivery(DeliveryRead(push.source_ref, push.target_ref, pr.number, ())).pr == pr


def test_exact_tree_reuse_preserves_a_legal_shallow_commit(tmp_path, monkeypatch):
    port, git, push, _state, calls = _fixture(tmp_path, monkeypatch)
    git.run('push', 'origin', push.after_sha + ':' + push.source_ref,
            push.after_sha + ':' + push.target_ref)
    (git.root / '.git' / 'shallow').write_text(push.after_sha + '\n')
    assert git.parents(push.after_sha) == ()
    start = len(_reads(calls))
    facts = port.delivery(DeliveryRead(push.source_ref, push.target_ref, None, (), read_pr=False))
    assert facts.source == facts.target == push.candidate
    assert facts.ancestry == ((push.after_sha, push.after_sha),)
    assert _objects(_reads(calls, start)) == {push.after_sha: 1}
    assert (git.root / '.git' / 'shallow').read_text() == push.after_sha + '\n'


def test_absent_refs_do_not_read_a_default_local_head(tmp_path, monkeypatch):
    port, _git, _push, _state, calls = _fixture(tmp_path, monkeypatch)
    seen, run = [], port.git.run
    def record(*args, **kwargs):
        seen.append(args)
        return run(*args, **kwargs)
    monkeypatch.setattr(port.git, 'run', record)
    start = len(_reads(calls))
    facts = port.delivery(DeliveryRead('refs/heads/absent-source', 'refs/heads/absent-target', None, (), read_pr=False))
    assert facts.source is None and facts.target is None and facts.pr is None and not facts.ancestry
    assert not seen and not _objects(_reads(calls, start))


def test_delivery_cache_cannot_hide_a_malformed_write_readback(tmp_path, monkeypatch):
    port, _git, push, _state, calls = _fixture(tmp_path, monkeypatch)
    original, pr = _publish_pr(port, push)
    assert port.delivery(DeliveryRead(push.source_ref, push.target_ref, pr.number, ())).pr == pr
    read = port._api
    def malformed(endpoint, *args, **kwargs):
        data = read(endpoint, *args, **kwargs)
        if endpoint.endswith('/git/commits/' + pr.base.sha):
            data['sha'] = pr.head.sha
        return data
    start = len(_reads(calls))
    monkeypatch.setattr(port, '_api', malformed)
    observed = port.reconcile(original)
    assert observed.disposition == 'unknown'
    assert observed.raw['diagnostic'] == 'foreign commit response'
    assert _objects(_reads(calls, start)) == {pr.base.sha: 1}
    assert not any('--method' in c for c in _reads(calls, start))


@pytest.mark.parametrize('presentation_config', [False, True])
def test_reused_commit_does_not_require_its_tree_object_to_be_downloaded(tmp_path, monkeypatch, presentation_config):
    port, git, push, _state, _calls = _fixture(tmp_path, monkeypatch)
    if presentation_config:
        git.run('config', 'i18n.logOutputEncoding', 'UTF-16')
        git.run('config', 'log.mailmap', 'true')
        git.run('config', 'mailmap.blob', 'f' * 40)
        git.run('config', 'log.showSignature', 'true')
    # A partial repository may own the commit header without its tree object.
    # The independent bare remote retains the complete object throughout.
    tree = git.root / '.git' / 'objects' / push.base.tree[:2] / push.base.tree[2:]
    retained = tmp_path / 'retained-private-tree'
    assert tree.is_file() and not tree.is_symlink()
    tree.rename(retained)
    try:
        assert git.run('cat-file', '-e', push.base.sha + '^{commit}', check=False).returncode == 0
        assert git.run('cat-file', '-e', push.base.tree, check=False).returncode != 0
        facts = port.delivery(DeliveryRead(push.source_ref, push.target_ref, None, (), read_pr=False))
        assert facts.source == facts.target == push.base
        assert facts.ancestry == ((push.base.sha, push.base.sha),)
        assert not tree.exists()  # No implicit tree retrieval was required.
    finally:
        retained.rename(tree)
