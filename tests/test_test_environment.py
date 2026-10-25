"""Exercise pytest with a simulated user home, never the real user's state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from support.environment import _assert_python_and_git_environment


_GIT_LOCATION_VARIABLES = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_TEMPLATE_DIR",
    "GIT_SHALLOW_FILE", "GIT_NAMESPACE",
)


@pytest.mark.parametrize("layout", [
    "regular", "entry-link", "root-link", "ancestor-link", "wrong-entry",
    "extra-search-root", "child-link", "changed-bytes",
])
def test_installed_runtime_checks_the_original_package_paths(tmp_path: Path, monkeypatch, layout: str) -> None:
    from types import SimpleNamespace
    from support import installed_runtime

    source = Path(__file__).resolve().parents[1] / "src" / "agent_run"
    prefix = tmp_path / "venv"
    package = prefix / "lib" / "site-packages" / "agent_run"
    shutil.copytree(source, package, ignore=shutil.ignore_patterns("__pycache__"))
    origin = package / "__init__.py"
    locations = [str(package)]
    mirror = prefix / "mirror"
    if layout in {"entry-link", "wrong-entry", "extra-search-root"}:
        shutil.copytree(package, mirror)
        if layout == "entry-link":
            origin.unlink()
            origin.symlink_to(mirror / "__init__.py")
        elif layout == "wrong-entry":
            origin = mirror / "__init__.py"
        else:
            locations.append(str(mirror))
        if layout != "extra-search-root":
            (package / "core" / "kernel.py").write_text("# Different submodule; never executed.\n")
    elif layout == "root-link":
        package.rename(mirror)
        package.symlink_to(mirror, target_is_directory=True)
    elif layout == "ancestor-link":
        parent = package.parent
        parent.rename(mirror)
        parent.symlink_to(mirror, target_is_directory=True)
    elif layout == "child-link":
        (package / "source_link.py").symlink_to(source / "__init__.py")
    elif layout == "changed-bytes":
        (package / "core" / "kernel.py").write_text("# Stale installed submodule; never executed.\n")
    monkeypatch.setattr(installed_runtime, "sys", SimpleNamespace(prefix=str(prefix)))
    monkeypatch.setattr(installed_runtime, "find_spec", lambda _name: SimpleNamespace(
        origin=str(origin), submodule_search_locations=locations))
    if layout == "regular":
        installed_runtime.require_current_installation()
    else:
        with pytest.raises(pytest.UsageError):
            installed_runtime.require_current_installation()


@pytest.mark.parametrize("dont_write_bytecode", [False, True])
def test_importing_installer_build_seam_preserves_interpreter_policy(
    dont_write_bytecode: bool,
) -> None:
    project = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(project / "src"), str(project / "tests")]
    )
    result = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.dont_write_bytecode = {dont_write_bytecode!r}; "
         "before = (sys.dont_write_bytecode, sys.path.copy()); "
         "from support.fast_runner_installer import build_candidate; "
         "assert (sys.dont_write_bytecode, sys.path) == before"],
        env=environment, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr



def test_pytest_isolates_each_case_and_cli_from_host_state(tmp_path: Path) -> None:
    _assert_python_and_git_environment()
    project = Path(__file__).resolve().parents[1]
    suite = tmp_path / "suite"
    suite.mkdir()
    shutil.copyfile(project / "tests/conftest.py", suite / "conftest.py")
    (suite / "test_isolation.py").write_text(
        """import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from agent_run.storage.tasks import read_task_bindings
from support.environment import _assert_python_and_git_environment

