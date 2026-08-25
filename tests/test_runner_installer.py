from __future__ import annotations

import json
import fcntl
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agent_run.runner_probe import RunnerProbeBackend, RunnerProbeError
from agent_run import runner_installer
from conftest import write_fixture
from support.fast_runner_installer import build_candidate as fast_build_candidate


PROJECT_ROOT = Path(__file__).parents[1]


def _source_tree(tmp_path: Path, *, real_install: bool = False) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(PROJECT_ROOT / "install.sh", source / "install.sh")
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", source / "pyproject.toml")
    shutil.copytree(
        PROJECT_ROOT / "src",
        source / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )
    if not real_install:
        shutil.copy2(
            PROJECT_ROOT / "tests" / "support" / "fast_runner_installer.py",
            source / "fast_runner_installer.py",
        )
        (source / "install.sh").write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            'SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)\n'
            'exec python3 "$SOURCE_DIR/fast_runner_installer.py" "$@" '
            '--source "$SOURCE_DIR"\n',
            encoding="utf-8",
        )
    (source / "install.sh").chmod(0o755)
    return source


@pytest.fixture
def fast_in_process_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_installer, "_build_candidate", fast_build_candidate)


def _fake_codex(
    tmp_path: Path,
    *,
    status: str = "ok",
    behavior: str = "success",
) -> tuple[Path, Path, Path]:
    directory = tmp_path / "bin"
    directory.mkdir()
    count = tmp_path / "codex-count"
    status_file = tmp_path / "codex-status"
    status_file.write_text(status, encoding="utf-8")
    behavior_file = tmp_path / "codex-behavior"
    behavior_file.write_text(behavior, encoding="utf-8")
    pid_file = tmp_path / "codex-pid"
    cwd_file = tmp_path / "codex-cwd"
    path_file = tmp_path / "codex-path"
    script = directory / "codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import pathlib\n"
        "import sys\n"
        "import time\n"
        f"count = pathlib.Path({str(count)!r})\n"
        f"status_file = pathlib.Path({str(status_file)!r})\n"
        f"behavior_file = pathlib.Path({str(behavior_file)!r})\n"
        f"pid_file = pathlib.Path({str(pid_file)!r})\n"
        f"cwd_file = pathlib.Path({str(cwd_file)!r})\n"
        f"path_file = pathlib.Path({str(path_file)!r})\n"
        "current = int(count.read_text() if count.exists() else '0')\n"
        "count.write_text(str(current + 1))\n"
        "pid_file.write_text(str(os.getpid()))\n"
        "cwd_file.write_text(str(pathlib.Path.cwd()))\n"
        "path_file.write_text(os.environ['PATH'])\n"
        "behavior = behavior_file.read_text()\n"
        "if behavior in {'nonzero', 'not-logged-in'}:\n"
        "    sys.exit(7)\n"
        "if behavior == 'missing':\n"
        "    sys.exit(0)\n"
        "if behavior == 'large':\n"
        "    output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        "    output.write_bytes(b'x' * (16 * 1024 + 1))\n"
        "    sys.exit(0)\n"
        "if behavior == 'timeout':\n"
        "    time.sleep(130)\n"
        "if behavior == 'sleep':\n"
        "    time.sleep(2)\n"
        "schema = pathlib.Path(sys.argv[sys.argv.index('--output-schema') + 1])\n"
        "assert json.loads(schema.read_text()) == {\n"
        "    'type': 'object',\n"
        "    'additionalProperties': False,\n"
        "    'properties': {'status': {'type': 'string', 'enum': ['ok']}},\n"
        "    'required': ['status'],\n"
        "}\n"
        "if behavior == 'wrong-result':\n"
        "    output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        "    output.write_text(json.dumps({'status': 'not-ok'}))\n"
        "    sys.exit(0)\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        "output.write_text(json.dumps({'status': status_file.read_text()}))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return directory, count, status_file


