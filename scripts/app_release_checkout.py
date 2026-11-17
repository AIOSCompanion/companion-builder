"""Private checkout with sealed git output and no persisted authentication."""
import base64
import os
from pathlib import Path
import subprocess

from app_release_artifacts import SHA
from app_release_identity import SOURCE


def checkout(env, run=subprocess.run):
    commit = env.get("SOURCE_SHA", "")
    token = env.get("GH_TOKEN", "")
    if not SHA.fullmatch(commit) or not token:
        raise ValueError("Immutable source and read-only credential are required")
    workspace = Path(env["GITHUB_WORKSPACE"]).resolve(strict=True)
    source = Path(env["APP_SOURCE_DIR"]).resolve()
    if source.parent != workspace or source.name != "source" or source.exists():
        raise ValueError("Private checkout must use a new source directory inside this job's workspace")
    clean = {key: value for key, value in env.items() if key not in ("GH_TOKEN", "GITHUB_TOKEN") and not key.startswith("GIT_CONFIG_")}
    clean.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_LFS_SKIP_SMUDGE="1", GIT_CONFIG_GLOBAL=os.devnull)
    header = "AUTHORIZATION: basic " + base64.b64encode(("x-access-token:" + token).encode()).decode()
    authenticated = dict(clean, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader", GIT_CONFIG_VALUE_0=header)
    commands = [(["git", "init", str(source)], clean),
                (["git", "-C", str(source), "remote", "add", "origin", "https://github.com/" + SOURCE + ".git"], clean),
                (["git", "-C", str(source), "fetch", "--no-recurse-submodules", "origin",
                  "+refs/heads/main:refs/remotes/origin/main", "+refs/tags/*:refs/tags/*"], authenticated),
                (["git", "-C", str(source), "checkout", "--detach", commit], clean)]
    for args, child in commands:
        result = run(args, env=child, timeout=300, check=False)
        if result.returncode:
            raise RuntimeError("Private checkout failed; git output is sealed")
    config = (source / ".git/config").read_text(encoding="utf-8")
    if token in config or header in config:
        raise RuntimeError("Authentication was unexpectedly persisted")
