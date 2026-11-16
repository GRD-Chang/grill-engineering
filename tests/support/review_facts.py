"""Explicit candidate and validation receipts for Core role and budget decisions."""
from contextlib import contextmanager

import pytest

from agent_run.core.operation_contract import DeliveryFacts
from agent_run.model import Commit, DeliveryKind, EffectResult, Policy
from support.core_operations import CoreOperations, ReadOnlyPorts
from support.run_scoped_kernel import domain_kernel, prepare_domain_history
from support.worker_facts import worker_fixture


class ReviewFacts(ReadOnlyPorts):
    """Finite external inputs; no SQL writes or invented budget/completion rows."""
    def __init__(self, original):
        super().__init__(original.requirements, original.source_ref, original.target_ref,
            DeliveryFacts(original.facts.target, original.facts.target))
        self.candidate, self.candidates = original.facts.target, 0

    def execute(self, effect, authorize):
        assert effect.operation in {'candidate', 'validation_checkout', 'validation_cleanup', 'integrate'}
        authorize.prepare()()
        if effect.operation == 'candidate':
            self.candidates += 1
            self.candidate = Commit(f'{self.candidates:040x}', f'{100 + self.candidates:040x}')
            return EffectResult('target', commit=self.candidate)
        if effect.operation == 'validation_cleanup':
            return EffectResult('target')
        if effect.operation == 'validation_checkout':
            assert effect.payload['reviewed_commit'] == self.candidate.sha
            return EffectResult('target', commit=self.candidate)
        assert effect.candidate == self.candidate
        return EffectResult('target', commit=self.candidate, expected_tree=self.candidate.tree)


@contextmanager
def requirement_review(root, *, kind=DeliveryKind.TICKET, policy=None):
    native, original = worker_fixture(root, kind=kind, policy=policy or Policy())
    execution = original.runner.execution
    ports = ReviewFacts(original.runner.ports)
    with domain_kernel(native) as kernel:
        yield kernel, CoreOperations(kernel, execution, lambda _: ports)


@pytest.fixture(scope="session")
def _ticket_ci_review_stage(tmp_path_factory):
    from support.offline_prefixes import _at_phase, _prepared_stage

    def prepare(root):
        return _at_phase(root, DeliveryKind.TICKET, "pr", policy=Policy(ticket_ci_review=True))

    with _prepared_stage(tmp_path_factory, "before-ci-review", prepare) as checkout:
        yield checkout


@pytest.fixture
def ticket_before_ci_review(_ticket_ci_review_stage, isolated_user_environment):
    with _ticket_ci_review_stage() as scenario, prepare_domain_history(scenario):
        yield scenario


@pytest.fixture
def domain_ticket_before_pr(ticket_before_pr):
    with prepare_domain_history(ticket_before_pr):
        yield ticket_before_pr


@pytest.fixture
def domain_parent_at_approval(parent_at_approval):
    with prepare_domain_history(parent_at_approval):
        yield parent_at_approval
