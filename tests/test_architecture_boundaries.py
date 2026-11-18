"""Normal checks must reject reintroduced reverse imports, including local ones."""
import importlib.util
from pathlib import Path

import pytest


CHECKER = Path(__file__).resolve().parents[1] / '.github' / 'ci' / 'check_boundaries.py'
spec = importlib.util.spec_from_file_location('capability_boundaries', CHECKER)
assert spec is not None and spec.loader is not None
boundaries = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boundaries)


def test_product_capability_boundaries():
    assert boundaries.check(CHECKER.parents[2] / 'src' / 'agent_run') == []


@pytest.mark.parametrize(('module', 'bad_import'), [
    ('adapters/github_commands.py', 'from agent_run.adapters.auth.native_probe import _ErrorEvidence'),
    ('adapters/github_commands.py', 'from .auth import native_probe'),
    ('adapters/remote_observation.py', 'from agent_run.adapters.remote import RemoteEffects'),
    ('adapters/remote_observation.py', 'from . import remote'),
    ('adapters/github_transport.py', 'import agent_run.adapters.remote as effects'),
    ('adapters/status_comment.py', 'from agent_run.adapters import remote'),
    ('notifications/status_comment.py', 'from agent_run.core.kernel import Kernel'),
    ('notifications/sender.py', 'from agent_run.core.kernel import Kernel'),
])
@pytest.mark.parametrize('local', [False, True])
def test_reverse_dependency_is_rejected_even_inside_function(module, bad_import, local):
    source = 'def callback():\n    ' + bad_import if local else bad_import
    failures = boundaries.violations(source, module)
    assert len(failures) == 1
    assert module in failures[0]


@pytest.mark.parametrize('module', [
    'adapters/github.py',
    'adapters/source.py',
    'adapters/github_transport.py',
    'adapters/remote_observation.py',
    'adapters/status_comment.py',
    'notifications/boundary_projection.py',
    'notifications/status_comment.py',
    'notifications/sender.py',
])
@pytest.mark.parametrize('form', ['direct', 'module', 'package', 'relative', 'relative_package'])
@pytest.mark.parametrize('local', [False, True])
def test_read_boundaries_reject_combined_effect_capability(module, form, local):
    relative = '.' if module.startswith('adapters/') else '..adapters.'
    package = '.' if module.startswith('adapters/') else '..adapters'
    bad_import = {
        'direct': 'from agent_run.adapters.operations import NativeOperations',
        'module': 'import agent_run.adapters.operations as operations',
        'package': 'from agent_run.adapters import operations',
        'relative': f'from {relative}operations import NativeOperations',
        'relative_package': f'from {package} import operations',
    }[form]
    source = 'def callback():\n    ' + bad_import if local else bad_import
    failures = boundaries.violations(source, module)
    assert len(failures) == 1
    assert 'agent_run.adapters.operations' in failures[0]
