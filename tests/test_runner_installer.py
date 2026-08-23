from __future__ import annotations

import json
import fcntl
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_run.runner_probe import RunnerProbeBackend, RunnerProbeError
from agent_run import runner_installer


PROJECT_ROOT = Path(__file__).parents[1]


def _source_tree(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(PROJECT_ROOT / "install.sh", source / "install.sh")
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", source / "pyproject.toml")
    shutil.copytree(PROJECT_ROOT / "src", source / "src")
    (source / "install.sh").chmod(0o755)
    return source


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
    script = directory / "codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import pathlib\n"
        "import sys\n"
        "import time\n"
        f"count = pathlib.Path({str(count)!r})\n"
        f"status_file = pathlib.Path({str(status_file)!r})\n"
        f"behavior_file = pathlib.Path({str(behavior_file)!r})\n"
        "current = int(count.read_text() if count.exists() else '0')\n"
        "count.write_text(str(current + 1))\n"
        "behavior = behavior_file.read_text()\n"
        "if behavior == 'nonzero':\n"
        "    sys.exit(7)\n"
        "if behavior == 'missing':\n"
        "    sys.exit(0)\n"
        "if behavior == 'timeout':\n"
        "    time.sleep(130)\n"
        "schema = pathlib.Path(sys.argv[sys.argv.index('--output-schema') + 1])\n"
        "assert json.loads(schema.read_text())['properties']['status']['enum'] == ['ok']\n"
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
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
        }
    )
    return subprocess.run(
        [str(source / "install.sh"), *arguments],
        cwd=source,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _data_root(home: Path) -> Path:
    return home / "data" / "agent-run"


def _active_snapshot(home: Path) -> Path:
    return (_data_root(home) / "active" / "current").resolve()


def _manifest(snapshot: Path) -> dict[str, object]:
    loaded = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


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
    first_identity = _manifest(first_snapshot)["content_identity"]
    assert int(count.read_text()) == 1
    assert (home / ".local" / "bin" / "agent-run").is_symlink()
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


@pytest.mark.parametrize("behavior", ["nonzero", "missing"])
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


def test_post_activation_failure_restores_old_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