def _run(
    source: Path,
    home: Path,
    fake_bin: Path,
    *arguments: str,
    path: str | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "PATH": path if path is not None else f"{fake_bin}{os.pathsep}{environment['PATH']}",
        }
    )
    return subprocess.run(
        [str(source / "install.sh"), *arguments],
        cwd=cwd or source,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _path_without_codex(tmp_path: Path) -> str:
    directory = tmp_path / "path-without-codex"
    directory.mkdir()
    (directory / "python3").symlink_to(sys.executable)
    dirname = shutil.which("dirname")
    assert dirname is not None
    (directory / "dirname").symlink_to(dirname)
    return str(directory)


def _data_root(home: Path) -> Path:
    return home / "data" / "agent-run"


def _active_snapshot(home: Path) -> Path:
    return (_data_root(home) / "active" / "current").resolve()


def _manifest(snapshot: Path) -> dict[str, object]:
    loaded = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_snapshot_entrypoints_rewrite_candidate_paths_inside_wrappers(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "data" / "agent-run" / "staging" / "candidate-123"
    snapshot = tmp_path / "data" / "agent-run" / "snapshots" / "sha256:test"
    bin_directory = snapshot / "bin"
    bin_directory.mkdir(parents=True)
    wrapper = bin_directory / "agent-run"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"exec '{candidate}/bin/python' -m agent_run.cli \"$@\"\n",
        encoding="utf-8",
    )

    runner_installer._rewrite_snapshot_entrypoints(snapshot, candidate)

    rewritten = wrapper.read_text(encoding="utf-8")
    assert str(candidate) not in rewritten
    assert str(snapshot) in rewritten


def _file_tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _managed_state_snapshot(
    home: Path, *, config: Path, locator: Path, delivery: Path
) -> dict[str, object]:
    data_root = _data_root(home)
    active = data_root / "active"
    generation = active.resolve()
    return {
        "active": os.readlink(active),
        "generation_links": {
            name: os.readlink(generation / name)
            for name in ("current", "previous")
            if (generation / name).is_symlink()
        },
        "stable_entry": os.readlink(home / ".local" / "bin" / "agent-run"),
        "snapshots": sorted(path.name for path in (data_root / "snapshots").iterdir()),
        "generations": sorted(path.name for path in (data_root / "generations").iterdir()),
        "staging": sorted(path.name for path in (data_root / "staging").iterdir()),
        "profile": (home / ".profile").read_bytes(),
        "app_profile": config.read_bytes(),
        "locator": locator.read_bytes(),
        "delivery_state": _file_tree(delivery / ".agent-run"),
    }


def _install_a_and_b(source: Path, home: Path, fake_bin: Path) -> None:
    for version in ("a", "b"):
        (source / "src" / "agent_run" / "__init__.py").write_text(
            f"__version__ = '{version}'\n", encoding="utf-8"
        )
        result = _run(source, home, fake_bin)
        assert result.returncode == 0, result.stderr


def _write_public_state_fixtures(tmp_path: Path, home: Path) -> tuple[Path, Path, Path]:
    config = home / "config" / "agent-run" / "github-app.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('{"profile":"keep"}\n', encoding="utf-8")
    locator = home / "state" / "agent-run" / "run-locator.json"
    locator.parent.mkdir(parents=True, exist_ok=True)
    locator.write_text('{"entries":[]}\n', encoding="utf-8")
    delivery = tmp_path / "delivery"
    (delivery / ".agent-run" / "runs").mkdir(parents=True)
    (delivery / ".agent-run" / "runs" / "existing.json").write_text(
        '{"run_id":"existing"}\n', encoding="utf-8"
    )
    return config, locator, delivery


def _assert_process_gone(pid_file: Path) -> None:
    pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    os.kill(pid, signal.SIGKILL)
    raise AssertionError("Codex process survived failed public installation")


def test_install_freezes_source_and_reinstall_same_active_is_idempotent(
    tmp_path: Path,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    first = _run(source, home, fake_bin)
    assert first.returncode == 0, first.stderr
    first_snapshot = _active_snapshot(home)
    first_manifest = _manifest(first_snapshot)
    first_identity = first_manifest["content_identity"]
    assert first_manifest["source_provenance"] == {"kind": "source-directory"}
    assert int(count.read_text()) == 1
    assert (home / ".local" / "bin" / "agent-run").is_symlink()
    public_entry = (home / ".local" / "bin" / "agent-run").resolve()
    assert str(_data_root(home) / "staging") not in public_entry.read_text(
        encoding="utf-8"
    )
    command = subprocess.run(
        [str(home / ".local" / "bin" / "agent-run"), "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert command.returncode == 0
    assert "agent-run" in command.stdout

    (source / "src" / "agent_run" / "__init__.py").write_text(
        "\"\"\"changed source after install\"\"\"\n__version__ = 'changed'\n",
        encoding="utf-8",
    )
    second = _run(source, home, fake_bin)
    assert second.returncode == 0, second.stderr
    assert int(count.read_text()) == 2
    assert _manifest(first_snapshot)["content_identity"] == first_identity
    second_identity = _manifest(_active_snapshot(home))["content_identity"]

    repeat = _run(source, home, fake_bin)
    assert repeat.returncode == 0, repeat.stderr
    assert int(count.read_text()) == 2
    assert _manifest(_active_snapshot(home))["content_identity"] == second_identity


def test_install_failure_preserves_active_and_cleans_candidate(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    fake_bin, count, _status_file = _fake_codex(tmp_path, status="not-ok")
    home = tmp_path / "home"
    home.mkdir()

    initial = _run(source, home, fake_bin)
    assert initial.returncode != 0
    assert not (_data_root(home) / "active").exists()
    assert not list((_data_root(home) / "staging").glob("*") if (_data_root(home) / "staging").exists() else [])
    assert int(count.read_text()) == 1


def test_build_failure_preserves_no_active_runner_and_cleans_staging(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (source / ".agent-run-test-build-failure").touch()

    result = _run(source, home, fake_bin)

    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert not (_data_root(home) / "active").exists()
    assert not list((_data_root(home) / "staging").glob("*"))
    assert not list((_data_root(home) / "snapshots").glob("*"))


def test_missing_codex_preserves_no_active_runner(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    result = _run(source, home, fake_bin, path=_path_without_codex(tmp_path))

    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "Codex executable is not available on PATH" in result.stderr
    assert not (_data_root(home) / "active").exists()
    assert not list((_data_root(home) / "staging").glob("*"))
    assert not list((_data_root(home) / "snapshots").glob("*"))


@pytest.mark.parametrize("behavior", ["nonzero", "missing", "large"])
def test_probe_process_failures_preserve_no_active_runner(
    tmp_path: Path, behavior: str
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, count, _status_file = _fake_codex(tmp_path, behavior=behavior)
    home = tmp_path / "home"
    home.mkdir()

    result = _run(source, home, fake_bin)

    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert len(result.stderr) < 2048
    assert not (_data_root(home) / "active").exists()
    assert not list((_data_root(home) / "staging").glob("*"))
    assert int(count.read_text()) == 1


def test_probe_timeout_terminates_its_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate" / "lib" / "python3.11" / "site-packages" / "agent_run"
    candidate.mkdir(parents=True)
    (candidate / "__init__.py").write_text("__version__ = 'probe'\n", encoding="utf-8")
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        "#!/usr/bin/python3\nimport time\ntime.sleep(10)\n", encoding="utf-8"
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(RunnerProbeError, match="timed out"):
        RunnerProbeBackend(timeout_seconds=0.05).check(tmp_path / "candidate")


def test_candidate_probe_timeout_cleans_nested_process_and_probe_temporary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_tree(tmp_path)
    installer = source / "src" / "agent_run" / "runner_installer.py"
    installer.write_text(
        installer.read_text(encoding="utf-8").replace(
            "_CANDIDATE_PROBE_TIMEOUT_SECONDS = 150.0",
            "_CANDIDATE_PROBE_TIMEOUT_SECONDS = 0.5",
        ),
        encoding="utf-8",
    )
    probe = source / "src" / "agent_run" / "runner_probe.py"
    probe.write_text(
        probe.read_text(encoding="utf-8").replace(
            "PROBE_TIMEOUT_SECONDS = 120.0", "PROBE_TIMEOUT_SECONDS = 10.0"
        ),
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    pid_file = tmp_path / "codex-pid"
    codex = fake_bin / "codex"
    codex.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import pathlib\n"
        "import time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    probe_tmp = tmp_path / "probe-tmp"
    probe_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(probe_tmp))
    home = tmp_path / "home"
    home.mkdir()

    result = _run(
        source,
        home,
        fake_bin,
        path=f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    )

    assert result.returncode == 1
    assert "Compatibility Check timed out" in result.stderr
    assert pid_file.is_file()
    pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        os.kill(pid, signal.SIGKILL)
        raise AssertionError("nested Codex process survived candidate probe timeout")
    assert not list(probe_tmp.glob("agent-run-candidate-probe-*"))
    assert not list(probe_tmp.glob("agent-run-probe-*"))
    assert not (_data_root(home) / "active").exists()
    assert not list((_data_root(home) / "staging").glob("*"))
    assert not list((_data_root(home) / "snapshots").glob("*"))


def test_candidate_probe_inner_timeout_kills_forked_codex_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_tree(tmp_path)
    installer = source / "src" / "agent_run" / "runner_installer.py"
    installer.write_text(
        installer.read_text(encoding="utf-8").replace(
            "_CANDIDATE_PROBE_TIMEOUT_SECONDS = 150.0",
            "_CANDIDATE_PROBE_TIMEOUT_SECONDS = 10.0",
        ),
        encoding="utf-8",
    )
    probe = source / "src" / "agent_run" / "runner_probe.py"
    probe.write_text(
        probe.read_text(encoding="utf-8").replace(
            "PROBE_TIMEOUT_SECONDS = 120.0", "PROBE_TIMEOUT_SECONDS = 0.2"
        ),
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    parent_pid_file = tmp_path / "codex-parent-pid"
    child_pid_file = tmp_path / "codex-child-pid"
    codex = fake_bin / "codex"
    codex.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import pathlib\n"
        "import time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        f"    pathlib.Path({str(child_pid_file)!r}).write_text(str(os.getpid()))\n"
        "    time.sleep(10)\n"
        "else:\n"
        f"    pathlib.Path({str(parent_pid_file)!r}).write_text(str(os.getpid()))\n"
        "    time.sleep(10)\n",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    probe_tmp = tmp_path / "probe-tmp"
    probe_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(probe_tmp))
    home = tmp_path / "home"
    home.mkdir()

    result = _run(
        source,
        home,
        fake_bin,
        path=f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    )

    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert child_pid_file.is_file()
    survivors: list[int] = []
    for pid_file in (parent_pid_file, child_pid_file):
        if not pid_file.is_file():
            continue
        pid = int(pid_file.read_text(encoding="utf-8"))
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        survivors.append(pid)
    for pid in survivors:
        os.kill(pid, signal.SIGKILL)
    assert not survivors, "forked Codex process survived inner probe timeout"
    assert not list(probe_tmp.glob("agent-run-candidate-probe-*"))
    assert not list(probe_tmp.glob("agent-run-probe-*"))
    assert not (_data_root(home) / "active").exists()
    assert not list((_data_root(home) / "staging").glob("*"))
    assert not list((_data_root(home) / "snapshots").glob("*"))


def test_compatibility_check_executes_candidate_probe_code(
    tmp_path: Path,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    first = _run(source, home, fake_bin)
    assert first.returncode == 0, first.stderr
    probe_cwd = Path((tmp_path / "codex-cwd").read_text(encoding="utf-8"))
    assert probe_cwd.name == "empty"
    assert source not in probe_cwd.parents
    assert (tmp_path / "codex-path").read_text(encoding="utf-8").split(os.pathsep)[0] == str(
        fake_bin
    )
    first_identity = _manifest(_active_snapshot(home))["content_identity"]
    probe = source / "src" / "agent_run" / "runner_probe.py"
    probe.write_text(
        probe.read_text(encoding="utf-8").replace(
            "raise SystemExit(main())", "raise SystemExit(3)"
        ),
        encoding="utf-8",
    )

    failed = _run(source, home, fake_bin)

    assert failed.returncode == 1
    assert "Traceback" not in failed.stderr
    assert int(count.read_text()) == 1
    assert _manifest(_active_snapshot(home))["content_identity"] == first_identity
    assert sorted(path.name for path in (_data_root(home) / "snapshots").iterdir()) == [
        first_identity
    ]
    assert not list((_data_root(home) / "staging").glob("*"))


def test_candidate_probe_ignores_pythonpath_outside_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    first = _run(source, home, fake_bin)
    assert first.returncode == 0, first.stderr

    shadow_package = tmp_path / "shadow" / "agent_run"
    shadow_package.mkdir(parents=True)
    (shadow_package / "__init__.py").write_text("\n", encoding="utf-8")
    (shadow_package / "runner_probe.py").write_text(
        "raise SystemExit(7)\n", encoding="utf-8"
    )
    monkeypatch.setenv("PYTHONPATH", str(shadow_package.parent.parent))
    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'second'\n", encoding="utf-8"
    )

    result = _run(source, home, fake_bin)

    assert result.returncode == 0, result.stderr
    assert int(count.read_text()) == 2


def test_compatibility_check_rejects_schema_drift_in_candidate_probe(
    tmp_path: Path,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    probe = source / "src" / "agent_run" / "runner_probe.py"
    probe.write_text(
        probe.read_text(encoding="utf-8").replace('"enum": ["ok"]', '"enum": ["wrong"]'),
        encoding="utf-8",
    )

    result = _run(source, home, fake_bin)

    assert result.returncode == 1
    assert int(count.read_text()) == 1
    assert not (_data_root(home) / "active").exists()
    assert not list((_data_root(home) / "snapshots").glob("*"))


def test_git_provenance_marks_an_untracked_source_dirty(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Agent Run Tests"], cwd=source, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "agent-run-tests@example.invalid"],
        cwd=source,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "initial source"], cwd=source, check=True
    )
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    clean_provenance = runner_installer._source_provenance(source)
    assert clean_provenance["kind"] == "git"
    assert clean_provenance["dirty"] is False
    clean_result = _run(source, home, fake_bin)
    assert clean_result.returncode == 0, clean_result.stderr
    assert _manifest(_active_snapshot(home))["source_provenance"] == clean_provenance

    (source / "src" / "agent_run" / "untracked.py").write_text(
        "untracked = True\n", encoding="utf-8"
    )
    provenance = runner_installer._source_provenance(source)
    assert provenance["kind"] == "git"
    assert provenance["dirty"] is True
    result = _run(source, home, fake_bin)

    assert result.returncode == 0, result.stderr
    manifest = _manifest(_active_snapshot(home))
    assert manifest["source_provenance"] == provenance


def test_clean_git_source_is_not_polluted_by_public_install(tmp_path: Path) -> None:
    source = _source_tree(tmp_path, real_install=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Agent Run Tests"], cwd=source, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "agent-run-tests@example.invalid"],
        cwd=source,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "initial source"], cwd=source, check=True)
    before = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    assert before.stdout == ""

    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    sentinel_directory = tmp_path / "external-tool-sentinels"
    sentinel_directory.mkdir()
    markers: list[Path] = []
    for command_name in (
        "sudo",
        "apt",
        "apt-get",
        "apt-cache",
        "dnf",
        "yum",
        "pacman",
        "apk",
        "pipx",
    ):
        marker = sentinel_directory / f"{command_name}.called"
        markers.append(marker)
        command = sentinel_directory / command_name
        command.write_text(
            "#!/bin/sh\n"
            f"printf called > {str(marker)!r}\n"
            "exit 99\n",
            encoding="utf-8",
        )
        command.chmod(0o755)
    result = _run(
        source,
        home,
        fake_bin,
        path=os.pathsep.join(
            [str(sentinel_directory), str(fake_bin), os.environ["PATH"]]
        ),
    )

    assert result.returncode == 0, result.stderr
    assert not [marker for marker in markers if marker.exists()]
    stable_entry = home / ".local" / "bin" / "agent-run"
    assert stable_entry.is_symlink()
    assert stable_entry.resolve() == _active_snapshot(home) / "bin" / "agent-run"
    command = subprocess.run(
        [str(stable_entry), "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert command.returncode == 0, command.stderr
    assert "agent-run" in command.stdout
    assert _manifest(_active_snapshot(home))["source_provenance"] == {
        "kind": "git",
        "commit": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "ref": "main",
        "dirty": False,
    }
    after = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    assert after.stdout == ""
    assert not (source / "build").exists()
    assert not (source / "src" / "agent_run" / "__pycache__").exists()
    assert not list(source.glob("*.egg-info"))


def test_install_rejects_managed_paths_inside_source_without_writing_source(
    tmp_path: Path,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)

    result = _run(source, source, fake_bin)

    assert result.returncode == 1
    assert "受管 Runner 路径不能位于源码目录内" in result.stderr
    assert not (source / "data" / "agent-run").exists()
    assert not (source / ".local").exists()
    assert not (source / ".profile").exists()
    assert not (source / "src" / "agent_run" / "__pycache__").exists()


@pytest.mark.parametrize(
    "failure",
    [
        "build",
        "missing_codex",
        "not_logged_in",
        "nonzero",
        "timeout",
        "missing_output",
        "schema_rejection",
    ],
)
def test_public_install_failures_preserve_existing_state(
    tmp_path: Path, failure: str
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _install_a_and_b(source, home, fake_bin)
    config, locator, delivery = _write_public_state_fixtures(tmp_path, home)
    before = _managed_state_snapshot(
        home, config=config, locator=locator, delivery=delivery
    )

    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'c'\n", encoding="utf-8"
    )
    path: str | None = None
    if failure == "build":
        (source / ".agent-run-test-build-failure").touch()
    elif failure == "missing_codex":
        path = _path_without_codex(tmp_path)
    elif failure == "not_logged_in":
        (tmp_path / "codex-behavior").write_text("not-logged-in", encoding="utf-8")
    elif failure == "nonzero":
        (tmp_path / "codex-behavior").write_text("nonzero", encoding="utf-8")
    elif failure == "timeout":
        (tmp_path / "codex-behavior").write_text("timeout", encoding="utf-8")
        probe = source / "src" / "agent_run" / "runner_probe.py"
        probe.write_text(
            probe.read_text(encoding="utf-8").replace(
                "PROBE_TIMEOUT_SECONDS = 120.0", "PROBE_TIMEOUT_SECONDS = 0.2"
            ),
            encoding="utf-8",
        )
    elif failure == "missing_output":
        (tmp_path / "codex-behavior").write_text("missing", encoding="utf-8")
    elif failure == "schema_rejection":
        (tmp_path / "codex-behavior").write_text("wrong-result", encoding="utf-8")
    else:
        raise AssertionError(f"unhandled failure: {failure}")

    result = _run(source, home, fake_bin, path=path, cwd=delivery)

    assert result.returncode == 1, result.stderr
    assert "Traceback" not in result.stderr
    assert _managed_state_snapshot(
        home, config=config, locator=locator, delivery=delivery
    ) == before
    if failure == "timeout":
        _assert_process_gone(tmp_path / "codex-pid")


def _write_activation_fault_sitecustomize(
    directory: Path, *, fault: str, data_root: Path
) -> None:
    directory.mkdir()
    (directory / "sitecustomize.py").write_text(
        "import os\n"
        "import sys\n"
        "\n"
        "if sys.argv and sys.argv[0].endswith('runner_installer.py'):\n"
        f"    fault = {fault!r}\n"
        f"    active = {str(data_root / 'active')!r}\n"
        f"    generations = {str(data_root / 'generations')!r}\n"
        "    if fault == 'rename':\n"
        "        original_replace = os.replace\n"
        "        state = {'injected': False}\n"
        "        def replace(source, destination, *args, **kwargs):\n"
        "            result = original_replace(source, destination, *args, **kwargs)\n"
        "            if not state['injected'] and os.fspath(destination) == active:\n"
        "                state['injected'] = True\n"
        "                raise OSError('injected public activation rename failure')\n"
        "            return result\n"
        "        os.replace = replace\n"
        "    elif fault == 'sync':\n"
        "        original_open = os.open\n"
        "        state = {'injected': False}\n"
        "        def open_(path, flags, *args, **kwargs):\n"
        "            if not state['injected'] and os.fspath(path) == generations:\n"
        "                state['injected'] = True\n"
        "                raise OSError('injected public activation sync failure')\n"
        "            return original_open(path, flags, *args, **kwargs)\n"
        "        os.open = open_\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("fault", ["rename", "sync"])
def test_public_activation_faults_preserve_existing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _install_a_and_b(source, home, fake_bin)
    config, locator, delivery = _write_public_state_fixtures(tmp_path, home)
    before = _managed_state_snapshot(
        home, config=config, locator=locator, delivery=delivery
    )
    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'c'\n", encoding="utf-8"
    )

    injector = tmp_path / "fault-injector"
    _write_activation_fault_sitecustomize(
        injector, fault=fault, data_root=_data_root(home)
    )
    monkeypatch.setenv("PYTHONPATH", str(injector))
    result = _run(source, home, fake_bin, cwd=delivery)

    assert result.returncode == 1, result.stderr
    assert "Traceback" not in result.stderr
    assert _managed_state_snapshot(
        home, config=config, locator=locator, delivery=delivery
    ) == before


@pytest.mark.parametrize("termination_signal", [signal.SIGINT, signal.SIGTERM])
def test_public_signal_cleans_candidate_probe_process_and_state(
    tmp_path: Path, termination_signal: signal.Signals
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _install_a_and_b(source, home, fake_bin)
    config, locator, delivery = _write_public_state_fixtures(tmp_path, home)
    before = _managed_state_snapshot(
        home, config=config, locator=locator, delivery=delivery
    )
    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'c'\n", encoding="utf-8"
    )
    (tmp_path / "codex-behavior").write_text("sleep", encoding="utf-8")
    pid_file = tmp_path / "codex-pid"
    pid_file.unlink()
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
        }
    )
    process = subprocess.Popen(
        [str(source / "install.sh")],
        cwd=delivery,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 30
    while not pid_file.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            break
        time.sleep(0.02)
    assert pid_file.exists(), process.communicate(timeout=10)[1]

    process.send_signal(termination_signal)
    _stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stderr
    _assert_process_gone(pid_file)
    assert _managed_state_snapshot(
        home, config=config, locator=locator, delivery=delivery
    ) == before


def test_public_signal_during_post_activation_cleanup_keeps_new_generation_complete(
    tmp_path: Path,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _install_a_and_b(source, home, fake_bin)
    config, locator, delivery = _write_public_state_fixtures(tmp_path, home)
    before = _managed_state_snapshot(
        home, config=config, locator=locator, delivery=delivery
    )
    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'c'\n", encoding="utf-8"
    )
    marker = tmp_path / "post-activation-cleanup-started"
    installer = source / "src" / "agent_run" / "runner_installer.py"
    installer.write_text(
        installer.read_text(encoding="utf-8").replace(
            "            try:\n                warnings = [\n",
            f"            try:\n                Path({str(marker)!r}).write_text('ready', encoding='utf-8')\n                time.sleep(2)\n                warnings = [\n",
            1,
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
        }
    )
    process = subprocess.Popen(
        [str(source / "install.sh")],
        cwd=delivery,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 30
    while not marker.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            break
        time.sleep(0.02)
    assert marker.exists(), process.communicate(timeout=10)[1]

    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode == 0, stderr
    assert "已保留新的 Active Runner" in stdout
    after = _managed_state_snapshot(
        home, config=config, locator=locator, delivery=delivery
    )
    assert after["active"] != before["active"]
    assert after["generation_links"]["previous"] == before["generation_links"]["current"]
    assert after["profile"] == before["profile"]
    assert after["app_profile"] == before["app_profile"]
    assert after["locator"] == before["locator"]
    assert after["delivery_state"] == before["delivery_state"]
    assert after["staging"] == []


def test_public_signal_before_candidate_creation_cleans_preexisting_staging(
    tmp_path: Path,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _install_a_and_b(source, home, fake_bin)
    config, locator, delivery = _write_public_state_fixtures(tmp_path, home)
    before = _managed_state_snapshot(
        home, config=config, locator=locator, delivery=delivery
    )
    stale = _data_root(home) / "staging" / "candidate-stale"
    stale.mkdir(parents=True)
    (stale / "build-source-stale").write_text("stale", encoding="utf-8")
    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'c'\n", encoding="utf-8"
    )
    marker = tmp_path / "preflight-started"
    installer = source / "src" / "agent_run" / "runner_installer.py"
    installer.write_text(
        installer.read_text(encoding="utf-8").replace(
            "    _check_prerequisites(source)\n",
            f"    Path({str(marker)!r}).write_text('ready', encoding='utf-8')\n    time.sleep(2)\n    _check_prerequisites(source)\n",
            1,
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
        }
    )
    process = subprocess.Popen(
        [str(source / "install.sh")],
        cwd=delivery,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 30
    while not marker.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            break
        time.sleep(0.02)
    assert marker.exists(), process.communicate(timeout=10)[1]

    process.send_signal(signal.SIGTERM)
    _stdout, stderr = process.communicate(timeout=30)

    assert process.returncode == 1, stderr
    assert int(count.read_text(encoding="utf-8")) == 2
    assert _managed_state_snapshot(
        home, config=config, locator=locator, delivery=delivery
    ) == before


def test_public_idempotent_signal_restores_profile_and_entry(
    tmp_path: Path,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "idempotent-cleanup-started"
    installer = source / "src" / "agent_run" / "runner_installer.py"
    installer.write_text(
        installer.read_text(encoding="utf-8").replace(
            "            try:\n                _ensure_profile(paths)\n                _ensure_stable_entry(paths)\n                warnings = [\n",
            f"            try:\n                _ensure_profile(paths)\n                _ensure_stable_entry(paths)\n                if os.environ.get('AGENT_RUN_TEST_IDEMPOTENT_SIGNAL') == '1':\n                    Path({str(marker)!r}).write_text('ready', encoding='utf-8')\n                    time.sleep(2)\n                warnings = [\n",
            1,
        ),
        encoding="utf-8",
    )
    first = _run(source, home, fake_bin)
    assert first.returncode == 0, first.stderr
    data_root = _data_root(home)
    active = data_root / "active"
    generation = active.resolve()
    before = {
        "active": os.readlink(active),
        "current": os.readlink(generation / "current"),
        "previous": os.readlink(generation / "previous")
        if (generation / "previous").is_symlink()
        else None,
        "snapshots": sorted(path.name for path in (data_root / "snapshots").iterdir()),
        "generations": sorted(path.name for path in (data_root / "generations").iterdir()),
    }
    profile = home / ".profile"
    stable_entry = home / ".local" / "bin" / "agent-run"
    profile.unlink()
    stable_entry.unlink()
    environment = os.environ.copy()
    environment.update(
        {
            "AGENT_RUN_TEST_IDEMPOTENT_SIGNAL": "1",
            "HOME": str(home),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
        }
    )
    process = subprocess.Popen(
        [str(source / "install.sh")],
        cwd=source,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 30
    while not marker.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            break
        time.sleep(0.02)
    assert marker.exists(), process.communicate(timeout=10)[1]

    process.send_signal(signal.SIGTERM)
    _stdout, stderr = process.communicate(timeout=30)

    assert process.returncode == 1, stderr
    assert int(count.read_text(encoding="utf-8")) == 1
    assert not profile.exists()
    assert not stable_entry.exists() and not stable_entry.is_symlink()
    assert os.readlink(active) == before["active"]
    restored_generation = active.resolve()
    assert os.readlink(restored_generation / "current") == before["current"]
    assert (
        os.readlink(restored_generation / "previous")
        if (restored_generation / "previous").is_symlink()
        else None
    ) == before["previous"]
    assert sorted(path.name for path in (data_root / "snapshots").iterdir()) == before[
        "snapshots"
    ]
    assert sorted(path.name for path in (data_root / "generations").iterdir()) == before[
        "generations"
    ]
    assert not list((data_root / "staging").iterdir())


def test_public_reactivating_previous_snapshot_reprobes_before_activation(
    tmp_path: Path,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    for version in ("a", "b"):
        (source / "src" / "agent_run" / "__init__.py").write_text(
            f"__version__ = '{version}'\n", encoding="utf-8"
        )
        result = _run(source, home, fake_bin)
        assert result.returncode == 0, result.stderr
    config, locator, delivery = _write_public_state_fixtures(tmp_path, home)
    before = _managed_state_snapshot(
        home, config=config, locator=locator, delivery=delivery
    )

    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'a'\n", encoding="utf-8"
    )
    (tmp_path / "codex-behavior").write_text("wrong-result", encoding="utf-8")
    failed = _run(source, home, fake_bin, cwd=delivery)

    assert failed.returncode == 1, failed.stderr
    assert "Traceback" not in failed.stderr
    assert int(count.read_text(encoding="utf-8")) == 3
    assert _managed_state_snapshot(
        home, config=config, locator=locator, delivery=delivery
    ) == before

    (tmp_path / "codex-behavior").write_text("success", encoding="utf-8")
    reactivated = _run(source, home, fake_bin, cwd=delivery)

    assert reactivated.returncode == 0, reactivated.stderr
    assert int(count.read_text(encoding="utf-8")) == 4
    assert _manifest(_active_snapshot(home))["source_provenance"]["kind"] == "source-directory"
    after_reactivation = _managed_state_snapshot(
        home, config=config, locator=locator, delivery=delivery
    )
    assert after_reactivation["active"] != before["active"]
    assert after_reactivation["generation_links"]["previous"] == before["generation_links"]["current"]

    repeated = _run(source, home, fake_bin, cwd=delivery)

    assert repeated.returncode == 0, repeated.stderr
    assert int(count.read_text(encoding="utf-8")) == 4
    assert _managed_state_snapshot(
        home, config=config, locator=locator, delivery=delivery
    ) == after_reactivation


def test_install_keeps_only_current_and_previous_after_a_b_c(
    tmp_path: Path,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    identities: list[str] = []
    for version in ("a", "b", "c"):
        (source / "src" / "agent_run" / "__init__.py").write_text(
            f"__version__ = '{version}'\n", encoding="utf-8"
        )
        result = _run(source, home, fake_bin)
        assert result.returncode == 0, result.stderr
        identity = _manifest(_active_snapshot(home))["content_identity"]
        assert isinstance(identity, str)
        identities.append(identity)

    active = _active_snapshot(home)
    assert _manifest(active)["content_identity"] == identities[-1]
    generation = (_data_root(home) / "active").resolve()
    assert (generation / "current").resolve() == active
    assert (generation / "previous").resolve() == (
        _data_root(home) / "snapshots" / identities[-2]
    ).resolve()
    assert sorted(path.name for path in (_data_root(home) / "snapshots").iterdir()) == sorted(
        identities[-2:]
    )
    assert int(count.read_text()) == 3

    repeat = _run(source, home, fake_bin)
    assert repeat.returncode == 0, repeat.stderr
    assert int(count.read_text()) == 3
    assert sorted(path.name for path in (_data_root(home) / "snapshots").iterdir()) == sorted(
        identities[-2:]
    )


def test_rollback_without_previous_is_a_noop_and_repeat_uninstall_is_safe(
    tmp_path: Path,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    rollback = _run(source, home, fake_bin, "--rollback")
    assert rollback.returncode == 1
    assert not (_data_root(home) / "active").exists()
    assert not count.exists()

    assert _run(source, home, fake_bin, "--uninstall").returncode == 0
    repeat = _run(source, home, fake_bin, "--uninstall")
    assert repeat.returncode == 0, repeat.stderr
    assert (_data_root(home) / "install.lock").exists()


def test_profile_path_is_unique_and_available_to_a_login_shell(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    profile_path = home / ".profile"
    profile_path.write_text("before\n", encoding="utf-8")
    profile_path.chmod(0o644)

    result = _run(source, home, fake_bin)
    assert result.returncode == 0, result.stderr
    profile = profile_path.read_text(encoding="utf-8")
    assert profile_path.stat().st_mode & 0o777 == 0o644
    assert profile.count("# >>> agent-run managed PATH >>>") == 1
    assert profile.count("# <<< agent-run managed PATH <<<") == 1

    repeat = _run(source, home, fake_bin)
    assert repeat.returncode == 0, repeat.stderr
    profile = profile_path.read_text(encoding="utf-8")
    assert profile.count("# >>> agent-run managed PATH >>>") == 1
    assert profile.count("# <<< agent-run managed PATH <<<") == 1

    shell_environment = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "XDG_DATA_HOME": str(home / "data"),
    }
    shell = subprocess.run(
        ["bash", "-lc", "command -v agent-run"],
        env=shell_environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert shell.returncode == 0, shell.stderr
    assert Path(shell.stdout.strip()).resolve() == (
        home / ".local" / "bin" / "agent-run"
    ).resolve()

    uninstall = _run(source, home, fake_bin, "--uninstall")
    assert uninstall.returncode == 0, uninstall.stderr
    assert profile_path.read_text(encoding="utf-8") == "before\n"
    assert profile_path.stat().st_mode & 0o777 == 0o644


def test_install_result_names_public_entry_and_login_shell_refresh(
    tmp_path: Path,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    result = _run(source, home, fake_bin)

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["entry"] == str(home / ".local" / "bin" / "agent-run")
    assert "重新打开登录 shell" in output["path_notice"]


@pytest.mark.parametrize("interrupt_stage", ["source", "paths", "install"])
def test_keyboard_interrupt_is_reported_as_a_bounded_install_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interrupt_stage: str,
) -> None:
    source = _source_tree(tmp_path)
    home = tmp_path / "home"
    environment = {
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "PATH": str(tmp_path / "bin"),
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    if interrupt_stage == "source":
        original_resolve = Path.resolve

        def interrupt_source_resolve(
            path: Path, *args: object, **kwargs: object
        ) -> Path:
            if path == source:
                raise KeyboardInterrupt()
            return original_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", interrupt_source_resolve)
    elif interrupt_stage == "paths":

        def interrupt_paths() -> runner_installer.InstallPaths:
            raise KeyboardInterrupt()

        monkeypatch.setattr(
            runner_installer.InstallPaths, "from_environment", interrupt_paths
        )
    else:

        def interrupt_install(
            _paths: runner_installer.InstallPaths, _source: Path
        ) -> dict[str, object]:
            raise KeyboardInterrupt()

        monkeypatch.setattr(runner_installer, "_install", interrupt_install)

    result = runner_installer.main(["--source", str(source)])

    captured = capsys.readouterr()
    assert result == 1
    assert "agent-run install: interrupted" in captured.err
    assert "Traceback" not in captured.err
    managed_root = home / "data" / "agent-run"
    assert not (managed_root / "active").exists()
    assert not (home / ".local" / "bin" / "agent-run").exists()
    assert not (home / ".profile").exists()


def test_post_activation_failure_restores_old_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_in_process_build: None,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    first = _run(source, home, fake_bin)
    assert first.returncode == 0, first.stderr
    first_identity = _manifest(_active_snapshot(home))["content_identity"]
    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'after-failure'\n", encoding="utf-8"
    )

    def fail_profile(_paths: object) -> None:
        raise OSError("injected profile failure")

    monkeypatch.setattr(runner_installer, "_ensure_profile", fail_profile)
    with pytest.raises(OSError, match="injected profile failure"):
        runner_installer._install(runner_installer.InstallPaths.from_environment(), source)

    assert _manifest(_active_snapshot(home))["content_identity"] == first_identity
    assert sorted(path.name for path in (_data_root(home) / "snapshots").iterdir()) == [
        first_identity
    ]
    assert (home / ".local" / "bin" / "agent-run").is_symlink()


@pytest.mark.parametrize("failure_call", [1, 2])
def test_activation_sync_failure_restores_the_complete_old_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_in_process_build: None,
    failure_call: int,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'first'\n", encoding="utf-8"
    )
    assert _run(source, home, fake_bin).returncode == 0
    first_identity = _manifest(_active_snapshot(home))["content_identity"]
    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'second'\n", encoding="utf-8"
    )
    assert _run(source, home, fake_bin).returncode == 0
    second_identity = _manifest(_active_snapshot(home))["content_identity"]
    assert isinstance(first_identity, str)
    assert isinstance(second_identity, str)
    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'failed-third'\n", encoding="utf-8"
    )

    calls = 0

    def fail_sync(_directory: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise OSError("injected directory sync failure")

    monkeypatch.setattr(runner_installer, "_sync_directory", fail_sync)
    with pytest.raises(OSError, match="injected directory sync failure"):
        runner_installer._install(runner_installer.InstallPaths.from_environment(), source)

    paths = runner_installer.InstallPaths.from_environment()
    current, previous, generation = runner_installer._read_active(paths)
    assert current is not None and previous is not None and generation is not None
    assert _manifest(current)["content_identity"] == second_identity
    assert _manifest(previous)["content_identity"] == first_identity
    assert (generation / "current").resolve() == current.resolve()
    assert (generation / "previous").resolve() == previous.resolve()
    assert paths.stable_entry.is_symlink()
    assert paths.stable_entry.resolve().is_file()
    assert sorted(path.name for path in paths.snapshots.iterdir()) == sorted(
        [first_identity, second_identity]
    )
    assert not list(paths.staging.iterdir())


def test_initial_activation_sync_failure_leaves_no_active_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_in_process_build: None,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    def fail_sync(_directory: Path) -> None:
        raise OSError("injected initial sync failure")

    monkeypatch.setattr(runner_installer, "_sync_directory", fail_sync)
    with pytest.raises(OSError, match="injected initial sync failure"):
        runner_installer._install(runner_installer.InstallPaths.from_environment(), source)

    data_root = _data_root(home)
    assert not (data_root / "active").exists()
    assert not list((data_root / "snapshots").glob("*"))
    assert not list((data_root / "staging").glob("*"))
    assert not (home / ".local" / "bin" / "agent-run").exists()


def test_initial_activation_recovery_failure_keeps_a_complete_new_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_in_process_build: None,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    paths = runner_installer.InstallPaths.from_environment()
    original_replace = os.replace
    original_unlink = os.unlink
    active_replace_calls = 0

    def replace_then_fail(source_path: Path, destination: Path) -> None:
        nonlocal active_replace_calls
        if destination == paths.active:
            active_replace_calls += 1
            original_replace(source_path, destination)
            raise OSError("injected initial active rename failure")
        original_replace(source_path, destination)

    def unlink_active_only(path: str | os.PathLike[str], *args: Any, **kwargs: Any) -> None:
        if os.fspath(path) == os.fspath(paths.active):
            raise OSError("injected active unlink failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace_then_fail)
    monkeypatch.setattr(os, "unlink", unlink_active_only)

    result = runner_installer._install(paths, source)

    assert result["warning"] == ["Active Runner 恢复失败，已保留完整新 generation"]
    assert active_replace_calls == 1
    current, previous, generation = runner_installer._read_active(paths)
    assert current is not None and previous is None and generation is not None
    assert (generation / "current").resolve() == current.resolve()
    assert paths.stable_entry.resolve().is_file()
    assert len(list(paths.snapshots.iterdir())) == 1
    assert not list(paths.staging.iterdir())


def test_activation_rename_failure_after_swap_restores_the_old_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_in_process_build: None,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert _run(source, home, fake_bin).returncode == 0
    old_identity = _manifest(_active_snapshot(home))["content_identity"]
    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'rename-failure'\n", encoding="utf-8"
    )
    paths = runner_installer.InstallPaths.from_environment()
    original_replace = os.replace
    active_replace_calls = 0

    def replace_then_fail(source_path: Path, destination: Path) -> None:
        nonlocal active_replace_calls
        if destination == paths.active:
            active_replace_calls += 1
            original_replace(source_path, destination)
            if active_replace_calls == 1:
                raise OSError("injected active rename failure")
            return
        original_replace(source_path, destination)

    monkeypatch.setattr(os, "replace", replace_then_fail)
    with pytest.raises(OSError, match="injected active rename failure"):
        runner_installer._install(paths, source)

    current, previous, generation = runner_installer._read_active(paths)
    assert current is not None and previous is None and generation is not None
    assert _manifest(current)["content_identity"] == old_identity
    assert (generation / "current").resolve() == current.resolve()
    assert paths.stable_entry.resolve().is_file()
    assert sorted(path.name for path in paths.snapshots.iterdir()) == [old_identity]
    assert not list(paths.staging.iterdir())
    assert active_replace_calls == 2


def test_recovery_failure_keeps_a_complete_new_generation_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_in_process_build: None,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert _run(source, home, fake_bin).returncode == 0
    old_identity = _manifest(_active_snapshot(home))["content_identity"]
    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'recovery-warning'\n", encoding="utf-8"
    )

    paths = runner_installer.InstallPaths.from_environment()
    original_replace = os.replace
    active_replace_calls = 0

    def replace_then_fail(source_path: Path, destination: Path) -> None:
        nonlocal active_replace_calls
        if destination == paths.active:
            active_replace_calls += 1
            if active_replace_calls == 2:
                raise OSError("injected recovery rename failure")
            original_replace(source_path, destination)
            if active_replace_calls == 1:
                raise OSError("injected active rename failure")
            return
        original_replace(source_path, destination)

    monkeypatch.setattr(os, "replace", replace_then_fail)

    result = runner_installer._install(paths, source)

    assert result["warning"] == ["Active Runner 恢复失败，已保留完整新 generation"]
    assert active_replace_calls == 2
    current, previous, generation = runner_installer._read_active(paths)
    assert current is not None and previous is not None and generation is not None
    assert _manifest(current)["content_identity"] != old_identity
    assert _manifest(previous)["content_identity"] == old_identity
    assert (generation / "current").resolve() == current.resolve()
    assert (generation / "previous").resolve() == previous.resolve()
    assert paths.stable_entry.resolve().is_file()
    assert len(list(paths.snapshots.iterdir())) == 2
    assert not list(paths.staging.iterdir())


@pytest.mark.parametrize("directory_name", ["generations", "data_root"])
def test_activation_directory_open_failure_restores_the_old_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_in_process_build: None,
    directory_name: str,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert _run(source, home, fake_bin).returncode == 0
    old_identity = _manifest(_active_snapshot(home))["content_identity"]
    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'open-failure'\n", encoding="utf-8"
    )
    paths = runner_installer.InstallPaths.from_environment()
    target = paths.generations if directory_name == "generations" else paths.data_root
    original_open = os.open

    def fail_open(
        path: str | os.PathLike[str],
        flags: int,
        *mode: int,
        **options: Any,
    ) -> int:
        if Path(path) == target:
            raise OSError("injected directory open failure")
        return original_open(path, flags, *mode, **options)

    monkeypatch.setattr(os, "open", fail_open)
    with pytest.raises(OSError, match="injected directory open failure"):
        runner_installer._install(paths, source)

    current, previous, generation = runner_installer._read_active(paths)
    assert current is not None and previous is None and generation is not None
    assert _manifest(current)["content_identity"] == old_identity
    assert (generation / "current").resolve() == current.resolve()
    assert paths.stable_entry.resolve().is_file()
    assert sorted(path.name for path in paths.snapshots.iterdir()) == [old_identity]
    assert not list(paths.staging.iterdir())


def test_retired_cleanup_is_retried_before_a_later_build_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fast_in_process_build: None,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert _run(source, home, fake_bin).returncode == 0
    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'second'\n", encoding="utf-8"
    )
    assert _run(source, home, fake_bin).returncode == 0
    paths = runner_installer.InstallPaths.from_environment()
    current_before, previous_before, _generation_before = runner_installer._read_active(paths)
    assert current_before is not None and previous_before is not None
    (paths.snapshots / "stale-snapshot").mkdir()
    (paths.generations / "stale-generation").mkdir()
    events: list[str] = []

    def injected_cleanup(*_arguments: object) -> list[str]:
        events.append("cleanup")
        return ["旧 Snapshot 清理失败"]

    def injected_build(_candidate: Path, _source: Path) -> tuple[str, str]:
        events.append("build")
        raise runner_installer.InstallerError("injected build failure")

    monkeypatch.setattr(runner_installer, "_cleanup_retired", injected_cleanup)
    monkeypatch.setattr(runner_installer, "_build_candidate", injected_build)
    with pytest.raises(runner_installer.InstallerError, match="injected build failure"):
        runner_installer._install(paths, source)

    assert events == ["cleanup", "build"]
    assert "旧 Snapshot 清理失败" in capsys.readouterr().err
    current_after, previous_after, _generation_after = runner_installer._read_active(paths)
    assert current_after == current_before
    assert previous_after == previous_before
    assert (paths.snapshots / "stale-snapshot").is_dir()
    assert (paths.generations / "stale-generation").is_dir()


def test_retired_cleanup_failure_is_warning_only_and_next_install_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_in_process_build: None,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert _run(source, home, fake_bin).returncode == 0
    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'second'\n", encoding="utf-8"
    )
    assert _run(source, home, fake_bin).returncode == 0
    paths = runner_installer.InstallPaths.from_environment()
    stale_snapshot = paths.snapshots / "stale-snapshot"
    stale_generation = paths.generations / "stale-generation"
    stale_snapshot.mkdir()
    stale_generation.mkdir()
    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'third'\n", encoding="utf-8"
    )
    original_remove = runner_installer._remove_path
    blocked = True

    def fail_retired_cleanup(path: Path) -> None:
        if blocked and path in {stale_snapshot, stale_generation}:
            raise OSError("injected retired cleanup failure")
        original_remove(path)

    monkeypatch.setattr(runner_installer, "_remove_path", fail_retired_cleanup)
    first_update = runner_installer._install(paths, source)
    assert first_update["warning"]
    assert stale_snapshot.is_dir()
    assert stale_generation.is_dir()
    active_after_failure = _active_snapshot(home)
    assert _manifest(active_after_failure)["source_provenance"]

    blocked = False
    second_update = runner_installer._install(paths, source)
    assert second_update["warning"] is None
    assert not stale_snapshot.exists()
    assert not stale_generation.exists()
    assert _active_snapshot(home) == active_after_failure


def test_rollback_does_not_probe_and_uninstall_preserves_user_data(
    tmp_path: Path,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, count, status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    assert _run(source, home, fake_bin).returncode == 0
    first_identity = _manifest(_active_snapshot(home))["content_identity"]
    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'second'\n", encoding="utf-8"
    )
    assert _run(source, home, fake_bin).returncode == 0
    second_identity = _manifest(_active_snapshot(home))["content_identity"]
    assert isinstance(first_identity, str)
    assert isinstance(second_identity, str)
    assert second_identity != first_identity
    assert int(count.read_text()) == 2

    rollback = _run(source, home, fake_bin, "--rollback")
    assert rollback.returncode == 0, rollback.stderr
    assert _manifest(_active_snapshot(home))["content_identity"] == first_identity
    assert int(count.read_text()) == 2

    config = home / "config" / "agent-run"
    config.mkdir(parents=True)
    (config / "github-app.json").write_text("keep", encoding="utf-8")
    locator = home / "state" / "agent-run"
    locator.mkdir(parents=True)
    (locator / "run-locator.json").write_text("keep", encoding="utf-8")
    target = tmp_path / "target"
    (target / ".agent-run").mkdir(parents=True)
    (target / ".agent-run" / "run.json").write_text("keep", encoding="utf-8")
    profile = home / ".profile"
    profile.write_text("before\n", encoding="utf-8")

    failed_config = (config / "github-app.json").read_bytes()
    failed_locator = (locator / "run-locator.json").read_bytes()
    failed_target = (target / ".agent-run" / "run.json").read_bytes()

    status_file.write_text("not-ok", encoding="utf-8")
    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'third'\n", encoding="utf-8"
    )
    failed_update = _run(source, home, fake_bin)
    assert failed_update.returncode != 0
    assert _manifest(_active_snapshot(home))["content_identity"] == first_identity
    assert sorted(path.name for path in (_data_root(home) / "snapshots").iterdir()) == sorted(
        [first_identity, second_identity]
    )
    assert (config / "github-app.json").read_bytes() == failed_config
    assert (locator / "run-locator.json").read_bytes() == failed_locator
    assert (target / ".agent-run" / "run.json").read_bytes() == failed_target
    assert profile.read_text(encoding="utf-8") == "before\n"

    uninstall = _run(source, home, fake_bin, "--uninstall")
    assert uninstall.returncode == 0, uninstall.stderr
    assert (config / "github-app.json").read_text(encoding="utf-8") == "keep"
    assert (locator / "run-locator.json").read_text(encoding="utf-8") == "keep"
    assert (target / ".agent-run" / "run.json").read_text(encoding="utf-8") == "keep"
    assert (profile.read_text(encoding="utf-8") == "before\n")
    assert (_data_root(home) / "install.lock").exists()
    assert not (home / ".local" / "bin" / "agent-run").exists()


def test_non_managed_entry_is_not_overwritten(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    fake_bin, count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    entry = home / ".local" / "bin" / "agent-run"
    entry.parent.mkdir(parents=True)
    entry.write_text("user-owned", encoding="utf-8")

    result = _run(source, home, fake_bin)

    assert result.returncode != 0
    assert entry.read_text(encoding="utf-8") == "user-owned"
    assert not count.exists()


def test_uninstall_reports_but_preserves_a_replaced_entry(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    assert _run(source, home, fake_bin).returncode == 0
    entry = home / ".local" / "bin" / "agent-run"
    entry.unlink()
    entry.write_text("user-owned", encoding="utf-8")

    result = _run(source, home, fake_bin, "--uninstall")

    assert result.returncode == 1
    assert entry.read_text(encoding="utf-8") == "user-owned"


@pytest.mark.parametrize("arguments", [(), ("--rollback",), ("--uninstall",)])
def test_management_lock_is_non_blocking(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    lock = _data_root(home) / "install.lock"
    lock.parent.mkdir(parents=True)
    with lock.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run(source, home, fake_bin, *arguments)

    assert result.returncode == 1
    assert "另一个" in result.stderr


@pytest.mark.parametrize("second_arguments", [(), ("--rollback",), ("--uninstall",)])
def test_concurrent_management_operations_have_one_winner_and_keep_invariants(
    tmp_path: Path,
    second_arguments: tuple[str, ...],
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    for version in ("first", "second"):
        (source / "src" / "agent_run" / "__init__.py").write_text(
            f"__version__ = '{version}'\n", encoding="utf-8"
        )
        initial = _run(source, home, fake_bin)
        assert initial.returncode == 0, initial.stderr
    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'third'\n", encoding="utf-8"
    )
    (tmp_path / "codex-behavior").write_text("sleep", encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
        }
    )
    lock = _data_root(home) / "install.lock"
    first = subprocess.Popen(
        [str(source / "install.sh")],
        cwd=source,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5
    lock_held = False
    while time.monotonic() < deadline:
        with lock.open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_held = True
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        if lock_held:
            break
        time.sleep(0.01)
    if not lock_held:
        first.kill()
        first.communicate(timeout=10)
        raise AssertionError("first install did not acquire the management lock")
    second = _run(source, home, fake_bin, *second_arguments)
    first_stdout, first_stderr = first.communicate(timeout=240)

    assert first.returncode == 0, first_stderr
    assert first_stdout
    assert second.returncode == 1
    assert "另一个" in second.stderr
    data_root = _data_root(home)
    generation = (data_root / "active").resolve()
    current = (generation / "current").resolve()
    previous = (generation / "previous").resolve()
    assert current.is_dir() and previous.is_dir()
    assert (home / ".local" / "bin" / "agent-run").resolve().is_file()
    assert len(list((data_root / "snapshots").iterdir())) == 2
    assert not list((data_root / "staging").iterdir())


def test_installed_runner_continues_a_delivery_run_and_does_not_write_incompatible_state(
    tmp_path: Path,
) -> None:
    source = _source_tree(tmp_path)
    fake_bin, _count, _status_file = _fake_codex(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=delivery, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Agent Run Tests"], cwd=delivery, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "agent-run-tests@example.invalid"],
        cwd=delivery,
        check=True,
    )
    (delivery / "README.md").write_text("# delivery\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=delivery, check=True)
    subprocess.run(["git", "commit", "-qm", "initial delivery"], cwd=delivery, check=True)
    fixture = write_fixture(
        delivery / "github.json",
        issues={
            "2": {
                "number": 2,
                "title": "Ticket 2",
                "body": "Implement ticket 2.",
                "state": "OPEN",
                "labels": ["ready-for-agent"],
                "blocked_by": [],
            }
        },
    )
    assert _run(source, home, fake_bin).returncode == 0
    cli_environment = os.environ.copy()
    cli_environment.update(
        {
            "HOME": str(home),
            "XDG_STATE_HOME": str(home / "state"),
            "PATH": f"{home / '.local' / 'bin'}{os.pathsep}{cli_environment['PATH']}",
        }
    )
    entry = home / ".local" / "bin" / "agent-run"
    started = subprocess.run(
        [str(entry), "start", "1", "--github-fixture", str(fixture)],
        cwd=delivery,
        env=cli_environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert started.returncode == 0, started.stderr
    started_output = json.loads(started.stdout)
    run_id = started_output["run_id"]
    state_path = next((delivery / ".agent-run" / "runs").glob("*.json"))

    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'second'\n", encoding="utf-8"
    )
    updated = _run(source, home, fake_bin)
    assert updated.returncode == 0, updated.stderr
    continued = subprocess.run(
        [str(entry), "start", "1", "--github-fixture", str(fixture)],
        cwd=delivery,
        env=cli_environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert continued.returncode == 0, continued.stderr
    continued_output = json.loads(continued.stdout)
    assert continued_output["result"] == "resumed"
    assert continued_output["run_id"] == run_id

    rollback = _run(source, home, fake_bin, "--rollback")
    assert rollback.returncode == 0, rollback.stderr
    continued_after_rollback = subprocess.run(
        [str(entry), "start", "1", "--github-fixture", str(fixture)],
        cwd=delivery,
        env=cli_environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert continued_after_rollback.returncode == 0, continued_after_rollback.stderr
    rollback_output = json.loads(continued_after_rollback.stdout)
    assert rollback_output["result"] == "resumed"
    assert rollback_output["run_id"] == run_id

    incompatible = json.loads(state_path.read_text(encoding="utf-8"))
    incompatible["schema_version"] = 1
    state_path.write_text(json.dumps(incompatible), encoding="utf-8")
    before = state_path.read_bytes()
    locator_path = home / "state" / "agent-run" / "run-locator.json"
    locator_before = locator_path.read_bytes()
    target_files_before = {
        path.relative_to(delivery / ".agent-run"): path.read_bytes()
        for path in (delivery / ".agent-run").rglob("*")
        if path.is_file()
    }
    target_paths_before = sorted(
        path.relative_to(delivery / ".agent-run")
        for path in (delivery / ".agent-run").rglob("*")
    )
    rejected = subprocess.run(
        [str(entry), "run", "1", "--github-fixture", str(fixture)],
        cwd=delivery,
        env=cli_environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 2
    assert json.loads(rejected.stdout)["status"] == "incompatible_run_state"
    assert state_path.read_bytes() == before
    assert locator_path.read_bytes() == locator_before
    assert {
        path.relative_to(delivery / ".agent-run"): path.read_bytes()
        for path in (delivery / ".agent-run").rglob("*")
        if path.is_file()
    } == target_files_before
    assert sorted(
        path.relative_to(delivery / ".agent-run")
        for path in (delivery / ".agent-run").rglob("*")
    ) == target_paths_before

    (source / "src" / "agent_run" / "__init__.py").write_text(
        "__version__ = 'third'\n", encoding="utf-8"
    )
    assert _run(source, home, fake_bin).returncode == 0
    assert state_path.read_bytes() == before
    assert locator_path.read_bytes() == locator_before
    assert {
        path.relative_to(delivery / ".agent-run"): path.read_bytes()
        for path in (delivery / ".agent-run").rglob("*")
        if path.is_file()
    } == target_files_before
    assert sorted(
        path.relative_to(delivery / ".agent-run")
        for path in (delivery / ".agent-run").rglob("*")
    ) == target_paths_before
