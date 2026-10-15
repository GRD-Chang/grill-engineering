"""共享测试支撑：cli_actions；每次调用保持独立状态。"""

from __future__ import annotations

import json
from pathlib import Path
from support.cli_fixtures import run_agents
from support.cli_payloads import parent_publication, passing_acceptance, publication


def _parent_agents(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-developer",
                        "summary": "Implemented the Parent delivery.",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [parent_publication()],
                "reviews": [passing_acceptance("parent-reviewer", "passed")],
            }
        ),
        encoding="utf-8",
    )
    return path


def _revision_agents(path: Path) -> Path:
    template_path = path.with_name("revision-template.json")
    template = run_agents(template_path)
    template_data = json.loads(template.read_text(encoding="utf-8"))
    label = path.stem
    data = {
        "developments": [
            {
                "expected_thread_id": None,
                "thread_id": f"{label}-developer",
                "summary": "Applied the requested Run revision.",
                "write_files": {"run-revision.txt": f"{label}\n"},
            }
        ],
        "publications": [publication()],
        "reviews": [],
        "run_reviews": [
            passing_acceptance(f"{label}-reviewer", "The revision passed.")
        ],
        "run_publications": [dict(template_data["run_publications"][0])],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    template.unlink()
    return path
