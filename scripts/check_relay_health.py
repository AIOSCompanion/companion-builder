#!/usr/bin/env python3
"""Poll the relay's public /health until it reports healthy, or fail.

The predicate is the one the relay's deployment controller uses after a deploy
(aiosxaegra scripts/deployment_common.py, healthy()). A degraded, busy or stale
HTTP 200 is not healthy. Component values are not printed: only the names of the
components that are not connected, so no connection detail reaches the log.

    python3 -I -B scripts/check_relay_health.py --url https://<relay-domain>/health
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

COMPONENTS = ("database", "langgraph_checkpointer", "langgraph_store")
MAX_BODY = 65536


def is_healthy(payload) -> bool:
    """True only for the controller's healthy shape: status healthy, every component connected."""
    return (isinstance(payload, dict)
            and payload.get("status") == "healthy"
            and all(payload.get(component) == "connected" for component in COMPONENTS))


def describe_unhealthy(payload) -> str:
    if not isinstance(payload, dict):
        return "the response is not a JSON object"
    names = (["status"] if payload.get("status") != "healthy" else []) + [
        component for component in COMPONENTS if payload.get(component) != "connected"]
    return "not connected or not healthy: " + ", ".join(names)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def fetch(url: str, timeout: float):
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        raw = response.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise ValueError("response exceeds its bound")
    return json.loads(raw)


def check(url, attempts, interval, *, timeout=10.0, fetcher=fetch, sleep=time.sleep) -> bool:
    last = "no attempt was made"
    for attempt in range(1, attempts + 1):
        try:
            payload = fetcher(url, timeout)
        except (OSError, ValueError) as exc:
            last = f"unreachable ({type(exc).__name__})"
        else:
            if is_healthy(payload):
                print(json.dumps({"attempt": attempt, "healthy": True}))
                return True
            last = describe_unhealthy(payload)
        print(f"attempt {attempt}/{attempts}: {last}")
        if attempt < attempts:
            sleep(interval)
    print(f"::error::the relay did not report healthy after {attempts} attempts ({last})", file=sys.stderr)
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--attempts", type=int, default=20)
    parser.add_argument("--interval", type=float, default=15.0)
    args = parser.parse_args(argv)
    parts = urlsplit(args.url)
    if parts.scheme != "https" or not parts.hostname:
        parser.error("--url must be an https URL")
    if not 1 <= args.attempts <= 60 or not 0 <= args.interval <= 60:
        parser.error("attempts must be 1-60 and interval 0-60 seconds")
    return 0 if check(args.url, args.attempts, args.interval) else 1


if __name__ == "__main__":
    raise SystemExit(main())
