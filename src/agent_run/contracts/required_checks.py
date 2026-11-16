"""Compare required-check authority without rewriting its original evidence."""
from dataclasses import asdict
import json
from typing import Any, Mapping

from agent_run.model import Checks, wire_json


def _binding(value: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(value)
    logs = bound.get('logs')
    if not isinstance(logs, str):
        return bound
    try:
        evidence = json.loads(logs)
    except ValueError:
        return bound
    if not isinstance(evidence, dict) or evidence.get('source') != 'original GitHub check/configuration observation':
        return bound
    observation = evidence.get('observation')
    if not isinstance(observation, dict) or any(not isinstance(observation.get(key), dict) for key in ('pr_before', 'pr_after')):
        return bound
    # Comment/Status activity updates this PR-record timestamp, independently
    # of its head, base, state and check/configuration facts. Keep every other
    # field binding and leave the complete original logs in durable material.
    for key in ('pr_before', 'pr_after'):
        observation[key].pop('updated_at', None)
    bound['logs'] = evidence
    return bound


def same_required_checks(recorded: Mapping[str, Any], current: Checks) -> bool:
    """Unknown/opaque evidence stays exact; only PR activity time is incidental."""
    return wire_json(_binding(recorded)) == wire_json(_binding(asdict(current)))
