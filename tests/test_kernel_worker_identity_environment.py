"""Frozen identities reach native credential preparation; no model or sandbox runs."""
from dataclasses import replace
from pathlib import Path
import os
import subprocess
import sys

import pytest

from agent_run.contracts.identity import validate_identity_snapshot
from agent_run.core.operation_runner import OperationRunner
from agent_run.errors import GitHubIdentityError
from agent_run.model import Commit, Delivery, DeliveryKind, RunSpec
from agent_run.runtime.codex import _CodexCapture
from support.core_operations import native_agent_fixture
from support.offline import fixture_inputs


def identity_preparation(tmp_path, worker, runner_identity):
    inputs = replace(fixture_inputs(),
        worker_identity=validate_identity_snapshot(worker),
        runner_identity=validate_identity_snapshot(runner_identity))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (tmp_path / "state").mkdir()
    kernel, ports = native_agent_fixture(tmp_path / "state" / "kernel.sqlite3", RunSpec(
        "native-pat", "example/repository", 1, "parent", "graph", (
            Delivery("ticket:2", DeliveryKind.TICKET, 2, "original", Commit("base", "tree"),
                     "refs/heads/ticket", "refs/heads/run", checkout=str(checkout)),),
        repository_root=str(checkout), inputs=inputs))
    execution = kernel.claim_executor("native-pat-owner")
    runner = OperationRunner(kernel, execution, lambda _: ports)
    request = None
    for _ in range(6):
        request = runner.run_once().preparation
        if request is not None:
            break
    assert request is not None
    binding = kernel.operation_binding(execution)
    return kernel, runner, request, binding


@pytest.mark.parametrize(("worker_reference", "runner_reference"), [
    ("GH_TOKEN", "RUNNER_PAT"),
    ("WORKER_PAT", "GH_TOKEN"),
    ("GITHUB_TOKEN", "RUNNER_PAT"),
])
def test_selected_worker_pat_survives_frozen_reference_masking(
    tmp_path: Path, worker_reference: str, runner_reference: str,
) -> None:
    kernel, runner, request, binding = identity_preparation(tmp_path,
        {"source": "pat", "pat": {"env": worker_reference}},
        {"source": "pat", "pat": {"env": runner_reference}})
    assert set(binding.hidden_environment) == {worker_reference, runner_reference}
    gh = tmp_path / "gh"
    log = tmp_path / "auth-reads"
    gh.write_text(f"#!{sys.executable}\n" + f'''import os, sys
assert sys.argv[1:] == ['api', '--hostname', 'github.com', 'user', '--include', '--silent']
assert os.environ.get('GH_TOKEN') == 'synthetic-worker-only'
if 'GH_CONFIG_DIR' in os.environ:
    assert {runner_reference!r} == 'GH_TOKEN' or {runner_reference!r} not in os.environ
    assert {worker_reference!r} == 'GH_TOKEN' or {worker_reference!r} not in os.environ
    assert 'GITHUB_TOKEN' not in os.environ
with open({str(log)!r}, 'a') as stream:
    stream.write('read\\n')
print('HTTP/2 200 OK\\r\\n\\r\\n')
''', encoding="utf-8")
    gh.chmod(0o700)
    environment = {"PATH": str(tmp_path) + os.pathsep + os.defpath, "HOME": str(tmp_path),
                   runner_reference: "synthetic-runner-only", worker_reference: "synthetic-worker-only"}
    original = dict(environment)
    capture = _CodexCapture(kernel, request, executable="never-executed", environment=environment,
        credential_file=None, identity=binding.inputs.worker_identity,
        hidden_paths=binding.hidden_paths, hidden_environment=binding.hidden_environment)
    with capture:
        consumer = subprocess.run([str(gh), "api", "--hostname", "github.com", "user", "--include", "--silent"],
            env=capture.environment, capture_output=True, timeout=5, check=False)
        assert consumer.returncode == 0  # No secret-bearing stderr in assertion output.
        assert log.read_text() == "read\nread\n"
        assert list(Path(capture.environment["GH_CONFIG_DIR"]).iterdir()) == []
        assert environment == original
        assert not capture.inherit_host_auth
        assert runner.state().active == () and runner.state().preparing == (request.reservation_id,)
        assert kernel.status(request.subject)["budgets"]["requirements"]["developments_used"] == 0
    assert capture.cleanup_complete
    assert not Path(capture.environment["GH_CONFIG_DIR"]).exists()
    kernel.preparation_failed(request, "identity_test_complete", {"resources_closed": True})
    assert kernel.worker_settled(request) and runner.state().preparing == ()


def test_missing_selected_pat_cannot_use_the_runner_token(tmp_path: Path) -> None:
    kernel, runner, request, binding = identity_preparation(tmp_path,
        {"source": "pat", "pat": {"env": "MISSING_WORKER_PAT"}},
        {"source": "pat", "pat": {"env": "GH_TOKEN"}})
    capture = _CodexCapture(kernel, request, executable="never-executed",
        environment={"PATH": os.defpath, "GH_TOKEN": "synthetic-runner-only"},
        credential_file=None, identity=binding.inputs.worker_identity,
        hidden_paths=binding.hidden_paths, hidden_environment=binding.hidden_environment)
    with pytest.raises(GitHubIdentityError):
        with capture:
            raise AssertionError("Missing selected PAT must not reach a Worker")
    assert capture.cleanup_complete and capture.arguments == []
    assert runner.state().active == () and runner.state().preparing == (request.reservation_id,)
    assert kernel.status(request.subject)["budgets"]["requirements"]["developments_used"] == 0
    kernel.preparation_failed(request, "identity_test_complete", {"resources_closed": True})
    assert kernel.worker_settled(request)


@pytest.mark.parametrize("credential_file", [False, True], ids=["host", "owned-config"])
def test_non_pat_delivery_never_restores_a_hidden_runner_token(tmp_path: Path, credential_file: bool) -> None:
    kernel, runner, request, binding = identity_preparation(tmp_path,
        {"source": "host"}, {"source": "pat", "pat": {"env": "GH_TOKEN"}})
    gh = tmp_path / "gh"
    gh.write_text(f"#!{sys.executable}\nprint('HTTP/2 200 OK\\r\\n\\r\\n')\n", encoding="utf-8")
    gh.chmod(0o700)
    credential = None
    if credential_file:
        credential = tmp_path / "synthetic-hosts.yml"
        credential.write_text("github.com:\n  oauth_token: synthetic-selected-config\n", encoding="utf-8")
    capture = _CodexCapture(kernel, request, executable="never-executed",
        environment={"PATH": str(tmp_path) + os.pathsep + os.defpath, "HOME": str(tmp_path),
                     "GH_TOKEN": "synthetic-runner-only"}, credential_file=credential,
        identity=binding.inputs.worker_identity, hidden_paths=binding.hidden_paths,
        hidden_environment=binding.hidden_environment)
    with capture:
        assert "GH_TOKEN" not in capture.environment
        assert capture.inherit_host_auth is not credential_file
        if credential_file:
            assert (Path(capture.environment["GH_CONFIG_DIR"]) / "hosts.yml").read_bytes() == credential.read_bytes()
    assert capture.cleanup_complete
    kernel.preparation_failed(request, "identity_test_complete", {"resources_closed": True})
    assert kernel.worker_settled(request) and runner.state().preparing == ()
