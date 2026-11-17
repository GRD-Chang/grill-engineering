"""Keep migrated creation/query/Host dependencies out of normal development."""
from pathlib import Path
from inspect import getmembers, isfunction

import pytest

from agent_run.core.kernel import Kernel
from support.run_architecture import violations


def test_run_boundaries_in_current_source_tree():
    root = Path(__file__).resolve().parents[1] / 'src' / 'agent_run'
    failures = [error for path in sorted(root.rglob('*.py'))
                for error in violations(path.relative_to(root).as_posix(), path.read_text())]
    assert not failures, '\n'.join(failures)


@pytest.mark.parametrize(('path', 'code'), [
    ('storage/routing.py', 'from agent_run.core.kernel import Kernel'),
    ('storage/routing.py', 'from ..queries import run'),
    ('core/kernel.py', 'from agent_run.adapters.workspace import ManagedWorkspace'),
    ('core/task_creation.py', 'from agent_run.core import kernel'),
    ('queries/current_observation.py', 'from agent_run.runtime.foreground_host import ForegroundHost'),
    ('queries/run.py', 'import agent_run.core.kernel as controller'),
    ('cli/lifecycle.py', 'controller.admit_host(request)'),
    ('cli/lifecycle.py', 'controller.storage.transaction(write=False)'),
    ('runtime/run_control.py', 'from agent_run.cli import selection'),
    ('runtime/inspection.py', 'from agent_run.core.kernel import Kernel'),
    ('runtime/inspection.py', 'observer.storage.transaction(write=False)'),
    ('cli/main.py', 'from agent_run.adapters.systemd import SystemdHost as Host'),
    ('storage/routing.py', '__import__("agent_run.core.kernel")'),
])
@pytest.mark.parametrize('nested', [False, True])
def test_restored_dependencies_are_rejected(path, code, nested):
    source = f'def accidental_restore():\n    {code}\n' if nested else code
    assert violations(path, source), (path, source)


@pytest.mark.parametrize(('path', 'source'), [
    ('runtime/inspection.py', '''def restored(kernel: Kernel, request):
    return kernel.host_usage(kernel.host_action(request))
'''),
    ('queries/current_observation.py', '''def restored(controller, request):
    return controller.host_usage(controller.host_action(request))
'''),
    ('runtime/inspection.py', '''def restored(kernel, request, lock):
    from agent_run.runtime.foreground_host import ForegroundHost
    return ForegroundHost(environment={}).observe(kernel, request, lock)
'''),
    ('runtime/inspection.py', '''def restored(kernel, request, lock):
    from agent_run.runtime.foreground_host import ForegroundHost as Carrier
    return Carrier(environment={}).observe(kernel, request, lock)
'''),
    ('runtime/inspection.py', '''from agent_run.runtime import foreground_host as physical
def restored(kernel, request, lock):
    return physical.ForegroundHost(environment={}).observe(kernel, request, lock)
'''),
    ('runtime/inspection.py', '''def restored():
    from agent_run.adapters.systemd import SystemdHost as Manager
    return Manager(environment={})
'''),
    ('runtime/inspection.py', 'def restored(controller: "Kernel"): pass'),
    ('runtime/inspection.py', 'def restored(controller: "core.Kernel"): pass'),
    ('runtime/inspection.py', 'getattr(controller, "host_usage")(action)'),
    ('runtime/inspection.py', 'read_usage = controller.host_usage'),
    ('runtime/inspection.py', 'controller.operation_state()'),
    ('runtime/inspection.py', 'controller.control("stop", action_id)'),
    ('runtime/inspection.py', 'controller.observe_host(request, observation)'),
    ('runtime/inspection.py', 'getattr(controller, "control")("stop", action_id)'),
    ('runtime/inspection.py', 'observe = controller.observe_host'),
    ('runtime/inspection.py', 'def restored(controller: Kernel): return controller.status(subject)'),
    ('core/task_creation.py', 'def restored(controller: Kernel): pass'),
])
def test_observers_reject_passed_core_capabilities_and_concrete_hosts(path, source):
    errors = violations(path, source)
    assert errors, (path, source)
    assert all(error.startswith(path + ':') for error in errors)
    assert any('runtime.inspection' in error for error in errors), errors


@pytest.mark.parametrize('source', [
    '''from agent_run.runtime.foreground_host import (
    HostInspection, WorkerInspection, _bound_record, _inspect_record, _unavailable)
''',
    '''from agent_run.runtime import foreground_host as facts
def inspect(binding: InspectionBindings) -> HostInspection:
    return facts._unavailable(binding.request)
''',
    '''from agent_run.adapters.foreground import ProcessPin
def inspect(binding: InspectionBindings):
    return ProcessPin.open(binding.pid).observe()
''',
    'def failed(error: KernelError): return error.code',
    'def project(observation): return observation.status',
    'def project(port): return port.history()',
    '''class ReadUnavailable(Exception):
    def __init__(self, detail):
        super().__init__(detail)
''',
])
def test_observers_keep_readonly_results_and_physical_helpers(source):
    assert violations('runtime/inspection.py', source) == []


def test_host_coordinator_can_use_its_admitted_core():
    source = '''def coordinate(kernel: Kernel, request):
    return kernel.host_usage(kernel.host_action(request))
'''
    assert violations('runtime/host_control.py', source) == []


@pytest.mark.parametrize('method', [name for name, member in getmembers(Kernel, isfunction)
                                  if not name.startswith('_') and name not in {'status', 'history'}])
@pytest.mark.parametrize('path', ['runtime/inspection.py', 'queries/current_observation.py'])
def test_public_core_surface_cannot_be_passed_to_readonly_observers(path, method):
    # Inspect the actual installed interface, independently of the checker's AST
    # extraction, so an omitted existing/new method becomes a failing case.
    source = f'def restored(controller, *args, **kwargs):\n    return controller.{method}(*args, **kwargs)\n'
    errors = violations(path, source)
    assert any(method in error and 'runtime.inspection' in error for error in errors), (method, errors)
