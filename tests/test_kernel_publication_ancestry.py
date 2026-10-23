"""Publishing an integrated tree must retain the remote target's ancestry."""
from pathlib import Path

import pytest

from agent_run_kernel import DeliveryKind
from support.offline import Scenario


@pytest.mark.parametrize('kind', [DeliveryKind.TICKET, DeliveryKind.PARENT_ONLY])
def test_conflict_repair_keeps_target_ancestry_through_publication_and_real_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: DeliveryKind,
) -> None:
    scenario = Scenario(tmp_path / 'scenario', kind,
        edits=({'shared.txt': 'Candidate content\n'}, {'shared.txt': 'Resolved complete content\n'}))
    for _ in range(30):
        if scenario.kernel.status(scenario.delivery.key)['phase'] == 'integration':
            break
        scenario.step()
    else:
        raise AssertionError('The original requirements review did not finish')
    external = scenario.port.external_remote_append(scenario.delivery.target_ref,
        {'shared.txt': 'External content\n'}, 'conflict')
    publications = []
    execute = scenario.port.execute

    def record(effect, authorize):
        if effect.operation == 'publication_commit':
            # This is the actual candidate selected by Core. The independent
            # external writer's SHA is the expected ancestor, not Core's tree.
            assert scenario.port.git.is_ancestor(external.sha, effect.payload['integrated_sha'])
        result = execute(effect, authorize)
        if effect.operation == 'publication_commit':
            publications.append(result.commit)
        return result

    monkeypatch.setattr(scenario.port, 'execute', record)
    assert scenario.run(approve=True).kind == 'complete'
    published, = publications
    assert published is not None and scenario.port.git.is_ancestor(external.sha, published.sha)
    pr = scenario.port.pull_request_by_ref(scenario.delivery.source_ref, scenario.delivery.target_ref)
    assert pr is not None and pr.state == 'MERGED' and pr.head == published
    merged = scenario.port.remote_commit(scenario.delivery.target_ref)
    assert merged is not None and pr.merged == merged
    assert scenario.port.git.run('show', f'{merged.sha}:shared.txt').stdout == 'Resolved complete content\n'
    assert scenario.port.git.parents(merged.sha) == (
        (external.sha,) if kind == DeliveryKind.TICKET else (external.sha, published.sha))
    requests = scenario.port.objects.snapshot()['api_requests']
    assert sum(row['endpoint'].endswith('/merge') for row in requests) == 1
    assert sum(write['operation'] == 'pr_write' for write in scenario.port.writes()) == 1
    integration = [call for call in scenario.role_calls if call['role'] == 'development' and call['purpose'] == 'integration']
    assert len(integration) == 1
    budgets = scenario.kernel.status(scenario.delivery.key)['budgets']
    assert budgets['requirements']['developments_used'] == budgets['requirements']['reviews_used'] == 1
    assert budgets['integration']['developments_used'] == 1
    assert budgets['integration']['reviews_used'] == (0 if kind == DeliveryKind.TICKET else 1)
    assert scenario.kernel.status(scenario.delivery.key)['resource_cleanup']['state'] == 'complete'
