"""Print the totals from a junit report, and nothing else.

The public log must never show test names, messages, tracebacks or source lines. Names come from
the PR's own test run, so they can carry private code, and a name can also carry a workflow
command. This reads the junit XML and prints one counts line. The names of failing tests stay in
the junit report, which the workflow encrypts before upload.
"""
import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def summarise(report):
    root = ET.parse(report).getroot()
    cases = list(root.iter("testcase"))
    failed = errored = skipped = 0
    for case in cases:
        if case.find("failure") is not None:
            failed += 1
        elif case.find("error") is not None:
            errored += 1
        elif case.find("skipped") is not None:
            skipped += 1
    return len(cases), failed, errored, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--exit", default="unknown", help="the suite's exit code")
    args = parser.parse_args()
    if not args.report.exists():
        print(f"no junit report; suite exit {args.exit}")
        return 0
    total, failed, errored, skipped = summarise(args.report)
    passed = total - failed - errored - skipped
    print(f"tests {total}, passed {passed}, failed {failed}, errors {errored}, skipped {skipped}; suite exit {args.exit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
