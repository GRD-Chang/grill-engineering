"""Finite CLI query/configuration inputs; no commands execute at import."""
from __future__ import annotations

from dataclasses import replace
import json
import subprocess
from pathlib import Path

from support.cli_payloads import passing_acceptance, publication
from support.offline import fixture_inputs
from agent_run.adapters.workspace import ManagedWorkspace
from agent_run.config.creation import freeze_creation
from agent_run.config.defaults import UserDefaultsStore
from agent_run.contracts.agent_profiles import resolve_profiles
from agent_run.core.kernel import Kernel
from agent_run.model import Blocker, Commit, Delivery, DeliveryKind, Issue, ParentIssue, Policy, Profile, Requirements, RunSpec


def run_agents(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "ticket-developer-3",
                        "summary": "Delivered the Ticket.",
                        "write_files": {"feature.txt": "done\n"},
                    }
                ],
                "publications": [publication()],
                "reviews": [
                    passing_acceptance("ticket-reviewer-3", "The Ticket flow passed.")
                ],
                "run_reviews": [
                    passing_acceptance("run-reviewer-1", "The complete Run passed.")
                ],
                "run_publications": [
                    {
                        "commit_message": "feat(run): publish the delivery",
                        "pr_title": "feat(run): publish the delivery",
                        "pr_body_markdown": (
                            "## What Problem This Solves\n\nThe completed Ticket needs a final review boundary.\n\n"
                            "## Why This Change Was Made\n\nThe Run branch keeps the delivery isolated.\n\n"
                            "## User Impact\n\nMaintainers can explicitly approve the complete delivery.\n\n"
                            "## Evidence\n\nThe public CLI Run passed."
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _default_deliveries() -> tuple[Delivery, ...]:
    return (Delivery("ticket:2", DeliveryKind.TICKET, 2, "ticket-v1", Commit("base", "base-tree"),
                     "refs/heads/ticket", "refs/heads/run"),)

def _fixture_source(deliveries: tuple[Delivery, ...], parent: int = 1) -> Requirements:
    numbers = {item.key: item.issue for item in deliveries}
    return Requirements(ParentIssue(parent, "Parent", "Original query requirement", tuple(numbers.values())),
        tuple(Issue(item.issue, "Ticket", item.revision, "OPEN", frozenset({"ready-for-agent"}),
                    tuple(Blocker(numbers[key], "OPEN") for key in item.dependencies)) for item in deliveries))

def _create(root: Path, run_id: str = "run-query", *, language: str | None = "en",
            repository: str = "example/project", parent: int = 1,
            repository_root: Path | None = None, deliveries: tuple[Delivery, ...] | None = None) -> Kernel:
    deliveries = deliveries or _default_deliveries()
    spec = RunSpec(run_id, repository, parent, "parent-v1", "graph-v1", deliveries,
                   Policy(development_profile=Profile("recorded-development-model", "high")),
                   language=language, repository_root=str(repository_root or root / "checkout"))
    if language is None:
        # Explicit incompatible historical reader input; no runner is admitted.
        return Kernel.create(root / run_id / "kernel.sqlite3", spec)
    inputs = fixture_inputs()
    profiles = resolve_profiles(preset="economy", overrides={
        "development_model": "recorded-development-model", "development_effort": "high"})
    final = Delivery("query-final", DeliveryKind.FINAL_RUN, parent, "unread", deliveries[0].base,
                     deliveries[0].target_ref, "refs/heads/query-main")
    return Kernel.create(root / run_id / "kernel.sqlite3", replace(spec, inputs=replace(inputs, profiles=profiles),
        deliveries=(*deliveries, final)), requirements=_fixture_source(deliveries, parent))

def _published(root: Path, run_id: str = "run-query", *, repository: str = "example/project",
               repository_root: Path | None = None, parent: int = 1) -> Kernel:
    delivery = Delivery(f"parent:{parent}", DeliveryKind.PARENT_ONLY, parent, "requirement", Commit("base", "tree"),
                        "refs/heads/parent", "refs/heads/main")
    spec = RunSpec(run_id, repository, parent, "parent", "graph", (delivery,), language="en",
                   repository_root=str(repository_root or root.parent / "repository"))
    return Kernel.create_task(root / run_id / "kernel.sqlite3", spec,
                              requirements=Requirements(ParentIssue(parent, "Parent", "Requirement"), ()))

def _published_profile_task(git_repo: Path) -> Kernel:
    workspace = ManagedWorkspace.for_repository('example/project')
    root = workspace.ensure_path(str(git_repo))
    defaults = UserDefaultsStore()
    defaults.configure(language='en', profile={'development_model': 'original-development'})
    base = Commit(subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
                  subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=root, text=True).strip())
    delivery = Delivery('parent:1', DeliveryKind.PARENT_ONLY, 1, 'parent-v1', base,
                        'refs/heads/parent', 'refs/heads/main')
    spec = freeze_creation(RunSpec('profile-public', workspace.repository, 1, 'parent-v1', 'graph-v1',
        (delivery,), repository_root=str(workspace.repository_root)), defaults)
    return Kernel.create_task(workspace.state_root / spec.run_id / 'kernel.sqlite3', spec,
        requirements=Requirements(ParentIssue(1, 'Profile Parent', 'Frozen original requirement'), ()))
