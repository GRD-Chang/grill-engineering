"""Language-aware argparse presentation; argument names and values remain literal."""
from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from typing import Any, NoReturn

from agent_run.cli_messages import cli_message


class HelpFormatter(argparse.HelpFormatter):
    def start_section(self, heading: str | None) -> None:
        if heading in {"positional arguments", "options", "optional arguments"}:
            heading = cli_message("cli.parser.positional" if heading == "positional arguments"
                                  else "cli.parser.options")
        super().start_section(heading)

    def _format_usage(
        self, usage: str | None, actions: Iterable[argparse.Action],
        groups: Iterable[argparse._MutuallyExclusiveGroup], prefix: str | None,
    ) -> str:
        return super()._format_usage(usage, actions, groups,
                                     prefix if prefix is not None else cli_message("cli.parser.usage"))


class ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", HelpFormatter)
        super().__init__(*args, **kwargs)
        for action in self._actions:
            if isinstance(action, argparse._HelpAction):
                action.help = cli_message("cli.parser.help")

    def error(self, message: str) -> NoReturn:
        # argparse provides no per-parser gettext callback. Translate only its
        # fixed grammar; captured option names, choices, and user values stay raw.
        for pattern, key in (
            (r"unrecognized arguments: (.*)", "unrecognized"),
            (r"the following arguments are required: (.*)", "required"),
            (r"argument (.*?): invalid choice: (.*?) \(choose from (.*)\)", "choice"),
            (r"argument (.*?): expected one argument", "one_argument"),
            (r"argument (.*?): not allowed with argument (.*)", "exclusive"),
            (r"one of the arguments (.*) is required", "one_required"),
        ):
            match = re.fullmatch(pattern, message, re.DOTALL)
            if match:
                message = cli_message(f"cli.parser.{key}", **{
                    f"value{index}": value for index, value in enumerate(match.groups())
                })
                break
        self.print_usage(sys.stderr)
        self.exit(2, cli_message("cli.parser.error", program=self.prog, message=message) + "\n")
