#!/bin/sh

set -eu

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)

if ! command -v python3 >/dev/null 2>&1; then
    echo "agent-run install: CPython 3.11 or newer (python3) is required" >&2
    exit 1
fi

exec python3 "$SOURCE_DIR/src/agent_run/runner_installer.py" "$@" --source "$SOURCE_DIR"
