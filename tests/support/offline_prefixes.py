"""Independent copies of two recurring Git/SQLite preconditions.

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
from agent_run.model import DeliveryKind
from support.environment import _apply_test_environment
from support.offline import OfflinePort, Scenario


@contextmanager
def _prepared_stage(tmp_path_factory, kind, phase):
    with TemporaryDirectory(prefix=phase + "-", dir=tmp_path_factory.getbasetemp()) as raw:
        root = Path(raw)
        live, prepared = root / "work", root / "prepared"
        with pytest.MonkeyPatch.context() as env:
            _apply_test_environment(root / "user", env)
            example = Scenario(live, kind)
            for _ in range(100):
                if example._progress()["phase"] == phase:
                    break
                example.step()
            else:
                raise AssertionError(f"Original {kind} never reached {phase}")
        live.rename(prepared)

        @contextmanager
        def checkout():
            # Reserve our copy before entering cleanup; a second live consumer
            # cannot delete another case’s checkout if acquisition fails.
            live.mkdir()
            try:
                shutil.copytree(prepared, live, symlinks=True, dirs_exist_ok=True)
                port = OfflinePort(live)
                kernel = Kernel(example.kernel.storage.path, run_id=example.spec.run_id)
                yield deepcopy(example, {id(example.port): port, id(example.kernel): kernel})
            finally:
                shutil.rmtree(live)

        yield checkout


@pytest.fixture(scope="session")
def _ticket_pr_stage(tmp_path_factory):
    with _prepared_stage(tmp_path_factory, DeliveryKind.TICKET, "pr") as checkout:
        yield checkout


@pytest.fixture(scope="session")
def _parent_approval_stage(tmp_path_factory):
    with _prepared_stage(tmp_path_factory, DeliveryKind.PARENT_ONLY, "approval") as checkout:
        yield checkout


@pytest.fixture
def ticket_before_pr(_ticket_pr_stage, isolated_user_environment):
    with _ticket_pr_stage() as scenario:
        yield scenario


@pytest.fixture
def parent_at_approval(_parent_approval_stage, isolated_user_environment):
    with _parent_approval_stage() as scenario:
        yield scenario
