"""Activity time is diagnostic; all check/configuration proof stays binding."""
from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from agent_run.contracts.required_checks import same_required_checks
from agent_run.model import Checks


def _checks():
    pr = {'number': 17, 'updated_at': '2026-10-21T10:00:00Z', 'state': 'open',
          'head': {'sha': 'a' * 40, 'ref': 'candidate', 'repo': {'full_name': 'example/project'}},
          'base': {'sha': 'b' * 40, 'ref': 'main', 'repo': {'full_name': 'example/project'}}}
    evidence = {'source': 'original GitHub check/configuration observation', 'job_logs': 'not_collected',
        'observation': {'pr_before': pr, 'pr_after': deepcopy(pr), 'head_sha': 'a' * 40,
            'repository': 'example/project', 'repository_default_branch': 'main',
            'required_checks': [{'name': 'quality', 'bucket': 'pass', 'app_id': 1, 'id': 7}],
            'classic_checks': [{'context': 'quality', 'app_id': 1}], 'classic_contexts': ['quality'],
            'classic_protection_absent': False, 'rulesets': []}}
    return Checks(17, 'a' * 40, 'pass', (('quality', 'pass'),), json.dumps(evidence))


def test_activity_comparison_preserves_inputs_and_opaque_evidence():
    original = _checks()
    recorded = asdict(original)
    untouched = deepcopy(recorded)
    evidence = json.loads(original.logs)
    evidence['observation']['pr_before']['updated_at'] = '2026-10-21T10:00:01Z'
    del evidence['observation']['pr_after']['updated_at']
    assert same_required_checks(recorded, replace(original, logs=json.dumps(evidence)))
    assert recorded == untouched and original.logs == untouched['logs']
    for value in ('opaque', '{malformed', json.dumps({'source': 'unknown', 'observation': {}}),
                  json.dumps({'source': 'original GitHub check/configuration observation', 'observation': {}})):
        assert same_required_checks(asdict(replace(original, logs=value)), replace(original, logs=value))
        assert not same_required_checks(asdict(replace(original, logs=value)), original)


@pytest.mark.parametrize('path,value', [
    (('pr_before', 'number'), 18), (('pr_after', 'head', 'sha'), 'c' * 40),
    (('pr_after', 'head', 'repo', 'full_name'), 'foreign/project'),
    (('pr_after', 'base', 'sha'), 'c' * 40), (('pr_after', 'base', 'ref'), 'release'),
    (('pr_after', 'state'), 'closed'), (('repository',), 'foreign/project'),
    (('required_checks',), [{'name': 'quality', 'bucket': 'pass', 'app_id': 2, 'id': 8}]),
    (('required_checks',), [{'name': 'quality', 'bucket': 'pass', 'app_id': 1, 'id': 8}]),
    (('classic_checks',), [{'context': 'quality', 'app_id': 2}]),
    (('classic_protection_absent',), True), (('rulesets',), [{'id': 9}]),
])
def test_changed_check_authority_is_still_rejected(path, value):
    original = _checks()
    evidence = json.loads(original.logs)
    selected = evidence['observation']
    for key in path[:-1]:
        selected = selected[key]
    selected[path[-1]] = value
    assert not same_required_checks(asdict(original), replace(original, logs=json.dumps(evidence)))


@pytest.mark.parametrize('changes', [
    {'pr_number': 18}, {'head_sha': 'c' * 40}, {'result': 'unknown'},
    {'details': (('new-required-check', 'pass'),)},
])
def test_changed_normalized_check_binding_is_still_rejected(changes):
    original = _checks()
    assert not same_required_checks(asdict(original), replace(original, **changes))
