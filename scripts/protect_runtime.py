"""Remove test-user write access to hosted Linux tools before untrusted installation or tests.

The hosted tool cache can be group/world writable. A clean environment is insufficient if a test can
replace the trusted interpreter, an imported library or an executable searched by a later trusted step.
This changes permissions on disposable hosted-runner tools only, never a product checkout.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def targets(path, prefixes, workspace):
    workspace = Path(workspace).resolve()
    roots = {Path(value).resolve() for value in [*path.split(os.pathsep), *prefixes] if value and Path(value).is_dir()}
    if any(root == Path(root.anchor) or root == workspace or root.is_relative_to(workspace) for root in roots):
        raise ValueError("trusted runtime paths must not include a root or product checkout")
    return sorted(roots, key=str)


def protect(roots, run=subprocess.run):
    parents = set()
    for root in roots:
        if run(["sudo", "-n", "chmod", "-R", "go-w", str(root)], check=False).returncode:
            raise RuntimeError("a trusted runtime could not be protected")
        parents.update(parent for parent in root.parents if parent != Path(parent.anchor))
    for parent in sorted(parents, key=str):
        if run(["sudo", "-n", "chmod", "go-w", str(parent)], check=False).returncode:
            raise RuntimeError("a runtime parent could not be protected")


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_OS") != "Linux":
        print("runtime protection runs only on a disposable hosted Linux runner")
        return 2
    try:
        roots = targets(os.environ.get("PATH", ""), (sys.prefix, sys.base_prefix), os.environ["GITHUB_WORKSPACE"])
        protect(roots)
        print(f"runtime protection: {len(roots)} trusted tool directories and their parents refuse test-user writes")
        return 0
    except (ValueError, KeyError, RuntimeError):
        print("runtime protection failed; untrusted code must not run")
        return 1


if __name__ == "__main__":
    sys.exit(main())
