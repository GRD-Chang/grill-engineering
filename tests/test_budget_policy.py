"""Allowance ownership and renewal arithmetic, without replaying delivery."""
import pytest

from agent_run.core.budget_policy import budget_limits, renewed_budget, requires_review
from agent_run.model import DeliveryKind, Policy, Purpose


@pytest.mark.parametrize('kind,requirements,integration,ci', [
    (DeliveryKind.TICKET, (3, 2), (11, 0), (13, None)),
    (DeliveryKind.PARENT_ONLY, (5, 5), (7, None), (17, None)),
    (DeliveryKind.FINAL_RUN, (5, 5), (7, 8), (17, None)),
])
def test_budget_owner_uses_its_frozen_policy(kind, requirements, integration, ci):
    policy = Policy(ticket_rounds=2, parent_rounds=5, run_repairs=7,
                    integration_developments=11, ticket_ci_developments=13, parent_ci_developments=17)
    assert budget_limits(policy, kind) == {
        Purpose.REQUIREMENTS: requirements, Purpose.INTEGRATION: integration, Purpose.CI: ci}


@pytest.mark.parametrize('kind,ticket_review,ci_review,integration_review', [
    (DeliveryKind.TICKET, False, False, False),
    (DeliveryKind.TICKET, True, True, False),
    (DeliveryKind.PARENT_ONLY, False, True, True),
    (DeliveryKind.PARENT_ONLY, True, True, True),
    (DeliveryKind.FINAL_RUN, False, True, True),
    (DeliveryKind.FINAL_RUN, True, True, True),
])
def test_review_requirement_is_independent_of_unlimited_ci_reviews(kind, ticket_review, ci_review, integration_review):
    policy = Policy(ticket_ci_review=ticket_review)
    assert budget_limits(policy, kind)[Purpose.CI][1] is None
    assert requires_review(policy, kind, Purpose.CI) is ci_review
    assert requires_review(policy, kind, Purpose.REQUIREMENTS) is True
    assert requires_review(policy, kind, Purpose.INTEGRATION) is integration_review


@pytest.mark.parametrize('kind,limit', [(DeliveryKind.TICKET, 7), (DeliveryKind.PARENT_ONLY, 11), (DeliveryKind.FINAL_RUN, 11)])
def test_ci_renewal_preserves_cumulative_reviews_and_renews_development(kind, limit):
    policy = Policy(ticket_ci_developments=7, parent_ci_developments=11, ticket_ci_review=True)
    old = {'development_limit': limit, 'review_limit': None, 'developments_used': limit, 'reviews_used': 8}
    assert renewed_budget(policy, kind, Purpose.CI, old) == (0, 8, limit, None, True)
    assert old['developments_used'] == limit and old['reviews_used'] == 8


@pytest.mark.parametrize('used,reviews,review_limit,new_cycle,expected', [
    (2, 3, 6, False, (2, 3)),
    (4, 3, 6, False, (0, 3)),
    (2, 6, 6, False, (2, 0)),
    (4, 6, 6, False, (0, 0)),
    (2, 3, 6, True, (0, 0)),
])
def test_final_resume_renews_only_exhausted_allowances(used, reviews, review_limit, new_cycle, expected):
    old = {'development_limit': 4, 'review_limit': review_limit, 'developments_used': used, 'reviews_used': reviews}
    assert renewed_budget(Policy(run_repairs=3), DeliveryKind.FINAL_RUN, Purpose.INTEGRATION,
                          old, new_cycle=new_cycle) == (*expected, 3, 4, True)


@pytest.mark.parametrize('kind,expected', [
    (DeliveryKind.TICKET, (2, 1, 9, 0, False)),
    (DeliveryKind.PARENT_ONLY, (2, 1, 7, None, True)),
])
def test_other_integration_renewal_adds_allowance_without_erasing_use(kind, expected):
    old = {'development_limit': 4, 'review_limit': None, 'developments_used': 2, 'reviews_used': 1}
    assert renewed_budget(Policy(run_repairs=3, integration_developments=5), kind, Purpose.INTEGRATION, old) == expected


def test_requirement_renewal_opens_fresh_budget():
    old = {'development_limit': 4, 'review_limit': 3, 'developments_used': 3, 'reviews_used': 3}
    assert renewed_budget(Policy(ticket_rounds=2), DeliveryKind.TICKET, Purpose.REQUIREMENTS, old) == (0, 0, 3, 2, True)