@pytest.mark.parametrize("case", ["first", "second"])
def test_case(git_repo, tmp_path, monkeypatch, case):
    _assert_python_and_git_environment()
    for key in ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        assert Path(os.environ[key]).is_relative_to(tmp_path), key
    assert not list(read_task_bindings())
    assert (git_repo / "README.md").read_text() == "# fixture\\n"
    assert not (git_repo / "previous-case").exists()
    assert subprocess.check_output(["git", "config", "user.name"], cwd=git_repo, text=True).strip() == "Agent Run Tests"
    inherited = subprocess.run(["git", "config", "--get", "isolation.inherited"], cwd=git_repo)
    assert inherited.returncode == 1
    private_config = Path(os.environ["HOME"]) / ".gitconfig"
    assert subprocess.check_output(["git", "config", "--global", "user.name"], text=True).strip() == "Agent Run Tests"
    private_config.write_text(private_config.read_text() + f"[isolation]\\n    private = {case}\\n")
    assert subprocess.check_output(["git", "config", "isolation.private"], cwd=git_repo, text=True).strip() == case
    with monkeypatch.context() as explicit:
        explicit.setenv("GIT_CONFIG_COUNT", "1")
        explicit.setenv("GIT_CONFIG_KEY_0", "isolation.injected")
        explicit.setenv("GIT_CONFIG_VALUE_0", case)
        assert subprocess.check_output(["git", "config", "isolation.injected"], cwd=git_repo, text=True).strip() == case
    assert "GIT_CONFIG_COUNT" not in os.environ
    # Seed through the public native creation API in a real child process.
    creation = \"\"\"
import json, sys
from pathlib import Path
from agent_run.adapters.workspace import ManagedWorkspace
from agent_run.model import Delivery, DeliveryKind, ParentIssue, Requirements, RunSpec
from agent_run.core.kernel import Kernel
from agent_run.adapters.git_objects import LocalGit
repository = Path(sys.argv[1])
run_id = "isolation-" + sys.argv[2]
base = LocalGit(repository).commit("HEAD")
delivery = Delivery("parent:1", DeliveryKind.PARENT_ONLY, 1, "requirement", base,
                    "refs/heads/isolated", "refs/heads/main")
spec = RunSpec(run_id, "example/project", 1, "parent", "graph", (delivery,),
               language="en", repository_root=str(repository))
managed = ManagedWorkspace.for_repository(spec.repository)
Kernel.create_task(managed.state_root / run_id / "kernel.sqlite3", spec,
                   requirements=Requirements(ParentIssue(1, "Parent", "Isolated requirement"), ()))
