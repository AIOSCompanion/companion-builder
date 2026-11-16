"""Deploy public orchestration only. Never print credentials or provider error bodies."""
from pathlib import Path
import os
import subprocess
import sys

WRANGLER = "wrangler@4.162.0"
REQUIRED = ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "BRIDGE_APP_ID",
            "BRIDGE_APP_PRIVATE_KEY", "BRIDGE_WEBHOOK_SECRET", "BRIDGE_STATUS_TOKEN")


def deploy(env, run=subprocess.run):
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        raise ValueError("Missing configuration: " + ", ".join(missing))
    if not env["BRIDGE_APP_ID"].isdigit() or env.get("BRIDGE_ENABLED") not in ("true", "false"):
        raise ValueError("Invalid app ID or enable flag")
    # Only deployment credentials reach Wrangler. App/webhook/status keys go through stdin.
    clean_env = {k: v for k, v in env.items() if k not in (
        "BRIDGE_APP_PRIVATE_KEY", "BRIDGE_WEBHOOK_SECRET", "BRIDGE_STATUS_TOKEN")}
    base = ["npx", "--yes", WRANGLER]
    def call(args, value=None):
        result = run(base + args, cwd=Path(__file__).resolve().parents[1] / "bridge",
                     env=clean_env, input=value, text=True, capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError("Bridge deployment failed; provider output withheld to protect credentials")
    def publish(enabled):
        call(["deploy", "--var", f"BRIDGE_APP_ID:{env['BRIDGE_APP_ID']}",
              "--var", f"BRIDGE_ENABLED:{enabled}"])
    # The first deployment never accepts webhooks before all secrets exist.
    publish("false")
    for name in ("BRIDGE_APP_PRIVATE_KEY", "BRIDGE_WEBHOOK_SECRET", "BRIDGE_STATUS_TOKEN"):
        call(["secret", "put", name], env[name])
    if env["BRIDGE_ENABLED"] == "true":
        publish("true")


def main():
    try:
        deploy(dict(os.environ))
        print("Bridge deployment completed; event acceptance " + os.environ["BRIDGE_ENABLED"])
        return 0
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
