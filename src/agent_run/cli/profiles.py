"""Configure a published Run through its existing immutable Profile authority."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_run.errors import AgentProfileError
from agent_run.adapters.workspace import workspace_for_root
from agent_run.errors import error_message
from agent_run.contracts.copy import text

from agent_run.cli import settings as personal_settings
from agent_run.queries.execution import recorded_profile
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.cli.render import safe_text


def add_parser(commands: Any, language: str) -> None:
    parser = commands.add_parser("configure", aliases=["config", "profile"], language=language,
                                 help=text("cli.configure_help", language=language))
    parser.add_argument("run_id")
    parser.add_argument("--repo", help=text("cli.repo_help", language=language))
    parser.add_argument("--state-dir", type=Path, help=text("cli.state_dir_help", language=language))
    personal_settings._profile_options(parser, language)
    parser.add_argument("--json", action="store_true", dest="as_json")


def execute(parsed: argparse.Namespace) -> int:
    from agent_run.cli.selection import _candidates, _select

    selection = argparse.Namespace(**vars(parsed))
    selection.parent = None
    if selection.run_id.isdecimal():
        selection.parent = int(selection.run_id)
        selection.run_id = None
    candidate = _select(selection, _candidates(selection))[0]
    database = Path(candidate["database"]).resolve()
    kernel = Kernel(database)
    with kernel.storage.transaction(write=False) as tx:
        run = tx.one("SELECT id,repository,parent_issue,language,repository_root FROM runs")
        if run["language"] not in {"zh", "en"} or run["repository_root"] is None:
            raise KernelError("creation_metadata_missing")
        parsed._run_language = language = run["language"]
        if run["id"] != candidate["run_id"] or run["repository"].lower() != candidate["repository"].lower():
            raise KernelError("managed_state_identity")
        workspace = workspace_for_root(Path(run["repository_root"]))
        if not database.is_relative_to(workspace.state_root.resolve()):
            raise KernelError("managed_state_identity")
        run_id, repository, parent = run["id"], run["repository"], run["parent_issue"]
    overrides = {f"{role}_{field}": getattr(parsed, f"{role}_{field}")
                 for role in ("development", "review", "publication") for field in ("model", "effort")
                 if getattr(parsed, f"{role}_{field}") is not None}
    if parsed.publication_from_development or parsed.publication_reference == "development":
        overrides["publication_from_development"] = True
    if parsed.profile_preset is None and not overrides:
        raise AgentProfileError(error_message("profile.error.option_required", audit="configure requires a preset or an explicit role override"))
    revision = kernel.configure_task_profiles(uuid4().hex, preset=parsed.profile_preset, overrides=overrides)
    with kernel.storage.transaction(write=False) as tx:
        profile = recorded_profile(tx, revision)
    output = {"result": "configured", "run_id": run_id, "profile_revision": revision,
              "preset": profile["preset"], "profiles": profile["profiles"]}
    if parsed.as_json:
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    else:
        for message, values in (("cli.repository", {"repository": repository}),
                                ("cli.parent_issue", {"parent": parent}),
                                ("cli.profile_saved", {"revision": revision}),
                                ("cli.profile_no_execution", {})):
            print(safe_text(text(message, language=language, **values)))
    return 0
