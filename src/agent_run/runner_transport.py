"""Per-command Runner credentials; no process-wide login or Git configuration."""
from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypedDict

from agent_run.runner_credentials import runner_credential_environment
from agent_run.git_errors import GitError


class RunnerOptions(TypedDict, total=False):
    runner_identity: Mapping[str, Any]


def runner_options(identity: Mapping[str, Any] | None) -> RunnerOptions:
    return {"runner_identity": identity} if identity else {}


def runner_environment(identity: Mapping[str, Any] | None) -> dict[str, str]:
    from agent_run.git_output import git_environment

    return runner_credential_environment(identity or {"source": "host"}, git_environment())


def runner_git_environment(
    identity: Mapping[str, Any] | None, *, cwd: Path | None = None,
) -> dict[str, str]:
    environment = runner_environment(identity)
    if not identity or identity.get("source") == "host":
        return environment
    # Command-local settings override helpers without writing tokens into argv,
    # files, remote URLs, or persistent config. GitHub SSH remotes use HTTPS.
    helper = ('!f() { host=; while IFS= read -r line && test -n "$line"; do '
              'case "$line" in host=*) host=${line#host=};; esac; done; '
              'if test "$1" = get && test "$host" = github.com; then '
              'printf "username=x-access-token\\npassword=%s\\n" "$GH_TOKEN"; fi; }; f')
    # URL-scoped local entries can outrank generic resets. Reset every such
    # authentication key before installing the selected helper. This is a
    # bounded local query and does not consume Runner credentials recursively.
    from agent_run.git_output import run_git

    local_config = run_git([
        "git", "config", "--local", "--includes", "--null", "--get-regexp",
        r"^(credential(\..*)?\.helper|http(\..*)?\.extraheader|remote\..*\.(url|pushurl)|url\..*\.(insteadof|pushinsteadof))$",
    ], cwd=cwd or Path.cwd())
    settings: list[tuple[str, str]] = []
    for record in local_config.stdout.split("\0"):
        if not record:
            continue
        key, _, value = record.partition("\n")
        if key.startswith(("remote.", "url.")):
            # Git uses URL userinfo before consulting a credential helper.
            # Refuse it before remote dispatch, including push URLs/rewrite keys.
            if re.search(r"https?://[^/\s]*@", key + " " + value, re.IGNORECASE):
                raise GitError("Runner identity refuses Git remote URLs containing credentials")
        else:
            settings.append((key, ""))
    settings.extend([
        ("credential.helper", ""), ("credential.helper", helper),
        ("credential.interactive", "false"), ("http.extraHeader", ""),
        ("http.https://github.com/.extraHeader", ""),
        ("url.https://github.com/.insteadOf", "git@github.com:"),
        ("url.https://github.com/.insteadOf", "ssh://git@github.com/"),
        ("url.https://github.com/.insteadOf", "ssh://github.com/"),
    ])
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "",
                        "SSH_ASKPASS": "", "GIT_ALLOW_PROTOCOL": "https",
                        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_CONFIG_COUNT": str(len(settings))})
    for index, (key, value) in enumerate(settings):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return environment


def redact_transport_output(value: str, environment: Mapping[str, str]) -> str:
    token = environment.get("GH_TOKEN")
    if token:
        for secret in (token, base64.b64encode(f"x-access-token:{token}".encode()).decode()):
            value = value.replace(secret, "[REDACTED]")
    return value
