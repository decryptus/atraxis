"""Independent unittest suites may legitimately reuse module basenames."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CollectionGuardTests(unittest.TestCase):
    def guard(self, second):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            one, two = root / 'one', root / 'two'
            one.mkdir(); two.mkdir()
            (one / 'test_example.py').write_text('import unittest\nclass First(unittest.TestCase):\n    def test_first(self): pass\n')
            (two / 'test_example.py').write_text(second)
            script = Path(__file__).resolve().parents[1] / 'scripts' / 'check-test-collection.py'
            return subprocess.run([sys.executable, str(script), '--runner', 'unittest', str(one), str(two)],
                                  cwd=root, capture_output=True, text=True, timeout=20)

    def test_same_basename_in_independent_suites_is_collected(self):
        result = self.guard('import unittest\nclass Second(unittest.TestCase):\n    def test_second(self): pass\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('1 declarations, 1 cases'), 2)

    def test_uncollected_function_in_second_suite_still_fails(self):
        result = self.guard('import unittest\nclass Second(unittest.TestCase):\n    def test_second(self): pass\ndef test_missing(): pass\n')
        self.assertEqual(result.returncode, 1)
        self.assertIn('test_missing', result.stderr)
