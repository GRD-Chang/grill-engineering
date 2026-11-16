"""Function-local imports and aliases must not restore controller dependencies."""
from pathlib import Path
import tempfile
import unittest

from core_boundaries import CAPABILITIES, violations


class CoreBoundariesTest(unittest.TestCase):
    def test_import_counterexamples_and_supported_boundary(self):
        counterexamples = (
            'from agent_run.core.kernel import Kernel as Controller',
            'def restore():\n    from agent_run.core import kernel as controller',
            'def restore():\n    import agent_run.core.operation_runner as driver',
            'from .kernel import Kernel',
            'from agent_run.core import *',
        )
        with tempfile.TemporaryDirectory(prefix='core-boundaries-') as temporary:
            root = Path(temporary)
            for name in (*CAPABILITIES, 'core/operation_runner.py'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('')
            capability = root / 'core/repair.py'
            for source in counterexamples:
                with self.subTest(source=source):
                    capability.write_text(source)
                    self.assertIn('cannot import Kernel', '\n'.join(violations(root)))
            capability.write_text('from agent_run.core.delivery_state import DeliveryState')
            self.assertEqual(violations(root), [])
            (root / 'core/operation_runner.py').write_text('kernel._wait_for_read(fence, execution)')
            self.assertIn('original ReadFence/work token', '\n'.join(violations(root)))


if __name__ == '__main__':
    unittest.main()
