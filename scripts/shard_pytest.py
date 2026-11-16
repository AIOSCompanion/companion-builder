"""Run one deterministic, disjoint share of a complete pytest collection."""
import argparse
import hashlib
import sys


def bucket(nodeid, count):
    return int.from_bytes(hashlib.sha256(nodeid.encode()).digest()[:8], 'big') % count


class Shard:
    def __init__(self, index, count):
        self.index, self.count = index, count

    def pytest_collection_modifyitems(self, config, items):
        selected, deselected = [], []
        for item in items:
            (selected if bucket(item.nodeid, self.count) == self.index else deselected).append(item)
        items[:] = selected
        config.hook.pytest_deselected(items=deselected)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', type=int, required=True)
    parser.add_argument('--count', type=int, required=True)
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not 1 <= args.count <= 16 or not 0 <= args.index < args.count:
        parser.error('invalid shard')
    import pytest
    pytest_args = args.pytest_args
    if pytest_args[:1] == ['--']:
        pytest_args = pytest_args[1:]
    return pytest.main(pytest_args, plugins=[Shard(args.index, args.count)])


if __name__ == '__main__':
    sys.exit(main())