print(json.dumps({"run_id": run_id}))
\"\"\"
    started = subprocess.run([sys.executable, "-c", creation, str(git_repo), case],
                             cwd=git_repo, text=True, capture_output=True, timeout=20)
    assert started.returncode == 0, started.stderr
    run_id = json.loads(started.stdout)["run_id"]
    assert [identity for identity, _path in read_task_bindings()] == [run_id]
    # Outside the checkout the public subprocess finds the native Task binding.
    result = subprocess.run([sys.executable, "-m", "agent_run", "status", run_id, "--json"],
                            cwd=tmp_path, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["run_id"] == run_id
    (git_repo / "README.md").write_text("changed by this case")
    (git_repo / "previous-case").touch()
    subprocess.run(["git", "config", "user.name", "Changed User"], cwd=git_repo, check=True)
""",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    host = tmp_path / "host"
    for key in (
        "HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
        "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
    ):
        directory = host / key.lower()
        directory.mkdir(parents=True)
        environment[key] = str(directory)
    locator = Path(environment["XDG_DATA_HOME"]) / "agent-run/run-locator.json"
    locator.parent.mkdir()
    locator.write_text("host locator must not be read or overwritten", encoding="utf-8")
    host_bin = host / "bin"
    host_bin.mkdir()
    wrong_python = host_bin / "python3"
    wrong_python.write_text("#!/bin/sh\necho wrong-host-python\nexit 93\n", encoding="utf-8")
    wrong_python.chmod(0o755)
    environment["PATH"] = str(host_bin) + os.pathsep + environment["PATH"]
    git_overrides = {}
    for key in _GIT_LOCATION_VARIABLES:
        target = host / key.lower()
        if key in {"GIT_INDEX_FILE", "GIT_SHALLOW_FILE"}:
            target.write_text("host Git file must not change", encoding="utf-8")
        else:
            target.mkdir()
            (target / "keep").write_text("host Git directory must not change", encoding="utf-8")
        git_overrides[key] = "host-namespace" if key == "GIT_NAMESPACE" else str(target)
    host_config = host / "host-gitconfig"
    host_config.write_text("[isolation]\n    inherited = host\n", encoding="utf-8")
    shutil.copyfile(host_config, Path(environment["HOME"]) / ".gitconfig")
    git_overrides.update({
        "GIT_CONFIG": str(host_config),
        "GIT_CONFIG_GLOBAL": str(host_config),
        "GIT_CONFIG_SYSTEM": str(host_config),
        "GIT_CONFIG_PARAMETERS": "'isolation.inherited=host'",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "isolation.inherited",
        "GIT_CONFIG_VALUE_0": "host",
        "GIT_CONFIG_NOSYSTEM": "0",
        "GIT_CONFIG_FUTURE_OVERRIDE": "host",
    })
    environment.update(git_overrides)
    before = {path.relative_to(host): path.read_bytes() for path in host.rglob("*") if path.is_file()}
    environment["PYTHONPATH"] = str(project / "tests")
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    git_trace = tmp_path / "git-trace.jsonl"
    environment["GIT_TRACE2_EVENT"] = str(git_trace)
    environment["GIT_TRACE2_ENV_VARS"] = ",".join(["HOME", "PATH", *git_overrides])
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--basetemp", str(tmp_path / "nested-tmp"), str(suite)],
        cwd=suite, env=environment, text=True, capture_output=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    events = [json.loads(line) for line in git_trace.read_text(encoding="utf-8").splitlines()]
    template_commands = [
        event for event in events if event.get("event") == "start"
        and event["argv"] in (["git", "init", "-b", "main"], ["git", "commit", "-m", "initial"])
    ]
    assert len(template_commands) == 2
    for command in template_commands:
        observed = {
            event["param"]: event["value"] for event in events
            if event.get("event") == "def_param" and event["sid"] == command["sid"]
        }
        # Git prepends its own exec path; the selected Python still precedes host tools.
        observed_path = observed["PATH"].split(os.pathsep)
        assert observed_path.index(str(Path(sys.executable).parent)) < observed_path.index(str(host_bin))
        assert Path(observed["HOME"]).is_relative_to(tmp_path / "nested-tmp")
        assert observed["GIT_CONFIG_NOSYSTEM"] == "1"
        assert not (set(git_overrides) - {"GIT_CONFIG_NOSYSTEM"}).intersection(observed)
    # Check actual Git child dispatch, including the template's initial commit.
    # Detached maintenance can otherwise keep mutating the template while copied.
    automatic_maintenance = [
        event["argv"]
        for event in events
        if event.get("event") == "child_start"
        and "--auto" in event["argv"]
        and {"maintenance", "gc"}.intersection(event["argv"])
    ]
    assert not automatic_maintenance, automatic_maintenance
    assert {path.relative_to(host): path.read_bytes() for path in host.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize('prepared_loader', [False, True])
def test_isolated_cli_children_keep_prepared_sqlite_loader(tmp_path, monkeypatch, prepared_loader):
    from support.environment import _user_environment

    monkeypatch.delenv('LD_LIBRARY_PATH', raising=False)
    if prepared_loader:
        library_path = os.pathsep.join((str(tmp_path / 'verified-sqlite'), str(tmp_path / 'prior-libraries')))
        monkeypatch.setenv('LD_LIBRARY_PATH', library_path)
    monkeypatch.setenv('GH_TOKEN', 'synthetic-host-secret-must-not-be-inherited')
    monkeypatch.setenv('LD_PRELOAD', '/synthetic-unrelated-loader-must-not-be-inherited')
    environment = _user_environment(tmp_path / 'isolated')
    assert environment.get('LD_LIBRARY_PATH') == (library_path if prepared_loader else None)
    assert 'GH_TOKEN' not in environment and 'LD_PRELOAD' not in environment
    assert environment['HOME'] == str(tmp_path / 'isolated/home')
    observed = json.loads(subprocess.check_output(
        [sys.executable, '-c', 'import json, os; print(json.dumps(os.environ.get("LD_LIBRARY_PATH")))'],
        env=environment, text=True, timeout=10,
    ))
    assert observed == (library_path if prepared_loader else None)
