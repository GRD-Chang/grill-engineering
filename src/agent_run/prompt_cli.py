"""Personal Markdown methods and side-effect-free execution Prompt previews."""
from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
from typing import Any

from agent_run import prompt_resources
from agent_run.messages import text
from agent_run.user_defaults import UserDefaultsError
from agent_run.development_prompts import development_prompt
from agent_run.prompt_context import structured_output_repair_prompt
from agent_run.publication_prompts import publication_continuation_prompt, publication_prompt
from agent_run.reviewer_prompts import review_continuation_prompt, review_prompt


def add_parser(commands: Any) -> None:
    try:
        language = prompt_resources.selected_language()
    except (UserDefaultsError, OSError):
        language = "zh"
    def message(key: str) -> str:
        return text(f"prompts.{key}", language=language)
    parser = commands.add_parser("prompts", help=message("help"))
    actions = parser.add_subparsers(dest="prompts_command", required=True)
    for name, help_text in (("init", message("init_help")),
                            ("diff", message("diff_help"))):
        action = actions.add_parser(name, help=help_text)
        action.add_argument("--json", action="store_true", dest="as_json")
    preview = actions.add_parser("preview", help=message("preview_help"))
    preview.add_argument("--request", required=True, type=Path, help=message("request_help"))
    preview.add_argument("--role", required=True,
                         choices=("development", "review", "publication", "final-publication", "output-repair"))
    preview.add_argument("--continuation", action="store_true", help=message("continuation_help"))
    preview.add_argument("--json", action="store_true", dest="as_json")


def execute(parsed: argparse.Namespace) -> int:
    if parsed.prompts_command == "preview":
        request = json.loads(parsed.request.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise ValueError(text("prompts.request_object", language=prompt_resources.selected_language()))
        if "_prompt_resources" not in request:
            request["_prompt_resources"] = prompt_resources.resolve_resources(request.get("language"))
        prompt_resources.validate_resources(request["_prompt_resources"])
        prompt = _preview(request, parsed.role, parsed.continuation)
        result: dict[str, Any] = {"result": "prompt_preview", "role": parsed.role, "prompt": prompt}
        plain = prompt
    else:
        language = prompt_resources.selected_language()
        directory = prompt_resources.personal_method_directory(language)
        defaults = prompt_resources.builtin_resources(language)
        if parsed.prompts_command == "init":
            directory.mkdir(parents=True, exist_ok=True)
            created, preserved = [], []
            for name in prompt_resources.METHOD_NAMES:
                path = directory / f"{name}.md"
                try:
                    with path.open("x", encoding="utf-8") as stream:
                        stream.write(defaults[f"methods/{name}"])
                except FileExistsError:
                    preserved.append(path.name)
                else:
                    created.append(path.name)
            result = {"result": "prompts_initialized", "directory": str(directory),
                      "created": created, "preserved": preserved}
            plain = json.dumps(result, ensure_ascii=False, indent=2)
        else:
            effective = prompt_resources.resolve_resources(language)
            differences = {}
            for name in prompt_resources.METHOD_NAMES:
                key = f"methods/{name}"
                differences[f"{name}.md"] = "".join(difflib.unified_diff(
                    defaults[key].splitlines(keepends=True), effective[key].splitlines(keepends=True),
                    fromfile=f"builtin/{name}.md", tofile=str(directory / f"{name}.md"),
                ))
            result = {"result": "prompts_diff", "directory": str(directory), "differences": differences}
            plain = "\n".join(value for value in differences.values() if value) or text("prompts.no_diff", language=language)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True) if parsed.as_json else plain)
    return 0


def _preview(request: dict[str, Any], role: str, continuation: bool) -> str:
    if role == "output-repair":
        output_name = request.get("output_name")
        contract_error = request.get("contract_error")
        if not isinstance(output_name, str) or not isinstance(contract_error, str):
            raise ValueError(text("prompts.repair_fields", language=prompt_resources.selected_language(request.get("language"))))
        if continuation:
            raise ValueError(text("prompts.repair_continuation", language=prompt_resources.selected_language(request.get("language"))))
        return structured_output_repair_prompt(output_name, contract_error, request=request)
    if role == "development":
        return development_prompt(request, force_continuation=continuation)
    if role == "review":
        return review_continuation_prompt(request) if continuation else review_prompt(request)
    build = publication_continuation_prompt if continuation else publication_prompt
    return build(request, final_run=role == "final-publication")
