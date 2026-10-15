"""共享测试支撑：user_defaults；每次调用保持独立状态。"""

from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from typing import Any
from agent_run import cli


def settings(*arguments: str) -> tuple[int, dict[str, Any]]:
    output = StringIO()
    with redirect_stdout(output):
        code = cli.main(["settings", *arguments, "--json"])
    return code, json.loads(output.getvalue())
