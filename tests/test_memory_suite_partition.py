"""The idle-store benchmark job complements the normal suite without gaps."""
import importlib.util
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('memory_suite_plan', ROOT / 'scripts/plan_suites.py')
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


class MemoryPartitionTests(unittest.TestCase):
    def test_real_pytest_covers_normal_and_benchmark_cases_once_and_keeps_failures(self):
        with tempfile.TemporaryDirectory(prefix='builder-memory-partition-') as temporary:
            root = Path(temporary)
            (root / 'tests').mkdir()
            (root / 'pytest.ini').write_text('[pytest]\n')
            (root / 'tests/test_normal.py').write_text(
                'def test_normal_one(): pass\ndef test_normal_two(): pass\n')
            benchmark = root / 'tests/test_memctx_scale_116k.py'
            benchmark.write_text(
                'import pytest\n'
                'def test_first(): pass\n'
                'def test_second(): pass\n'
                '@pytest.mark.parametrize("n", [1, 2, 3])\n'
                'def test_future_parameterized_case(n): assert n > 0\n')
            suites = plan.SUITES['companion-memory']
            self.assertEqual(len(suites), 2)
            seen = []
            for suite, expected in zip(suites, (2, 5)):
                args = shlex.split(suite['run'])
                args[0] = sys.executable
                result = subprocess.run(args, cwd=root, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                cases = list(ET.parse(root / 'junit.xml').iter('testcase'))
                self.assertEqual(len(cases), expected)
                seen.extend((case.get('classname'), case.get('name')) for case in cases)
            self.assertEqual(len(seen), 7)
            self.assertEqual(len(set(seen)), 7)

            benchmark.write_text('def test_real_failure(): assert False\n')
            args = shlex.split(suites[1]['run'])
            args[0] = sys.executable
            result = subprocess.run(args, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual(len(list(ET.parse(root / 'junit.xml').iter('failure'))), 1)


if __name__ == '__main__':
    unittest.main()
