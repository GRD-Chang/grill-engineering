"""Independent copies of recurring Git/SQLite preconditions.

The workspace sits at the same worker-private path for each copy: both the
worktree and the stored Run identify the original checkout by absolute path.
Neither the prepared directory nor its Python scenario is handed to a test.
"""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

import pytest

from agent_run.core.kernel import Kernel
from agent_run.model import DeliveryKind, Policy
from support.environment import _apply_test_environment
from support.offline import OfflinePort, Scenario
from support.run_scoped_kernel import prepare_domain_history


@contextmanager
def _prepared_stage(tmp_path_factory, name, prepare):
    with TemporaryDirectory(prefix=name + "-", dir=tmp_path_factory.getbasetemp()) as raw:
        root = Path(raw)
        live, prepared = root / "work", root / "prepared"
        with pytest.MonkeyPatch.context() as env:
            _apply_test_environment(root / "user", env)
            example = prepare(live)
        live.rename(prepared)

        @contextmanager
        def checkout():
            # Reserve our copy before entering cleanup; a second live consumer
            # cannot delete another case’s checkout if acquisition fails.
            live.mkdir()
            try:
                shutil.copytree(prepared, live, symlinks=True, dirs_exist_ok=True)
                port = OfflinePort(example.port.root)
                kernel = Kernel(example.kernel.storage.path, run_id=example.spec.run_id)
                yield deepcopy(example, {id(example.port): port, id(example.kernel): kernel})
            finally:
                shutil.rmtree(live)

        yield checkout


def _at_phase(root, kind, phase, *, policy=None):
    scenario = Scenario(root, kind, policy=policy)
    with prepare_domain_history(scenario):
        for _ in range(100):
            if scenario._progress()["phase"] == phase:
                return scenario
            scenario.step()
    raise AssertionError(f"Original {kind} never reached {phase}")


@pytest.fixture(scope="session")
def _ticket_pr_stage(tmp_path_factory):
    with _prepared_stage(tmp_path_factory, "pr", lambda root: _at_phase(root, DeliveryKind.TICKET, "pr")) as checkout:
        yield checkout


@pytest.fixture(scope="session")
def _parent_approval_stage(tmp_path_factory):
    with _prepared_stage(tmp_path_factory, "approval", lambda root: _at_phase(root, DeliveryKind.PARENT_ONLY, "approval")) as checkout:
        yield checkout


@pytest.fixture(scope="session")
def _parent_without_resync_stage(tmp_path_factory):
    def prepare(root):
        return _at_phase(root, DeliveryKind.PARENT_ONLY, "approval", policy=Policy(resync_limit=0))

    with _prepared_stage(tmp_path_factory, "approval-no-resync", prepare) as checkout:
        yield checkout


@pytest.fixture
def parent_approval_without_resync(_parent_without_resync_stage, isolated_user_environment):
    with _parent_without_resync_stage() as scenario:
        yield scenario


@pytest.fixture
def ticket_before_pr(_ticket_pr_stage, isolated_user_environment):
    with _ticket_pr_stage() as scenario:
        yield scenario


@pytest.fixture
def parent_at_approval(_parent_approval_stage, isolated_user_environment):
    with _parent_approval_stage() as scenario:
        yield scenario


@pytest.fixture(scope="session")
def _completed_managed_ticket(tmp_path_factory):
    from support.control_scenarios import managed_abandon_scenario

    def finish_ticket(root):
        scenario, _ = managed_abandon_scenario(root)
        assert scenario.run().kind == "complete"
        return scenario

    with _prepared_stage(tmp_path_factory, "completed-managed-ticket", finish_ticket) as checkout:
        yield checkout


@pytest.fixture
def managed_completed_ticket(_completed_managed_ticket, isolated_user_environment):
    with _completed_managed_ticket() as scenario:
        yield scenario, scenario.kernel.host_request("start")


@pytest.fixture(scope="session")
def _ticket_merge_stage(tmp_path_factory):
    from support.cleanup_scenarios import _native_scenario

    def before_merge(root):
        scenario = _native_scenario(root, DeliveryKind.TICKET)
        for _ in range(60):
            if scenario._progress()["phase"] == "merge":
                return scenario
            scenario.step()
        raise AssertionError("Ticket never reached merge")

    with _prepared_stage(tmp_path_factory, "before-merge", before_merge) as checkout:
        yield checkout


@pytest.fixture
def graph_ticket_before_merge(_ticket_merge_stage, isolated_user_environment):
    with _ticket_merge_stage() as scenario:
        yield scenario


@pytest.fixture(scope="session")
def _dependency_stage(tmp_path_factory):
    from support.core_operations import _prepare_dependent_ticket

    with _prepared_stage(tmp_path_factory, "dependency-complete", _prepare_dependent_ticket) as checkout:
        yield checkout


@pytest.fixture
def dependent_ticket(_dependency_stage, isolated_user_environment):
    with _dependency_stage() as scenario:
        yield scenario, scenario.port.remote_commit(scenario.delivery.target_ref)


@pytest.fixture(scope="session")
def _failed_final_review_stage(tmp_path_factory):
    from support.notification import _enabled_scenario, recorded_agents

    def prepare(root):
        with pytest.MonkeyPatch.context() as patch:
            scenario = _enabled_scenario(root, patch, DeliveryKind.FINAL_RUN, reviews=("fail", "pass"))
            with prepare_domain_history(scenario):
                recorded_agents(scenario, patch)
                for _ in range(100):
                    step = scenario.step()
                    if step.call is not None and step.call.role == "review":
                        assert scenario.step().kind == "progress"
                        return scenario
                    assert step.kind not in {"paused", "waiting", "approval"}, step
        raise AssertionError("The original Final review did not return its failure")

    with _prepared_stage(tmp_path_factory, "failed-final-review", prepare) as checkout:
        yield checkout


@pytest.fixture
def notification_failed_final_review(_failed_final_review_stage, isolated_user_environment):
    with _failed_final_review_stage() as scenario:
        native_storage = scenario.kernel.storage
        with prepare_domain_history(scenario):
            yield scenario, native_storage


@pytest.fixture(scope="session")
def _notification_pr_stage(tmp_path_factory):
    from support.notification import _enabled_scenario

    def prepare(root):
        with pytest.MonkeyPatch.context() as patch:
            scenario = _enabled_scenario(root, patch, DeliveryKind.TICKET, mode="detailed")
        with prepare_domain_history(scenario):
            for _ in range(100):
                if scenario._progress()["phase"] == "pr":
                    return scenario
                scenario.step()
        raise AssertionError("The original Ticket did not prepare its PR")

    with _prepared_stage(tmp_path_factory, "notification-pr", prepare) as checkout:
        yield checkout


@pytest.fixture
def notification_ticket_before_pr(_notification_pr_stage, isolated_user_environment):
    with _notification_pr_stage() as scenario:
        yield scenario
