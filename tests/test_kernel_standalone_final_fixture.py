"""A standalone Final observer starts from real, retained Completion history."""
import json
from pathlib import Path

from agent_run.model import DeliveryKind
from support.offline import Scenario


def test_standalone_final_repair_reopen_approval_and_close_preserve_the_original_story(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / 'final', DeliveryKind.FINAL_RUN, reviews=('fail', 'pass'))
    history = scenario.historical_trace
    assert history is not None
    assert history['raw_backend_calls'] == {'development': 1, 'review': 1, 'publication': 1}
    assert history['final']['phase'] == 'complete'
    assert history['completion']['subject'] == 'ticket:2'
    assert history['completion']['review_attempt'] is not None and history['completion']['pr_number'] == 1
    assert scenario.role_calls == []
    assert (scenario.development_count, scenario.review_count, scenario.publication_count) == (0, 0, 0)
    assert scenario.kernel.source_status()['final_subject'] == 'final:1'
    assert len(scenario.kernel.source_status()['completions']) == 1
    prefix_history = scenario.kernel.history('ticket:2')
    assert len([row for row in prefix_history if row['kind'] == 'agent_allocated']) == 3
    assert scenario.writes() == [] and scenario.port.writes() == history['external_writes']
    original_target = scenario.port.remote_commit(scenario.delivery.target_ref)
    assert scenario.run(cut='publication_pending').kind == 'publication_pending'
    assert [call['role'] for call in scenario.role_calls] == ['review', 'development', 'review']
    assert scenario.kernel.status('final:1')['budgets']['integration']['developments_used'] == 1
    assert not any(write['operation'] == 'pr_write' for write in scenario.writes())
    assert scenario.run().kind == 'approval'
    assert scenario.port.remote_commit(scenario.delivery.target_ref) == original_target
    assert scenario.port.issue_state(1) == 'OPEN'
    scenario.kernel.control('approve', 'standalone-final-approval', subject='final:1', world=scenario.world())
    assert scenario.run().kind == 'complete'
    assert [call['role'] for call in scenario.role_calls] == ['review', 'development', 'review', 'publication']
    assert [call['purpose'] for call in scenario.role_calls if call['role'] == 'development'] == ['integration']
    assert scenario.port.issue_state(1) == scenario.port.issue_state(2) == 'CLOSED'
    assert [write['operation'] for write in scenario.writes()].count('merge') == 1
    assert [write['operation'] for write in scenario.writes()].count('completion_comment') == 1
    assert [write['operation'] for write in scenario.writes()].count('close') == 1
    assert [write['operation'] for write in scenario.port.writes()].count('merge') == 2
    assert scenario.kernel.history('ticket:2') == prefix_history
    assert scenario.kernel.status('ticket:2')['budgets']['requirements']['developments_used'] == 1
    assert scenario.kernel.status('ticket:2')['budgets']['requirements']['reviews_used'] == 1
    assert scenario.kernel.status('final:1')['resource_cleanup']['state'] == 'complete'
    assert not Path(scenario.delivery.checkout).exists()
    assert scenario.port.remote_refs().keys() == {'refs/heads/main'}


def test_original_neutral_final_script_keeps_its_final_wire_and_literal_suffix_expectations(tmp_path: Path) -> None:
    script = json.loads((Path(__file__).parent / 'data/kernel/normal-final-v1.json').read_text())
    scenario = Scenario(tmp_path / 'neutral-final', DeliveryKind.FINAL_RUN, neutral_script=script)
    assert scenario.historical_trace is not None
    assert scenario.historical_trace['raw_backend_calls'] == {'development': 1, 'review': 1, 'publication': 1}
    assert scenario.historical_trace['final']['budgets']['requirements']['developments_used'] == 1
    assert scenario.historical_trace['final']['budgets']['requirements']['reviews_used'] == 1
    assert scenario.run().kind == 'approval'
    assert scenario.run(approve=True).kind == 'complete'
    assert [call['role'] for call in scenario.role_calls] == ['review', 'publication']
    observed = scenario.neutral_observation(script, 'original-literal-input')
    assert observed['raw_backend_calls'] == {'development': 0, 'review': 1, 'publication': 1}
    assert observed['budget'] == {'initial_development': 0, 'initial_review': 1}
    assert observed['publication_wire'] == script['agents']['publication'][0]
    assert observed['pull_request']['title'] == 'feat(run): publish the completed delivery'
    assert script['agents']['publication'][0]['pr_body_markdown'] in observed['pull_request']['body']
    assert observed['closed_issues'] == [1]
    assert all(write['subject'] == 'final:1' for write in observed['raw_effects'])
    assert observed['historical_source']['raw_backend_calls'] == {'development': 1, 'review': 1, 'publication': 1}
    assert observed['historical_input']['budget'] == {'development_attempts': 1, 'reviewer_invocations': 1}
    assert observed['historical_input']['head'] == scenario.delivery.base.sha
    assert observed['cross_architecture_compared'] is False
