"""Remote effects through real bare Git and an isolated stateful gh executable."""
from dataclasses import replace
import json
import os
import shutil
from pathlib import Path
import sys

import pytest

from agent_run_kernel.effects import LocalGit
from agent_run_kernel.model import Effect, Execution, KernelError
from agent_run_kernel.remote_effects import RemoteEffects


def _fixture(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = LocalGit(repo)
    git.run("init", "--initial-branch=main")
    git.run("config", "--local", "user.name", "Remote fixture author")
    git.run("config", "--local", "user.email", "remote-fixture@example.invalid")
    git.run("config", "gc.auto", "0")
    (repo / "base").write_text("original\n")
    git.run("add", ".")
    git.run("commit", "-m", "base")
    base = git.commit("HEAD")
    remote = tmp_path / "origin.git"
    git.run("init", "--bare", str(remote))
    git.run("remote", "add", "origin", str(remote))
    git.run("push", "origin", "refs/heads/main", base.sha + ":refs/heads/delivery")
    git.run("switch", "-c", "delivery")
    (repo / "change").write_text("actual publication\n")
    git.run("add", ".")
    git.run("commit", "-m", "publication")
    published = git.commit("HEAD")
    state = tmp_path / "github.json"
    state.write_text(json.dumps({"prs": [], "comments": [], "events": [], "issue_state": "open"}))
    calls = tmp_path / "calls.jsonl"
    binary = tmp_path / "bin"
    binary.mkdir()
    fake = binary / "gh"
    fake.write_text("#!" + sys.executable + "\n" + FAKE_GH)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("REMOTE_TEST_STATE", str(state))
    monkeypatch.setenv("REMOTE_TEST_CALLS", str(calls))
    monkeypatch.setenv("REMOTE_TEST_BARE", str(remote))
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    port = RemoteEffects(repo, "example/project", runner_identity={"source": "host"})
    effect = Effect("push-1", "ticket:2", "push", "example/project", "refs/heads/delivery", "refs/heads/main",
        base.sha, published.sha, published, base, None, "publisher", {}, Execution("run-1", "worker", 1))
    return port, git, effect, state, calls


def test_publish_uses_exact_remote_lease_and_does_not_repeat_a_lost_response(tmp_path, monkeypatch):
    port, git, effect, _state, _calls = _fixture(tmp_path, monkeypatch)
    real_git = shutil.which("git")
    pushes = tmp_path / "pushes.jsonl"
    wrapper = tmp_path / "bin" / "git"
    wrapper.write_text("#!" + sys.executable + "\n" +
        "import json,subprocess,sys\n" +
        "result=subprocess.run([" + repr(real_git) + ",*sys.argv[1:]])\n" +
        "if sys.argv[1]=='push':\n" +
        " with open(" + repr(str(pushes)) + ", 'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n" +
        " raise SystemExit(1 if result.returncode==0 else result.returncode)\n" +
        "raise SystemExit(result.returncode)\n")
    wrapper.chmod(0o755)
    authorized = []
    result = port.execute(effect, lambda: authorized.append(effect.effect_id))
    assert result.disposition == "target" and result.commit == effect.candidate
    assert authorized == ["push-1"] and result.raw["write_returncode"] == 1
    assert len(pushes.read_text().splitlines()) == 1
    assert git.run("ls-remote", "--refs", "origin", effect.source_ref).stdout.split()[0] == effect.after_sha
    again = port.execute(effect, lambda: pytest.fail("target readback must not authorize another write"))
    assert again == port.reconcile(effect) and again.disposition == "target"


def test_publish_never_adopts_an_external_remote_head_or_bypasses_current_guard(tmp_path, monkeypatch):
    port, git, effect, _state, _calls = _fixture(tmp_path, monkeypatch)
    git.run("push", "origin", effect.before_sha + ":" + effect.source_ref)
    def stopped():
        raise KernelError("executor_fenced")
    with pytest.raises(KernelError, match="executor_fenced"):
        port.execute(effect, stopped)
    assert port.reconcile(effect).disposition == "prior"
    foreign = git.make_commit(effect.base.tree, (effect.before_sha,), "foreign remote actor")
    git.run("push", "origin", foreign.sha + ":" + effect.source_ref)
    assert port.execute(effect, lambda: pytest.fail("third value must not be overwritten")).disposition == "third"
    assert git.run("ls-remote", "--refs", "origin", effect.source_ref).stdout.split()[0] == foreign.sha




def _change(state, **values):
    document = json.loads(state.read_text())
    document.update(values)
    state.write_text(json.dumps(document))


def _pr_effect(effect):
    return replace(effect, effect_id="pr-1", operation="pr_write", before_sha=effect.after_sha,
                   payload={"title": "Original title", "body": "Original narrative\r\n中文", "prior": None})


def _publish_pr(port, effect):
    assert port.execute(effect, lambda: None).disposition == "target"
    effect = _pr_effect(effect)
    result = port.execute(effect, lambda: None)
    assert result.disposition == "target" and result.pr.number == 17
    return effect, result.pr


def test_pr_ack_loss_and_narrative_update_reconcile_exact_original_identity(tmp_path, monkeypatch):
    port, _git, push, state, calls = _fixture(tmp_path, monkeypatch)
    assert port.execute(push, lambda: None).disposition == "target"
    pr = _pr_effect(push)
    _change(state, lose_operation="pr")
    result = port.execute(pr, lambda: None)
    assert result.disposition == "target" and result.pr.title == "Original title"
    assert result.pr.body == "Original narrative\r\n中文" and result.pr.head_repository == "example/project"
    assert result.raw["write_returncode"] == 1
    assert port.execute(pr, lambda: pytest.fail("must only read original PR")).disposition == "target"
    update = replace(pr, effect_id="edit-1", pr_number=17,
        payload={"title": "Updated title", "body": "Updated body", "prior": {"title": result.pr.title, "body": result.pr.body}})
    assert port.execute(update, lambda: None).pr.body == "Updated body"
    actual = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum("POST" in c and c[1].endswith("/pulls") for c in actual) == 1
    assert sum("PATCH" in c for c in actual) == 1


def test_written_pr_target_settles_after_base_append_without_another_write(tmp_path, monkeypatch):
    port, git, push, state, calls = _fixture(tmp_path, monkeypatch)
    _change(state, lose_operation="pr")
    effect, original = _publish_pr(port, push)
    later = git.make_commit(push.base.tree, (push.base.sha,), "independent base append")
    git.run("push", "origin", later.sha + ":" + push.target_ref)
    document = json.loads(state.read_text())
    document["prs"][0]["base"]["sha"] = later.sha
    state.write_text(json.dumps(document))
    readback = port.execute(effect, lambda: pytest.fail("written original target needs no new write authority"))
    assert readback.disposition == "target" and readback.pr.number == original.number
    assert readback.pr.base.sha == later.sha and readback.pr.head == original.head
    update = replace(effect, effect_id="new-narrative", pr_number=original.number,
        payload={"title": "Next title", "body": "Next narrative", "prior": {"title": original.title, "body": original.body}})
    assert port.execute(update, lambda: pytest.fail("new write still needs original base")).disposition == "third"
    actual = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum("--method" in c for c in actual) == 1


@pytest.mark.parametrize("field", ["actor", "head_repository", "title", "closed"])
def test_pr_foreign_original_facts_cannot_be_overwritten_or_replaced(tmp_path, monkeypatch, field):
    port, _git, push, state, _calls = _fixture(tmp_path, monkeypatch)
    effect, _pr = _publish_pr(port, push)
    document = json.loads(state.read_text())
    pr = document["prs"][0]
    if field == "actor": pr["user"]["login"] = "external-author"
    elif field == "head_repository": pr["head"]["repo"]["full_name"] = "external/fork"
    elif field == "title": pr["title"] = "Unrelated external narrative"
    else: pr["state"] = "closed"
    state.write_text(json.dumps(document))
    assert port.execute(effect, lambda: pytest.fail("foreign PR must not be edited")).disposition == "third"
    assert len(json.loads(state.read_text())["prs"]) == 1


@pytest.mark.parametrize("method", ["squash", "merge"])
def test_original_merge_ack_loss_settles_after_later_target_append(tmp_path, monkeypatch, method):
    port, git, push, state, calls = _fixture(tmp_path, monkeypatch)
    _effect, pr = _publish_pr(port, push)
    parents = [push.base.sha] + ([] if method == "squash" else [push.after_sha])
    merge = replace(push, effect_id="merge-1", operation="merge", pr_number=pr.number,
        payload={"method": method, "parents": parents, "commit_message": "Reviewed merge\nOriginal body"})
    _change(state, lose_operation="merge")
    result = port.execute(merge, lambda: None)
    assert result.disposition == "target" and result.raw["write_returncode"] == 1
    assert result.commit.tree == push.candidate.tree
    assert result.raw["parents"] == parents and result.pr.merged == result.commit
    document = json.loads(state.read_text())
    for field in ("merged_by", "user"):
        original = document["prs"][0][field]
        document["prs"][0][field] = None
        state.write_text(json.dumps(document))
        assert port.execute(merge, lambda: pytest.fail("unobservable actor cannot authorize a write")).disposition == "unknown"
        document["prs"][0][field] = original
    state.write_text(json.dumps(document))
    git.run("fetch", "origin", result.commit.sha)
    later = git.make_commit(result.commit.tree, (result.commit.sha,), "later independent append")
    git.run("push", "origin", later.sha + ":" + push.target_ref)
    document = json.loads(state.read_text())
    document["prs"][0]["base"]["sha"] = later.sha
    state.write_text(json.dumps(document))
    resumed = port.execute(merge, lambda: pytest.fail("unknown merge must not merge twice"))
    assert resumed.disposition == "target" and resumed.commit == result.commit
    assert resumed.raw["target_head"] == later.sha and resumed.raw["live_pr_base"] == later.sha
    assert resumed.pr.base.sha == parents[0] == push.base.sha
    actual = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum(c[1].endswith("/merge") and "PUT" in c for c in actual) == 1


def test_remote_cas_still_preserves_third_head_appearing_at_dispatch(tmp_path, monkeypatch):
    port, git, effect, _state, _calls = _fixture(tmp_path, monkeypatch)
    foreign = git.make_commit(effect.base.tree, (effect.before_sha,), "concurrent remote writer")
    def race():
        git.run("push", "origin", foreign.sha + ":" + effect.source_ref)
    result = port.execute(effect, race)
    assert result.disposition == "third" and result.raw["write_returncode"] != 0
    assert result.raw["remote_head"] == foreign.sha


def test_close_uses_original_comment_watermark_and_rejects_reopened_or_foreign_issue(tmp_path, monkeypatch):
    port, _git, effect, state, calls = _fixture(tmp_path, monkeypatch)
    comment = replace(effect, effect_id="comment-1", operation="completion_comment", after_sha=None, pr_number=17,
        payload={"issue": 2, "merge_sha": effect.after_sha, "gate_mode": "reviewed", "kind": "ticket"})
    _change(state, lose_operation="comment")
    result = port.execute(comment, lambda: None)
    assert result.disposition == "target" and result.raw["write_returncode"] == 1
    assert result.raw["close_intent"]["baseline_event_id"] == 0
    assert "PR #17; Run Branch commit" in result.raw["close_intent"]["comment_body"]
    assert "not yet entered the default branch" in result.raw["close_intent"]["comment_body"]
    close = replace(comment, effect_id="close-1", operation="close",
        payload={"issue": 2, "merge_sha": effect.after_sha, "kind": "ticket", "close_intent": result.raw["close_intent"]})
    _change(state, lose_operation="close")
    result = port.execute(close, lambda: None)
    assert result.disposition == "target" and result.raw["event"]["id"] == 1
    assert port.execute(close, lambda: pytest.fail("observed original close is read-only")).disposition == "target"
    document = json.loads(state.read_text())
    document["events"][0]["actor"] = None
    state.write_text(json.dumps(document))
    assert port.execute(close, lambda: pytest.fail("unknown transition actor cannot write")).disposition == "unknown"
    document["events"][0]["actor"] = {"login": "publisher"}
    document["comments"][0]["user"] = None
    state.write_text(json.dumps(document))
    assert port.execute(close, lambda: pytest.fail("unknown comment author cannot write")).disposition == "unknown"
    assert port.execute(comment, lambda: pytest.fail("unknown comment author cannot be replaced")).disposition == "unknown"
    document["comments"][0]["user"] = {"login": "publisher"}
    document["events"].append({"id": 2, "event": "reopened", "actor": {"login": "external"}, "created_at": "2026-10-21T10:02:00Z"})
    document["issue_state"] = "open"
    state.write_text(json.dumps(document))
    assert port.execute(close, lambda: pytest.fail("do not reclose external reopen")).disposition == "third"
    actual = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum("POST" in c for c in actual) == 1 and sum("PATCH" in c for c in actual) == 1


def test_unknown_reads_and_unbound_close_never_authorize_a_write(tmp_path, monkeypatch):
    port, _git, effect, state, calls = _fixture(tmp_path, monkeypatch)
    close = replace(effect, operation="close", payload={"issue": 2, "merge_sha": effect.after_sha})
    assert port.execute(close, lambda: pytest.fail("no persisted close intent")).disposition == "unknown"
    _change(state, read_error=True)
    assert port.execute(_pr_effect(effect), lambda: pytest.fail("unknown cannot write")).disposition == "unknown"
    actual = [json.loads(line) for line in calls.read_text().splitlines()]
    assert not any("--method" in c for c in actual)


def test_selected_runner_actor_change_cannot_dispatch_original_effect(tmp_path, monkeypatch):
    port, _git, effect, state, _calls = _fixture(tmp_path, monkeypatch)
    _change(state, actor="different-login")
    with pytest.raises(KernelError, match="remote_actor_changed"):
        port.execute(effect, lambda: pytest.fail("actor mismatch must precede authorization"))


def test_optional_issue_link_ack_loss_is_not_readback_success_or_an_automatic_retry(tmp_path, monkeypatch):
    port, _git, push, state, calls = _fixture(tmp_path, monkeypatch)
    assert port.execute(push, lambda: None).disposition == "target"
    link = replace(push, effect_id="link-1", operation="issue_link", before_sha=push.after_sha,
        payload={"issue": 2, "ref": push.source_ref, "source_head": push.after_sha})
    _change(state, lose_operation="issue_link")
    authorized = []
    result = port.execute(link, lambda: authorized.append("link"))
    assert result.disposition == "unknown" and result.raw["display"] == "indeterminate"
    assert json.loads(state.read_text())["links"] == [{"ref": "delivery", "sha": push.after_sha}]
    # Actual linked branch exists, but it cannot prove a lost acknowledgement.
    reopened = RemoteEffects(port.root, port.repository, runner_identity={"source": "host"})
    assert reopened.reconcile(link).disposition == "unknown" and authorized == ["link"]
    actual = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum(c[1] == "graphql" and "POST" in c for c in actual) == 1


def test_optional_issue_link_keeps_exact_successful_graphql_acknowledgement(tmp_path, monkeypatch):
    port, _git, push, state, _calls = _fixture(tmp_path, monkeypatch)
    assert port.execute(push, lambda: None).disposition == "target"
    link = replace(push, effect_id="link-1", operation="issue_link", before_sha=push.after_sha,
        payload={"issue": 2, "ref": push.source_ref, "source_head": push.after_sha})
    result = port.execute(link, lambda: None)
    assert result.disposition == "target" and result.raw["display"] == "linked"
    assert result.raw["acknowledgement"]["data"]["createLinkedBranch"]["linkedBranch"]["ref"] == {
        "name": "delivery", "target": {"oid": push.after_sha}}
    assert json.loads(state.read_text())["links"] == [{"ref": "delivery", "sha": push.after_sha}]


@pytest.mark.parametrize("mode", ["acknowledged", "ack-lost", "unavailable"])
def test_real_issue_ack_stays_optional_and_once_across_kernel_reopen(tmp_path, monkeypatch, mode):
    from agent_run_kernel import DeliveryKind, Kernel
    from agent_run_kernel.offline import Scenario
    port, _git, _push, state, calls = _fixture(tmp_path, monkeypatch)
    # A separate synthetic authenticated principal, explicitly observed by gh;
    # no PR author or personal default is used as credential evidence.
    _change(state, actor="offline-publisher", lose_operation="issue_link" if mode == "ack-lost" else None,
            issue_read_error=mode == "unavailable")
    fake = tmp_path / "bin" / "gh"
    fake.write_text(fake.read_text().replace("example/project", "example/repository"))
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    original = scenario.port.execute
    selected = RemoteEffects(scenario.port.git.root, "example/repository", runner_identity={"source": "host"})
    def execute(effect, authorize=None):
        if effect.operation != "issue_link":
            return original(effect, authorize)
        scenario.port.git.run("push", str(tmp_path / "origin.git"), effect.before_sha + ":" + effect.source_ref)
        assert authorize is not None
        return selected.execute(effect, authorize)
    monkeypatch.setattr(scenario.port, "execute", execute)
    assert scenario.run().kind == "complete"
    durable = Kernel(scenario.kernel.storage.path)
    assert durable.status(scenario.delivery.key)["phase"] == "complete"
    assert durable.status(scenario.delivery.key)["issue_link"]["state"] == {"ack-lost": "indeterminate", "acknowledged": "linked", "unavailable": "unavailable"}[mode]
    before_roles = list(scenario.role_calls)
    before_usage = durable.status(scenario.delivery.key)["budgets"]
    scenario.kernel = durable
    assert scenario.run().kind == "complete"
    assert scenario.role_calls == before_roles and durable.status(scenario.delivery.key)["budgets"] == before_usage
    actual = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum(c[1] == "graphql" and "POST" in c for c in actual) == (0 if mode == "unavailable" else 1)
    assert len(json.loads(state.read_text()).get("links", [])) == (0 if mode == "unavailable" else 1)


def test_remote_cleanup_uses_selected_runner_transport_and_original_head(tmp_path, monkeypatch):
    port, git, push, _state, _calls = _fixture(tmp_path, monkeypatch)
    assert port.execute(push, lambda: None).disposition == "target"
    real_git = shutil.which("git")
    log = tmp_path / "git-environment.jsonl"
    transport = tmp_path / "transport-bin"
    transport.mkdir()
    wrapper = transport / "git"
    wrapper.write_text("#!" + sys.executable + "\n" +
        "import json,os,subprocess,sys\n" +
        "with open(" + repr(str(log)) + ", 'a') as f: f.write(json.dumps({'args':sys.argv[1:],'token':os.environ.get('GH_TOKEN'),'config':os.environ.get('GIT_CONFIG_COUNT')})+'\\n')\n" +
        # Only the external test server permits file://; production transport
        # remains restricted by the selected Runner's normal credential leaf.
        "env=os.environ.copy();env['GIT_ALLOW_PROTOCOL']='file'\n" +
        "raise SystemExit(subprocess.run([" + repr(real_git) + ",*sys.argv[1:]],env=env).returncode)\n")
    wrapper.chmod(0o755)
    # Give the selected transport a new PATH after fixture Git was resolved.
    monkeypatch.setenv("PATH", str(transport) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("SYNTHETIC_RUNNER_PAT", "frozen-synthetic-test-token")
    monkeypatch.setenv("GH_TOKEN", "unselected-inherited-test-token")
    identity = {"source": "pat", "pat": {"env": "SYNTHETIC_RUNNER_PAT"}}
    selected = RemoteEffects(port.root, port.repository, runner_identity=identity)
    cleanup = replace(push, effect_id="cleanup-1", operation="cleanup_remote_ref", before_sha=push.after_sha, after_sha=None,
        payload={"remote": "origin", "branch_ref": push.source_ref, "expected_head": push.after_sha, "github_identity": identity})
    authorized = []
    result = selected.execute(cleanup, lambda: authorized.append("delete"))
    assert result.disposition == "target" and authorized == ["delete"]
    assert git.run("show-ref", "--verify", "--hash", push.source_ref).stdout.strip() == push.after_sha
    observed = [json.loads(line) for line in log.read_text().splitlines()]
    def verb(row):
        args = row["args"]
        return args[2] if args[0] == "-C" else args[0]
    remote = [row for row in observed if verb(row) in {"ls-remote", "push"}]
    assert remote and all(row["token"] == "frozen-synthetic-test-token" and int(row["config"]) > 0 for row in remote)
    assert sum(verb(row) == "push" for row in remote) == 1
    assert selected.execute(cleanup, lambda: pytest.fail("absence needs no delete authority")).disposition == "target"


def _source_kernel(tmp_path, port, push):
    from agent_run_kernel import Delivery, DeliveryKind, Kernel, RunSpec
    from agent_run.agent_profiles import resolve_profiles
    from agent_run.prompt_resources import resolve_resources
    from agent_run_kernel.model import CreationInputs, ParentIssue, Requirements
    source = Requirements(ParentIssue(1, "Parent", "Whole requirement", ()), ())
    job = Delivery("parent:1", DeliveryKind.PARENT_ONLY, 1, "not-authority", push.base,
                   push.source_ref, push.target_ref)
    inputs = CreationInputs(resolve_profiles(preset="economy"), resolve_resources("en"),
                            {"source": "host"}, {"source": "host"}, {"enabled": False}, {"source": "explicit-test"})
    spec = RunSpec("source-run", port.repository, 1, "not-parent", "not-graph", (job,),
                   language="en", repository_root=str(port.root.resolve()), inputs=inputs)
    return Kernel.create(tmp_path / "source.sqlite3", spec, requirements=source)


def test_observe_uses_real_requirements_remote_commits_and_required_ci_facts(tmp_path, monkeypatch):
    port, git, push, state, calls = _fixture(tmp_path, monkeypatch)
    _effect, published = _publish_pr(port, push)
    # A real remote-only append is absent from this managed clone's object DB.
    other = tmp_path / "external-clone"
    git.run("clone", "--branch", "delivery", str(tmp_path / "origin.git"), str(other))
    external = LocalGit(other)
    external.run("config", "--local", "user.name", "Independent fixture author")
    external.run("config", "--local", "user.email", "independent-fixture@example.invalid")
    (other / "external-change").write_text("actual remote-only bytes\n")
    external.run("add", ".")
    external.run("commit", "-m", "external source append")
    latest = external.commit("HEAD")
    external.run("push", "origin", "HEAD:refs/heads/delivery")
    assert git.run("cat-file", "-e", latest.sha + "^{commit}", check=False).returncode != 0
    assert not (port.root / ".git" / "FETCH_HEAD").exists()
    document = json.loads(state.read_text())
    document["prs"][0]["head"]["sha"] = latest.sha
    state.write_text(json.dumps(document))
    _change(state, checks=[{"name": "Required test", "bucket": "fail", "state": "FAILURE", "workflow": "Original workflow",
                          "link": "https://external-ci.example/job/1", "description": "Original required failure"}])
    kernel = _source_kernel(tmp_path, port, push)
    receipt = kernel.source_status()
    refs_before = git.run("for-each-ref", "--format=%(refname) %(objectname)").stdout
    previous_calls = len(calls.read_text().splitlines())
    monkeypatch.setattr(kernel, "source_status", lambda: pytest.fail("must use this refresh receipt, never latest global status"))
    world = port.observe(kernel, "parent:1")
    assert world.parent_revision == receipt["observed_parent_revision"] and world.graph_revision == receipt["observed_graph_revision"]
    assert world.revision == world.parent_revision and world.completion_revision == "no-tickets"
    assert world.source == latest and world.target == published.base
    assert world.pr.number == 17 and world.pr.head_repository == "example/project"
    assert world.pr.body == "Original narrative\r\n中文"
    assert world.checks.result == "fail" and world.checks.head_sha == latest.sha
    assert "Original required failure" in world.checks.logs and "not_collected" in world.checks.logs
    assert world.publisher_actor == "publisher" and world.publisher_identity == {"source": "host"}
    assert (push.base.sha, latest.sha) in world.ancestry
    assert git.commit(latest.sha) == latest
    assert not (port.root / ".git" / "FETCH_HEAD").exists()
    assert git.run("for-each-ref", "--format=%(refname) %(objectname)").stdout == refs_before
    observed = [json.loads(line) for line in calls.read_text().splitlines()][previous_calls:]
    assert not any("--method" in c for c in observed)
    assert any(c[:2] == ["api", "graphql"] for c in observed)


def test_observe_rejects_a_real_old_source_read_when_new_requirements_commit_first(tmp_path, monkeypatch):
    from threading import Event, Thread
    import agent_run_kernel.remote_effects as external
    from agent_run.github import GhGitHubReader
    from agent_run_kernel.source_reading import read_requirements
    port, _git, push, state, _calls = _fixture(tmp_path, monkeypatch)
    kernel = _source_kernel(tmp_path, port, push)
    before = kernel.source_status()
    reached, release = Event(), Event()
    failures = []
    def delayed(reader, parent):
        result = read_requirements(reader, parent)  # real fake-gh subprocess reads before the barrier
        reached.set()
        assert release.wait(10), "bounded stale reader release"
        return result
    monkeypatch.setattr(external, "read_requirements", delayed)
    def old_observer():
        try:
            port.observe(kernel, "parent:1")
        except Exception as error:
            failures.append(error)
    thread = Thread(target=old_observer)
    thread.start()
    try:
        assert reached.wait(10), "old external source reader reached"
        _change(state, source_body="New actual requirement")
        current = GhGitHubReader(port.repository, working_directory=port.root, runner_identity={"source": "host"})
        newer = kernel.refresh(lambda: read_requirements(current, 1))
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and len(failures) == 1
    assert isinstance(failures[0], KernelError) and failures[0].code == "source_observation_changed"
    assert newer["observed_parent_revision"] != before["observed_parent_revision"]
    assert kernel.source_status() == newer


def test_final_observation_proves_real_current_completions_into_remote_run_before_activation(tmp_path, monkeypatch):
    from agent_run_kernel.model import Issue, ParentIssue, Requirements
    from agent_run_kernel.offline import GraphScenario
    _port, _git, _push, state, calls = _fixture(tmp_path, monkeypatch)
    source = Requirements(ParentIssue(1, "Parent", "Whole requirement", (2,)),
                          (Issue(2, "Ticket", "Whole ticket", "OPEN", frozenset({"ready-for-agent"})),))
    scenario = GraphScenario(tmp_path / "graph", source)
    assert scenario.run().kind == "complete"
    completed = scenario.kernel.source_status()["completions"]
    assert len(completed) == 1 and completed[0]["subject"] == "ticket:2"
    assert scenario.kernel.source_status()["final_subject"] is None
    initial_run = scenario.port.git.commit("refs/heads/run")
    default = scenario.port.git.commit("refs/heads/main")
    remote = tmp_path / "final-origin.git"
    scenario.port.git.run("init", "--bare", str(remote))
    scenario.port.git.run("remote", "add", "actual", str(remote))
    scenario.port.git.run("push", "actual", "refs/heads/run", "refs/heads/main")
    other = tmp_path / "final-external"
    scenario.port.git.run("clone", "--branch", "run", str(remote), str(other))
    external = LocalGit(other)
    external.run("config", "--local", "user.name", "Independent fixture author")
    external.run("config", "--local", "user.email", "independent-fixture@example.invalid")
    (other / "external-run-change").write_text("independent completed-run append\n")
    external.run("add", ".")
    external.run("commit", "-m", "later run head")
    latest = external.commit("HEAD")
    external.run("push", "origin", "HEAD:refs/heads/run")
    assert latest.sha != initial_run.sha != default.sha
    assert scenario.port.git.run("cat-file", "-e", latest.sha + "^{commit}", check=False).returncode != 0
    monkeypatch.setenv("REMOTE_TEST_BARE", str(remote))
    _change(state, source_graph={"parent": {"number": 1, "title": "Parent", "body": "Whole requirement", "sub_issues": [2]},
        "issues": {"2": {"number": 2, "title": "Ticket", "body": "Whole ticket", "state": "CLOSED", "labels": ["ready-for-agent"]}}})
    selected = RemoteEffects(scenario.port.git.root, "example/project", runner_identity={"source": "host"}, remote="actual")
    role_calls = list(scenario.role_calls)
    before_calls = len(calls.read_text().splitlines()) if calls.exists() else 0
    run_head, default_head, proof = selected.final_observation(scenario.kernel)
    assert run_head == latest and default_head == default
    assert (completed[0]["merge_sha"], latest.sha) in proof
    final = scenario.kernel.activate_final(scenario.execution, run_head, default_head, ancestry=proof)
    assert scenario.kernel.delivery(final).base == latest
    assert scenario.kernel.delivery(final).source_ref == "refs/heads/run"
    assert scenario.kernel.status(final)["phase"] == "workspace"
    assert scenario.role_calls == role_calls  # Final activation does not create initial Development.
    assert scenario.port.git.commit("refs/heads/run") == initial_run
    actual = [json.loads(line) for line in calls.read_text().splitlines()][before_calls:]
    assert not any("--method" in c for c in actual)


FAKE_GH = r'''import json, os, subprocess, sys
from pathlib import Path
args=sys.argv[1:]
state=Path(os.environ['REMOTE_TEST_STATE'])
d=json.loads(state.read_text())
os.environ.update(GIT_AUTHOR_NAME='Publisher',GIT_AUTHOR_EMAIL='publisher@example.invalid',GIT_COMMITTER_NAME='Publisher',GIT_COMMITTER_EMAIL='publisher@example.invalid',GIT_AUTHOR_DATE='2000-01-01T00:00:00Z',GIT_COMMITTER_DATE='2000-01-01T00:00:00Z')
with open(os.environ['REMOTE_TEST_CALLS'],'a') as f: f.write(json.dumps(args)+'\n')
def git(*args, input=None):
    result=subprocess.run(['git','--git-dir',os.environ['REMOTE_TEST_BARE'],*args],text=True,input=input,capture_output=True,check=True)
    return result.stdout.strip()
def head(ref):
    r=subprocess.run(['git','--git-dir',os.environ['REMOTE_TEST_BARE'],'rev-parse','--verify',ref],text=True,capture_output=True)
    return r.stdout.strip() if r.returncode==0 else None
if args[:2]==['repo','view']:
    print(json.dumps({'nameWithOwner':'example/project','defaultBranchRef':{'name':'main'}}));raise SystemExit(0)
if args[:2]==['pr','checks']:
    print(json.dumps(d.get('checks',[])));raise SystemExit(0)
if args[0]!='api': raise SystemExit('unexpected '+repr(args))
endpoint=args[1].split('?',1)[0]
method=args[args.index('--method')+1] if '--method' in args else 'GET'
body=json.loads(Path(args[args.index('--input')+1]).read_text()) if '--input' in args else {}
if d.get('read_error') and method=='GET':
    print('HTTP 403 forbidden',file=sys.stderr);raise SystemExit(1)
if endpoint=='user': result={'login':d.get('actor','publisher')}
elif endpoint=='graphql' and method=='GET':
    fields=dict(value.split('=',1) for index,value in enumerate(args) if index and args[index-1] in ('-F','-f'))
    graph=d.get('source_graph',{'parent':{'number':1,'title':'Parent','body':d.get('source_body','Whole requirement'),'sub_issues':[]},'issues':{}})
    if fields['number']=='1':
        p=graph['parent'];issue={'number':1,'title':p['title'],'body':p['body'],'subIssues':{'nodes':[{'number':n} for n in p['sub_issues']],'pageInfo':{'hasNextPage':False,'endCursor':None}}}
    else:
        p=graph['issues'][fields['number']];issue={**p,'labels':{'nodes':[{'name':name} for name in p['labels']]},'blockedBy':{'nodes':[],'pageInfo':{'hasNextPage':False,'endCursor':None}}}
    result={'data':{'repository':{'issue':issue}}}
elif endpoint=='graphql':
    v=body['variables']; assert v['issueId']=='issue-node-2' and head('refs/heads/'+v['name'])==v['oid']
    d.setdefault('links',[]).append({'ref':v['name'],'sha':v['oid']})
    result={'data':{'createLinkedBranch':{'linkedBranch':{'ref':{'name':v['name'],'target':{'oid':v['oid']}}}}}}
elif endpoint=='repos/example/project/commits/main': result={'sha':head('refs/heads/main')}
elif endpoint=='repos/example/project/rulesets': result=[[]]
elif '/protection/required_status_checks/contexts' in endpoint: result=[]
elif '/git/commits/' in endpoint:
    sha=endpoint.rsplit('/',1)[1]
    result={'sha':sha,'tree':{'sha':git('show','-s','--format=%T',sha)},'parents':[{'sha':x} for x in git('show','-s','--format=%P',sha).split()]}
elif '/compare/' in endpoint:
    a,b=endpoint.rsplit('/',1)[1].split('...')
    r=subprocess.run(['git','--git-dir',os.environ['REMOTE_TEST_BARE'],'merge-base','--is-ancestor',a,b])
    result={'status':'identical' if a==b else 'ahead' if r.returncode==0 else 'diverged','base_commit':{'sha':a},'merge_base_commit':{'sha':git('merge-base',a,b)}}
elif endpoint.startswith('repos/example/project/pulls'):
    tail=endpoint.split('/pulls',1)[1]
    if not tail or tail.startswith('?'):
        if method=='POST':
            pr={'number':17,'state':'open','title':body['title'],'body':body['body'],'user':{'login':'publisher'},'merged':False,'merge_commit_sha':None,
                'head':{'sha':head('refs/heads/'+body['head']),'ref':body['head'],'repo':{'full_name':'example/project'}},
                'base':{'sha':head('refs/heads/'+body['base']),'ref':body['base'],'repo':{'full_name':'example/project'}}}
            d['prs'].append(pr)
            result=pr
        else: result=[d['prs']] if '--slurp' in args else d['prs']
    else:
        number=int(tail.split('/')[1]);pr=next((p for p in d['prs'] if p['number']==number),None)
        if pr is None: print('HTTP 404',file=sys.stderr);raise SystemExit(1)
        if tail.endswith('/merge'):
            assert body['sha']==pr['head']['sha']
            parents=[pr['base']['sha']]+([] if body['merge_method']=='squash' else [pr['head']['sha']])
            command=['commit-tree',git('show','-s','--format=%T',pr['head']['sha'])]
            for parent in parents:command+=['-p',parent]
            message=body['commit_title']+'\n'+body.get('commit_message','')
            sha=git(*command,input=message+'\n')
            git('update-ref','refs/heads/'+pr['base']['ref'],sha,pr['base']['sha'])
            pr.update(state='closed',merged=True,merge_commit_sha=sha,merged_by={'login':'publisher'})
            result={'merged':True,'sha':sha}
        elif method=='PATCH':pr.update(body);result=pr
        else:result=pr
elif endpoint.startswith('repos/example/project/issues/'):
    if d.get('issue_read_error') and method=='GET':
        print('HTTP 403 Issue unavailable',file=sys.stderr);raise SystemExit(1)
    if endpoint.endswith('/comments'):
        if method=='POST':
            c={'id':len(d['comments'])+1,'body':body['body'],'user':{'login':'publisher'},'created_at':'2026-10-21T10:00:00Z'}
            d['comments'].append(c);result=c
        else:result=[d['comments']] if '--slurp' in args else d['comments']
    elif endpoint.endswith('/events'):result=[d['events']] if '--slurp' in args else d['events']
    elif method=='PATCH':
        d['issue_state']=body['state'];d['events'].append({'id':len(d['events'])+1,'event':'closed','actor':{'login':'publisher'},'created_at':'2026-10-21T10:01:00Z'});result={'state':d['issue_state']}
    else:result={'number':2,'node_id':'issue-node-2','state':d['issue_state']}
else:raise SystemExit('unexpected endpoint '+endpoint)
state.write_text(json.dumps(d))
print(json.dumps(result))
if method!='GET' and d.get('lose_operation')==('issue_link' if endpoint=='graphql' else 'merge' if endpoint.endswith('/merge') else 'comment' if endpoint.endswith('/comments') else 'close' if '/issues/' in endpoint else 'pr'):
    print('response lost after actual mutation',file=sys.stderr);raise SystemExit(1)
'''
