"""The toolchain the new app's pinned source asks for: Go from go.mod, Node from the UI's engines.

Go: the toolchain line when go.mod has one, else its go line; a complete x.y.z either way. The workflow installs
exactly that version and builds with GOTOOLCHAIN=local, so Go never downloads a different toolchain.

Node: ui/package.json engines.node, either an exact x.y.z (installed exactly) or a >=x.y.z floor. A floor
selects its own major line (setup-node installs that line's newest patch) rather than the newest Node of all,
which could be a major the UI was never built with; the preflight then checks the installed Node against the
floor. Any other range is refused rather than guessed.
"""
import json
from pathlib import Path
import re


def go_version(text):
    go_lines = re.findall(r"^go[ \t]+(\S+)[ \t]*(?://.*)?$", text, flags=re.M)
    toolchain_lines = re.findall(r"^toolchain\b[ \t]*(\S*)[ \t]*(?://.*)?$", text, flags=re.M)
    if len(go_lines) != 1 or len(toolchain_lines) > 1:
        raise ValueError("go.mod must have one go line and at most one toolchain line")
    if toolchain_lines:
        match = re.fullmatch(r"go(\d+\.\d+\.\d+)", toolchain_lines[0])
    else:
        match = re.fullmatch(r"(\d+\.\d+\.\d+)", go_lines[0])
    if not match:
        raise ValueError("go.mod must pin a complete Go version (go 1.27.4, or toolchain go1.27.4)")
    return match.group(1)


def node_spec(text):
    try:
        spec = json.loads(text).get("engines", {}).get("node")
    except (ValueError, AttributeError):
        raise ValueError("ui/package.json is not a JSON object with engines") from None
    if not isinstance(spec, str):
        raise ValueError("ui/package.json must declare engines.node")
    exact = re.fullmatch(r"\s*(\d+)\.(\d+)\.(\d+)\s*", spec)
    if exact:
        return "exact", tuple(int(part) for part in exact.groups())
    floor = re.fullmatch(r"\s*>=\s*(\d+)\.(\d+)\.(\d+)\s*", spec)
    if floor:
        return "floor", tuple(int(part) for part in floor.groups())
    raise ValueError("ui/package.json engines.node must be an exact version or a >= floor, such as >=22.12.0")


def node_setup_version(spec):
    kind, version = spec
    return ".".join(map(str, version)) if kind == "exact" else str(version[0])


def node_satisfies(spec, reported):
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", (reported or "").strip())
    if not match:
        return False
    installed = tuple(int(part) for part in match.groups())
    kind, version = spec
    return installed == version if kind == "exact" else installed >= version


def read(source, relative):
    path = Path(source) / relative
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{relative} is missing from the pinned source")
    return path.read_text(encoding="utf-8")


def resolve(source):
    """{go_version, node_version} for setup-go and setup-node."""
    return {"go_version": go_version(read(source, "go.mod")),
            "node_version": node_setup_version(node_spec(read(source, "ui/package.json")))}
