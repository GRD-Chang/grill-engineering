"""Concrete operation ports: real Git and the isolated GitHub transport."""
from dataclasses import replace
import json

import pytest

from agent_run.errors import KernelError
from agent_run.contracts.write_authority import WriteAuthority
from agent_run.model import ResourceWitness
from agent_run.core.operation_contract import DeliveryRead, OperationPorts
from agent_run.adapters.remote import RemoteEffects
from support.remote_effects import publication
from support.remote_effects import _change, _fixture, _publish_pr


def test_source_and_delivery_facts_need_no_core_or_database(tmp_path, monkeypatch):
    port, git, push, state, calls = _fixture(tmp_path, monkeypatch)
    _effect, published = _publish_pr(port, push)
    _change(state, source_graph={"parent": {"number": 1, "title": "Parent", "body": "Whole requirement", "sub_issues": []},
                                 "issues": {}})
    ports: OperationPorts = port
    before = git.run("for-each-ref", "--format=%(refname) %(objectname)").stdout
    first = len(calls.read_text().splitlines())
    source = ports.source(1)
    assert source.parent.number == 1 and source.parent.body == "Whole requirement" and source.issues == ()
    observed = ports.delivery(DeliveryRead(push.source_ref, push.target_ref, published.number, (push.base.sha,)))
    assert observed.source == published.head and observed.target == published.base
    assert observed.pr == published and observed.publisher_actor == "publisher"
    assert observed.publisher_identity == {"source": "host"}
    assert (push.base.sha, published.head.sha) in observed.ancestry
    assert git.run("for-each-ref", "--format=%(refname) %(objectname)").stdout == before
    reads = [json.loads(line) for line in calls.read_text().splitlines()][first:]
    assert not any("--method" in command for command in reads)


def test_local_operation_port_preserves_witness_guard_and_original_readback(git_repo, tmp_path):
    git, _local, effect = publication(git_repo, tmp_path)
    ports: OperationPorts = RemoteEffects(git.root, effect.repository, runner_identity={"source": "host"})
    witness = ports.prepare_publication(effect)
    work = replace(effect, payload={**effect.payload, "publication_commit_intent": witness})
    assert ports.reconcile(work).disposition == "prior"
    def stopped():
        raise KernelError("executor_fenced")
    with pytest.raises(KernelError, match="executor_fenced"):
        ports.execute(work, WriteAuthority(lambda: stopped))
    assert ports.reconcile(work).disposition == "prior"
    authorized = []
    result = ports.execute(work, WriteAuthority(lambda: lambda: authorized.append(work.effect_id)))
    assert authorized and result.disposition == "target"
    assert result.commit.sha == witness["expected_sha"]
    assert git.run("cat-file", "commit", result.commit.sha).stdout == witness["raw_commit"]
    assert ports.reconcile(work).commit == result.commit
    resource = ResourceWitness(work.subject, 1, "development", work.payload["checkout"],
                               work.payload["owned_root"], work.payload["common_git_dir"], work.source_ref, result.commit.sha)
    inspected = ports.inspect((resource,))
    assert len(inspected) == 1 and inspected[0].resource == resource
    assert inspected[0].head_sha == result.commit.sha and inspected[0].disposition == "clean"
