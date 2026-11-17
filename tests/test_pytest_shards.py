import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

path = Path(__file__).resolve().parents[1] / 'scripts/shard_pytest.py'
spec = importlib.util.spec_from_file_location('shard_pytest', path)
shards = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shards)


class ShardTests(unittest.TestCase):
    def test_full_engine_shards_keep_collecting_failures(self):
        import plan_suites
        for suite in plan_suites.SUITES['aiosxaegra']:
            self.assertNotIn('--maxfail', suite['run'])
            self.assertIn('--junitxml=junit.xml', suite['run'])

    def test_real_pytest_reports_every_synthetic_test_once(self):
        with tempfile.TemporaryDirectory(prefix='builder-shard-') as temporary:
            folder = Path(temporary)
            config = folder / 'pytest.ini'
            config.write_text('[pytest]\n')
            source = folder / 'test_synthetic.py'
            source.write_text(
                'import json, pathlib, uuid, pytest\n' +
                '\n'.join(f'def test_case_{i}(): assert True' for i in range(64)) +
                '\n@pytest.mark.parametrize("ordinal,nonce", [(i,str(uuid.uuid4())) for i in range(16)])\n'
                'def test_random_collection_ids(ordinal, nonce):\n'
                '    with pathlib.Path(__file__).with_name("seen.jsonl").open("a") as out:\n'
                '        out.write(json.dumps(ordinal) + "\\n")\n')
            total = 0
            for index in range(8):
                report = folder / f'{index}.xml'
                result = subprocess.run([sys.executable, str(path), '--index', str(index), '--count', '8',
                                         '--', '-c', str(config), str(source), '-q', f'--junitxml={report}'],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                total += len(list(ET.parse(report).iter('testcase')))
            self.assertEqual(total, 80)
            seen = [int(line) for line in (folder / 'seen.jsonl').read_text().splitlines()]
            self.assertEqual(len(seen), 16)
            self.assertEqual(set(seen), set(range(16)))

    def test_entire_collection_runs_once_across_all_shards_with_no_gaps(self):
        original = [SimpleNamespace(nodeid=f'tests/unit/test_example.py::test_case_{i // 7}[{i % 7}]') for i in range(10000)]
        seen = []
        for index in range(8):
            selected = list(original)
            rejected = []
            hook = SimpleNamespace(pytest_deselected=lambda items: rejected.extend(items))
            shards.Shard(index, 8).pytest_collection_modifyitems(SimpleNamespace(hook=hook), selected)
            self.assertEqual(len(selected) + len(rejected), len(original))
            self.assertTrue(selected)
            seen.extend(item.nodeid for item in selected)
        self.assertEqual(len(seen), len(original))
        self.assertEqual(set(seen), {item.nodeid for item in original})

    def test_partition_is_stable_when_collection_order_changes(self):
        names = ['tests/test_a.py::test_one', 'tests/test_b.py::test_two[param]']
        expected = {name: shards.bucket(name, 8) for name in names}
        self.assertEqual({name: shards.bucket(name, 8) for name in reversed(names)}, expected)

    def test_random_parameter_ids_cannot_change_the_functions_shard(self):
        names = [f'tests/test_a.py::test_one[owner-{i}]' for i in range(100)]
        self.assertEqual(len({shards.bucket(name, 8) for name in names}), 1)
        # A parametrized class also stays together across collectors.
        names = [f'tests/test_b.py::TestOwner[nonce-{i}]::test_reply' for i in range(100)]
        self.assertEqual(len({shards.bucket(name, 8) for name in names}), 1)

    def test_invalid_index_refuses_to_run_pytest(self):
        with self.assertRaises(SystemExit):
            shards.main(['--index', '8', '--count', '8'])
