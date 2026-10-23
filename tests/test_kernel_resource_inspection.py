"""Original managed Git witnesses are inspected without changing their resources."""
from pathlib import Path

from agent_run.model import Effect, Execution, ResourceWitness
from agent_run.adapters.git_objects import LocalGit
from agent_run.adapters.local_git import LocalEffects
from agent_run.model import resource_wire


def test_resource_preflight_distinguishes_owned_dirty_absent_and_foreign_without_writes(
    git_repo: Path, tmp_path: Path,
) -> None:
    git = LocalGit(git_repo)
    head = git.commit("HEAD")
    owned = tmp_path / "work"
    owned.mkdir()
    resources: list[ResourceWitness] = []
    for name in ("clean", "dirty", "absent"):
        checkout = owned / name
        ref = "refs/heads/" + name
        git.run("update-ref", ref, head.sha)
        git.run("worktree", "add", "--detach", str(checkout), head.sha)
        resources.append(ResourceWitness("ticket:2", 1, "development", str(checkout), str(owned),
                                         str(git_repo / ".git"), ref, head.sha))
    (Path(resources[1].checkout) / "keep.txt").write_text("Unsaved work stays here.\n")
    git.run("worktree", "remove", resources[2].checkout)
    foreign = ResourceWitness("ticket:2", 1, "validation", resources[0].checkout, str(owned),
                              str(tmp_path / "foreign.git"), resources[0].source_ref, head.sha)
    original = tuple(resources) + (foreign,)
    before = {str(path.relative_to(git_repo)): path.read_bytes()
              for path in (git_repo / ".git").rglob("*") if path.is_file()}
    local = LocalEffects(git, owned_root=owned)
    observed = local.inspect_resources(original)
    assert tuple(item.resource for item in observed) == original
    assert tuple(item.disposition for item in observed) == ("clean", "dirty", "absent", "foreign")
    assert tuple(item.head_sha for item in observed[:3]) == (head.sha, head.sha, None)
    assert (Path(resources[1].checkout) / "keep.txt").read_text() == "Unsaved work stays here.\n"
    assert not Path(resources[2].checkout).exists()
    assert {str(path.relative_to(git_repo)): path.read_bytes()
            for path in (git_repo / ".git").rglob("*") if path.is_file()} == before


def test_resource_inspection_effect_authorizes_before_its_fresh_read(git_repo: Path, tmp_path: Path) -> None:
    git = LocalGit(git_repo)
    head = git.commit("HEAD")
    owned = tmp_path / "work"
    owned.mkdir()
    checkout = owned / "delivery"
    git.run("worktree", "add", "--detach", str(checkout), head.sha)
    resource = ResourceWitness("ticket:2", 1, "development", str(checkout), str(owned),
                               str(git_repo / ".git"), "refs/heads/task", head.sha)
    effect = Effect("inspect-original", resource.subject, "inspect_resources", "example/project",
                    resource.source_ref, "refs/heads/run", head.sha, None, None, head, None, None,
                    {"control_action": "abandon-original", "discard_worktree": False,
                     "resources": [resource_wire(resource)]}, Execution("run", "executor", 1))
    authorized: list[str] = []

    def authorize() -> None:
        authorized.append("original-action")
        (checkout / "arrived-before-inspection.txt").write_text("Preserve this fresh work.\n")

    local = LocalEffects(git, owned_root=owned)
    result = local.execute(effect, authorize)
    assert result.disposition == "target"
    assert authorized == ["original-action"]
    assert result.raw["resources"] == [{"resource": resource_wire(resource), "disposition": "dirty",
                                         "head_sha": head.sha, "reason": "worktree_dirty"}]
    assert local.reconcile(effect) == result
    assert authorized == ["original-action"]
    assert (checkout / "arrived-before-inspection.txt").read_text() == "Preserve this fresh work.\n"


def test_resource_inspection_uses_each_generations_root_and_checks_validation_dirt(
    git_repo: Path, tmp_path: Path,
) -> None:
    git = LocalGit(git_repo)
    head = git.commit("HEAD")
    resources: list[ResourceWitness] = []
    for generation, kind in ((1, "development"), (2, "validation")):
        owned = tmp_path / f"generation-{generation}"
        owned.mkdir()
        checkout = owned / "checkout"
        git.run("worktree", "add", "--detach", str(checkout), head.sha)
        resources.append(ResourceWitness("ticket:2", generation, kind, str(checkout), str(owned),
                                         str(git_repo / ".git"), "refs/heads/task", head.sha))
    (Path(resources[1].checkout) / "validation-output").write_text("Keep validation work.\n")
    local = LocalEffects(git)
    observed = local.inspect_resources(tuple(resources))
    assert tuple(item.resource for item in observed) == tuple(resources)
    assert tuple(item.disposition for item in observed) == ("clean", "dirty")
    assert tuple(item.head_sha for item in observed) == (head.sha, head.sha)
    assert (Path(resources[1].checkout) / "validation-output").read_text() == "Keep validation work.\n"
