"""Independent copies of the history before Parent repair consumer boundaries.

Both stages use the original Core/Git producers. Future Agent/check scripts
remain case inputs; no Final checks or Agent calls precede the first stage,
and the second stops before creating the repair PR.
"""

import pytest

from agent_run.model import DeliveryKind
from support.offline import Scenario
from support.offline_prefixes import _prepared_stage
from support.run_scoped_kernel import prepare_domain_history


@pytest.fixture(scope="session")
def _final_review_stage(tmp_path_factory):
    def prepare(root):
        scenario = Scenario(root, DeliveryKind.FINAL_RUN)
        assert scenario.review_count == scenario.development_count == 0
        assert scenario.check_heads == {}
        return scenario

    with _prepared_stage(tmp_path_factory, "final-before-review", prepare) as checkout:
        yield checkout


@pytest.fixture
def final_before_review(_final_review_stage, isolated_user_environment):
    with _final_review_stage() as scenario, prepare_domain_history(scenario):
        yield scenario


@pytest.fixture(scope="session")
def _repair_pr_stage(tmp_path_factory):
    def prepare(root):
        scenario = Scenario(root, DeliveryKind.FINAL_RUN,
                            reviews=("fail", "pass"), checks=("pending",))
        with prepare_domain_history(scenario):
            for _ in range(100):
                if scenario._progress()["phase"] == "repair_pr":
                    # Scripts may change in the copy only while their result
                    # has not entered any checks observation or budget fact.
                    assert not any(scenario.check_heads.values())
                    assert scenario.review_count == 2 and scenario.development_count == 1
                    return scenario
                step = scenario.step()
                assert step.kind not in {"paused", "complete", "approval", "waiting"}, step
        raise AssertionError("The original Parent never reached repair PR publication")

    with _prepared_stage(tmp_path_factory, "repair-before-pr", prepare) as checkout:
        yield checkout


@pytest.fixture
def repair_before_pr(_repair_pr_stage, isolated_user_environment):
    with _repair_pr_stage() as scenario, prepare_domain_history(scenario):
        yield scenario
