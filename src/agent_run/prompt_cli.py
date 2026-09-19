"""Personal Markdown methods and side-effect-free execution Prompt previews."""
from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
from typing import Any

from agent_run import prompt_resources
from agent_run.development_prompts import development_prompt
from agent_run.prompt_context import structured_output_repair_prompt
from agent_run.publication_prompts import publication_continuation_prompt, publication_prompt
from agent_run.reviewer_prompts import review_continuation_prompt, review_prompt


def add_parser(commands: Any) -> None:
    parser = commands.add_parser("prompts", help="初始化个人方法、查看默认差异和预览实际 Prompt")
    actions = parser.add_subparsers(dest="prompts_command", required=True)
    for name, help_text in (("init", "补齐五份个人方法，保留已有文件"),
                            ("diff", "只读比较个人方法与当前内置默认")):
        action = actions.add_parser(name, help=help_text)
        action.add_argument("--json", action="store_true", dest="as_json")
    preview = actions.add_parser("preview", help="只读组装请求的最终 Prompt，不调用 Agent")
    preview.add_argument("--request", required=True, type=Path, help="实际角色请求 JSON 对象文件")
    preview.add_argument("--role", required=True,
                         choices=("development", "review", "publication", "final-publication", "output-repair"))
    preview.add_argument("--continuation", action="store_true", help="预览同轮 Resume 的续接指令")
    preview.add_argument("--json", action="store_true", dest="as_json")


def execute(parsed: argparse.Namespace) -> int:
    if parsed.prompts_command == "preview":
        request = json.loads(parsed.request.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise ValueError("Prompt 请求必须为 JSON 对象")
        if "_prompt_resources" not in request:
            request["_prompt_resources"] = prompt_resources.resolve_resources()
        prompt_resources.validate_resources(request["_prompt_resources"])
        prompt = _preview(request, parsed.role, parsed.continuation)
        result: dict[str, Any] = {"result": "prompt_preview", "role": parsed.role, "prompt": prompt}
        plain = prompt
    else:
        directory = prompt_resources.personal_method_directory()
        defaults = prompt_resources.builtin_resources()
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
            effective = prompt_resources.resolve_resources()
            differences = {}
            for name in prompt_resources.METHOD_NAMES:
                key = f"methods/{name}"
                differences[f"{name}.md"] = "".join(difflib.unified_diff(
                    defaults[key].splitlines(keepends=True), effective[key].splitlines(keepends=True),
                    fromfile=f"builtin/{name}.md", tofile=str(directory / f"{name}.md"),
                ))
            result = {"result": "prompts_diff", "directory": str(directory), "differences": differences}
            plain = "\n".join(value for value in differences.values() if value) or "个人方法与当前内置默认一致。"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True) if parsed.as_json else plain)
    return 0


def _preview(request: dict[str, Any], role: str, continuation: bool) -> str:
    if role == "output-repair":
        output_name = request.get("output_name")
        contract_error = request.get("contract_error")
        if not isinstance(output_name, str) or not isinstance(contract_error, str):
            raise ValueError("输出格式修复需要 output_name 和 contract_error 字符串")
        if continuation:
            raise ValueError("输出格式修复不支持 --continuation")
        return structured_output_repair_prompt(output_name, contract_error, request=request)
    if role == "development":
        return development_prompt(request, force_continuation=continuation)
    if role == "review":
        return review_continuation_prompt(request) if continuation else review_prompt(request)
    build = publication_continuation_prompt if continuation else publication_prompt
    return build(request, final_run=role == "final-publication")
